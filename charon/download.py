"""CHARON.EXE — download staging: pure logic.

Phase 2 of the Crossing: Download. Charon fetches the updates a scan
found outdated and stages them — **nothing is installed here**; the
toll (Phase 3) and the crossing itself (Phase 4) come later, in
order, always.

This module is pure logic. It builds the download plan from a scan
report, revalidates the plan against a *fresh* Windows Update search
(an update can be withdrawn or replaced between scan and download —
when that happens Charon marks it ``no_longer_available`` and
downloads **nothing** in its place; silent substitution is a betrayal
of the manifest), drives the one download seam, and keeps Charon's
download ledger: the record of what is aboard the ferry.

The actual fetch lives behind exactly one seam (``wua_download``) and
is injected here as a callable, so every behavior in this module is
testable anywhere. Staging truth, stated here as in the seam:
``Download()`` stages into the Windows Update cache — there is no
Charon target folder; the ledger is Charon's record, the cache is
Windows'.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .inventory import normalize_update
from .model import (
    STATUS_UPDATE_AVAILABLE,
    DeviceReport,
    ScanReport,
    UpdateCandidate,
    format_date,
)

log = logging.getLogger("charon.download")

# ----------------------------------------------------------------------
# Plan / result vocabulary
# ----------------------------------------------------------------------

RESULT_NO_LONGER_AVAILABLE = "no_longer_available"
RESULT_SKIPPED = "skipped"

#: Statuses that mean the driver is genuinely staged on the PC.
STAGED_STATUSES = frozenset({"succeeded", "succeeded_with_errors"})

#: Every status a ledger row can carry.
LEDGER_STATUSES = STAGED_STATUSES | {
    "failed",
    "aborted",
    RESULT_NO_LONGER_AVAILABLE,
    RESULT_SKIPPED,
}

# ----------------------------------------------------------------------
# Progress events (Phase 5's animated UI consumes these)
# ----------------------------------------------------------------------

EVENT_UPDATE_STARTED = "update_started"
EVENT_UPDATE_PROGRESS = "update_progress"
EVENT_UPDATE_COMPLETED = "update_completed"
EVENT_BATCH_COMPLETED = "batch_completed"


@dataclass(frozen=True)
class ProgressEvent:
    """One beat in a download run, for screens and transcripts.

    ``kind`` is one of the EVENT_* constants. ``percent`` is only ever
    set by the seam's progress callback and may be ``None`` the whole
    run — screens must treat progress as a bonus, never a promise.
    """

    kind: str
    device_name: str = ""
    title: str = ""
    percent: int | None = None
    status: str = ""
    message: str = ""


EmitFn = Callable[[ProgressEvent], None]

# A fetch callable: exactly ``wua_download.download_updates``' shape,
# injected so this module never imports the seam.
FetchFn = Callable[..., list]


def _noop_emit(event: ProgressEvent) -> None:
    pass


# ----------------------------------------------------------------------
# The plan
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class DownloadPlanEntry:
    """One passenger ticket: this device's driver rides the ferry."""

    update: UpdateCandidate
    device_name: str
    device_id: str = ""

    @property
    def key(self) -> str:
        """Stable identity for selection and the ledger."""
        return self.device_id or self.device_name


@dataclass
class DownloadPlan:
    entries: list[DownloadPlanEntry] = field(default_factory=list)
    # Selection tokens the user named that matched nothing.
    not_found: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.entries


def _selection_matches(entry: DownloadPlanEntry, token: str) -> bool:
    token = token.strip()
    if not token:
        return False
    if entry.device_id and token.lower() == entry.device_id.lower():
        return True
    return token.lower() == entry.device_name.lower()


def build_download_plan(
    report: ScanReport, selection: list[str] | None = None
) -> DownloadPlan:
    """Plan from a scan report's ``update_available`` devices.

    ``selection`` — when given — restricts the plan to the named
    devices (by device ID, falling back to device name): this is the
    hook the future UI's driver picker uses. Tokens that match no
    outdated device land in ``plan.not_found`` so the caller can say
    so out loud instead of silently shrinking the manifest.
    """
    available: list[DownloadPlanEntry] = []
    for device in report.devices:
        if device.status != STATUS_UPDATE_AVAILABLE or device.update is None:
            continue
        available.append(
            DownloadPlanEntry(
                update=device.update,
                device_name=device.driver.device_name,
                device_id=device.driver.device_id,
            )
        )
    if selection is None:
        return DownloadPlan(entries=available)

    chosen: list[DownloadPlanEntry] = []
    not_found: list[str] = []
    for token in selection:
        hits = [e for e in available if _selection_matches(e, token)]
        if hits:
            for hit in hits:
                if hit not in chosen:
                    chosen.append(hit)
        else:
            not_found.append(token)
    # Plan order follows the manifest, not the selection order.
    chosen.sort(key=lambda e: available.index(e))
    return DownloadPlan(entries=chosen, not_found=not_found)


# ----------------------------------------------------------------------
# Revalidation against a fresh search
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedEntry:
    """A plan entry checked against the fresh search, moments ago."""

    entry: DownloadPlanEntry
    available: bool
    com_update: Any = None  # the fresh seam object; never serialized
    note: str = ""


def _same_update(plan: UpdateCandidate, fresh: UpdateCandidate) -> bool:
    """Is the fresh result the *same* update the scan promised?

    Same title and same version. Hardware IDs must overlap when both
    sides name them. A newer version under the same title is a
    *different* offer — revalidation never upgrades the ticket.
    """
    if plan.title.strip().lower() != fresh.title.strip().lower():
        return False
    if (plan.version or "") != (fresh.version or ""):
        return False
    if plan.hardware_ids and fresh.hardware_ids:
        return bool(set(plan.hardware_ids) & set(fresh.hardware_ids))
    return True


def revalidate_plan(
    plan: DownloadPlan, fresh_raw_updates: list[dict]
) -> list[ResolvedEntry]:
    """Check every ticket against a search run *just now*.

    ``fresh_raw_updates`` are the seam's raw mappings; each may carry
    an opaque ``com_update`` object, which is attached to the entry
    it resolves — the download then uses **the fresh object**, never
    a stale handle from scan time. Anything that no longer resolves
    comes back ``available=False`` with a plain-language note, and
    nothing is ever substituted in its place.
    """
    fresh: list[tuple[UpdateCandidate, Any]] = []
    for raw in fresh_raw_updates:
        fresh.append((normalize_update(raw), raw.get("com_update")))

    resolved: list[ResolvedEntry] = []
    for entry in plan.entries:
        hit = next(
            ((cand, obj) for cand, obj in fresh if _same_update(entry.update, cand)),
            None,
        )
        if hit is not None:
            resolved.append(
                ResolvedEntry(entry=entry, available=True, com_update=hit[1])
            )
            continue
        update = entry.update
        note = (
            f"Windows Update no longer offers version "
            f"{update.version or 'unknown'} of the "
            f"{entry.device_name} driver (offered at scan time from "
            f"{format_date(update.driver_date)}). It may have been "
            f"withdrawn or replaced. Charon downloaded nothing in "
            f"its place."
        )
        resolved.append(
            ResolvedEntry(entry=entry, available=False, note=note)
        )
    return resolved


# ----------------------------------------------------------------------
# Staging: drive the one seam, honestly
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class StagedResult:
    """The final word on one plan entry for this run."""

    entry: DownloadPlanEntry
    status: str  # a LEDGER_STATUSES member
    detail: str = ""
    eula_accepted: bool = False
    size_bytes: int | None = None
    result_code: int | None = None
    hresult: int | None = None

    @property
    def staged(self) -> bool:
        return self.status in STAGED_STATUSES


def stage_plan(
    resolved: list[ResolvedEntry],
    fetch: FetchFn,
    emit: EmitFn | None = None,
) -> list[StagedResult]:
    """Download everything that still resolves; report all of it.

    ``fetch`` is the download seam's entry point, injected. The batch
    is fetched in one call (that is how Windows Update works); the
    results come back per update and are reported per update — one
    driver's failure never repaints another's verdict.
    """
    emit = emit or _noop_emit
    by_entry: dict[int, StagedResult] = {}
    live = [r for r in resolved if r.available]

    for item in resolved:
        if not item.available:
            by_entry[id(item.entry)] = StagedResult(
                entry=item.entry,
                status=RESULT_NO_LONGER_AVAILABLE,
                detail=item.note,
            )

    if live:
        for item in live:
            emit(
                ProgressEvent(
                    kind=EVENT_UPDATE_STARTED,
                    device_name=item.entry.device_name,
                    title=item.entry.update.title,
                )
            )
        fetched = fetch(
            [item.com_update for item in live], emit=emit
        )
        if len(fetched) != len(live):
            # A seam that answers for fewer updates than it was given
            # has broken the contract; refuse to invent verdicts.
            raise ValueError(
                f"download seam returned {len(fetched)} results for "
                f"{len(live)} updates"
            )
        for item, outcome in zip(live, fetched):
            status = outcome.status
            detail = outcome.detail
            if status not in LEDGER_STATUSES:
                # A seam that answers in a vocabulary Charon doesn't
                # know has not staged anything Charon can vouch for:
                # count it as failed, never as (say) withdrawn — the
                # summary and the ledger counts must stay honest.
                detail = (
                    f"{detail} " if detail else ""
                ) + (
                    f"The download step returned an unrecognized "
                    f"result ({status!r}); Charon counts that as "
                    f"not downloaded."
                )
                status = "failed"
            by_entry[id(item.entry)] = StagedResult(
                entry=item.entry,
                status=status,
                detail=detail,
                eula_accepted=outcome.eula_accepted,
                size_bytes=outcome.size_bytes,
                result_code=outcome.result_code,
                hresult=outcome.hresult,
            )
            emit(
                ProgressEvent(
                    kind=EVENT_UPDATE_COMPLETED,
                    device_name=item.entry.device_name,
                    title=item.entry.update.title,
                    status=outcome.status,
                    message=outcome.detail,
                )
            )

    # Verdicts are reported in manifest order, not completion order.
    results = [by_entry[id(item.entry)] for item in resolved]

    staged = sum(1 for r in results if r.staged)
    failed = sum(
        1 for r in results if r.status in ("failed", "aborted")
    )
    gone = sum(1 for r in results if r.status == RESULT_NO_LONGER_AVAILABLE)
    emit(
        ProgressEvent(
            kind=EVENT_BATCH_COMPLETED,
            status="complete",
            message=(
                f"{staged} of {len(results)} drivers downloaded"
                + (f"; {failed} failed" if failed else "")
                + (f"; {gone} no longer offered" if gone else "")
                + ". Nothing was installed."
            ),
        )
    )
    return results


def summarize_results(results: list[StagedResult]) -> str:
    """One honest sentence-set about a finished staging run."""
    if not results:
        return "Nothing was selected for download."
    staged = [r for r in results if r.staged]
    lines: list[str] = []
    total = len(results)
    if len(staged) == total:
        lines.append(
            f"All {total} driver{'s' if total != 1 else ''} "
            f"downloaded and staged."
        )
    else:
        lines.append(
            f"{len(staged)} of {total} drivers downloaded; "
            + "; ".join(
                f"the {r.entry.device_name} driver "
                f"{'failed' if r.status in ('failed', 'aborted') else 'is no longer offered by Windows Update'}"
                for r in results
                if not r.staged
            )
            + "."
        )
    lines.append(
        "Nothing was installed — downloads are staged only. The toll "
        "comes before anything is applied."
    )
    return " ".join(lines)


# ----------------------------------------------------------------------
# The download ledger
# ----------------------------------------------------------------------

LEDGER_FILENAME = "ledger.json"
LEDGER_FORMAT_VERSION = 1


def default_ledger_path() -> Path:
    """Where the ledger lives when nobody overrides it.

    Next to Charon's other app state: ``%PROGRAMDATA%\\Charon`` on
    Windows, the XDG data dir elsewhere. The ledger is a record, not
    a setting — losing it loses history, never safety.
    """
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
        return base / "Charon" / LEDGER_FILENAME
    base = Path(
        os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
    )
    return base / "charon" / LEDGER_FILENAME


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def load_ledger(path: Path) -> dict:
    """Read the ledger; a missing or unreadable one starts empty.

    A corrupt ledger must never take the ferry down with it — the
    worst case is lost history, and that is said out loud in the log.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"format": LEDGER_FORMAT_VERSION, "runs": []}
    except (OSError, ValueError) as exc:
        log.warning("ledger at %s unreadable (%s); starting fresh", path, exc)
        return {"format": LEDGER_FORMAT_VERSION, "runs": []}
    if not isinstance(data, dict) or not isinstance(data.get("runs"), list):
        log.warning("ledger at %s has an unexpected shape; starting fresh", path)
        return {"format": LEDGER_FORMAT_VERSION, "runs": []}
    return data


def _result_row(result: StagedResult, moment: datetime) -> dict:
    update = result.entry.update
    return {
        "device_id": result.entry.device_id,
        "device_name": result.entry.device_name,
        "update_title": update.title,
        "update_version": update.version,
        "update_date": format_date(update.driver_date)
        if update.driver_date
        else None,
        "result": result.status,
        "eula_accepted": result.eula_accepted,
        "size_bytes": result.size_bytes,
        "downloaded_at": _iso(moment) if result.staged else None,
        "result_code": result.result_code,
        "hresult": result.hresult,
        "detail": result.detail,
    }


# ----------------------------------------------------------------------
# Ledger write serialization
#
# The ledger is one file shared by the download runs (here) and the
# toll records (``toll.py``). Atomic replace alone does not make a
# load → modify → rewrite cycle safe when two Charon processes (or
# threads) write at once: both used to stage through the SAME fixed
# ``ledger.json.tmp`` sibling — one process's replace then moved the
# other's temp file out from under it (FileNotFoundError) — and the
# last writer silently erased the other's appended records. Writers
# now hold an OS-level lock on a ``.lock`` sibling for the whole
# cycle (the kernel releases it if a holder dies, so no stale-lock
# cleanup is ever needed), and each writer stages through a unique
# temp name. The lock is best-effort: if it cannot be taken, the
# write still proceeds — a lost race then costs history, never
# safety, because every reader tolerates a missing record.
# ----------------------------------------------------------------------

_ledger_lock_guard = threading.Lock()
_ledger_lock_holds: dict[tuple[str, int], int] = {}


@contextlib.contextmanager
def _ledger_lock(path: Path):
    """Hold the ledger's write lock for one load-modify-write cycle.

    Re-entrant for the holding thread; distinct threads and
    processes contend on the OS lock itself.
    """
    path = Path(path)
    key = (str(path), threading.get_ident())
    with _ledger_lock_guard:
        held = _ledger_lock_holds.get(key, 0)
        _ledger_lock_holds[key] = held + 1
    fd = None
    locked = False
    try:
        if not held:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(
                    path.with_name(path.name + ".lock"),
                    os.O_CREAT | os.O_RDWR,
                    0o644,
                )
            except OSError:
                fd = None  # proceed unlocked; the write itself decides
            if fd is not None:
                try:
                    if sys.platform.startswith("win"):
                        import msvcrt

                        if os.fstat(fd).st_size == 0:
                            os.write(fd, b"\0")
                        # msvcrt.locking locks from the fd's current
                        # offset, not from 0: the seed write above
                        # leaves a fresh fd at offset 1 while every
                        # later fd starts at 0, so without rewinding
                        # the first writer locks byte 1 and the rest
                        # lock byte 0 — two writers, no contention.
                        # Every writer must contend for byte 0.
                        os.lseek(fd, 0, os.SEEK_SET)
                        for _attempt in range(200):
                            try:
                                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                                locked = True
                                break
                            except OSError:
                                time.sleep(0.05)
                    else:
                        import fcntl

                        fcntl.flock(fd, fcntl.LOCK_EX)
                        locked = True
                except OSError:
                    log.warning(
                        "ledger lock for %s unavailable; writing unlocked",
                        path,
                    )
        yield
    finally:
        if fd is not None:
            try:
                if locked:
                    if sys.platform.startswith("win"):
                        import msvcrt

                        # Unlock the same byte that was locked:
                        # locking/unlocking both address byte 0.
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)
        with _ledger_lock_guard:
            depth = _ledger_lock_holds.get(key, 1) - 1
            if depth > 0:
                _ledger_lock_holds[key] = depth
            else:
                _ledger_lock_holds.pop(key, None)


def _atomic_write_ledger(path: Path, ledger: dict) -> None:
    """Rewrite the ledger whole: unique temp sibling + ``os.replace``.

    A crash mid-write can never leave a torn ledger behind: the old
    record survives whole or the new one lands whole. The replace is
    retried briefly — on Windows a reader holding the file open for
    a moment can make the first replace attempt fail.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    )
    try:
        tmp.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")
        last_exc: OSError | None = None
        for _attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except OSError as exc:
                last_exc = exc
                time.sleep(0.05)
        raise last_exc  # type: ignore[misc]
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def append_run_to_ledger(
    path: Path,
    results: list[StagedResult],
    *,
    source: str = "scan",
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> dict:
    """Append one run to the ledger, atomically.

    The file is rewritten via a temp sibling + ``os.replace``, so a
    crash mid-write can never leave a torn ledger behind: the old
    record survives whole or the new one lands whole. Concurrent
    writers are serialized with :func:`_ledger_lock`, so an
    interleaved toll record is never erased by this rewrite (nor
    this run by a toll's).
    """
    finished = finished_at or _utc_now()
    started = started_at or finished
    run = {
        "started_at": _iso(started),
        "finished_at": _iso(finished),
        "source": source,
        "counts": {
            "planned": len(results),
            "downloaded": sum(1 for r in results if r.staged),
            "failed": sum(
                1 for r in results if r.status in ("failed", "aborted")
            ),
            "no_longer_available": sum(
                1 for r in results if r.status == RESULT_NO_LONGER_AVAILABLE
            ),
        },
        "entries": [_result_row(r, finished) for r in results],
    }
    path = Path(path)
    with _ledger_lock(path):
        ledger = load_ledger(path)
        ledger.setdefault("format", LEDGER_FORMAT_VERSION)
        ledger["runs"].append(run)
        _atomic_write_ledger(path, ledger)
    return run
