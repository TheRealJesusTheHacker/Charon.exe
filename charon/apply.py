"""CHARON.EXE — the apply engine: pure logic for the crossing.

Phase 4 of the Crossing: Apply. Everything before this module was
preparation — the scan (Phase 1) listed the passengers, the
download (Phase 2) brought them aboard, the toll (Phase 3) proved
the way back exists. This module drives the one install seam
(``wua_install``) and keeps the apply record in the same ledger.

The order is enforced here, by construction, not by politeness:

1. **The staged set comes from the ledger** — exactly what Phase 2
   staged, minus what an earlier crossing already applied. Nothing
   else can be applied; there is no other source of candidates.
2. **Administrator rights are confirmed** with the toll seam's probe
   — installing drivers is administrator work.
3. **A fresh search re-validates every staged update** moments
   before the crossing (Phase 2's revalidation discipline, applied
   again): an update Windows Update no longer offers — withdrawn,
   replaced, superseded — is marked ``no_longer_available`` and
   skipped, and *nothing is installed in its place*. A swapped
   staged set is caught here, never crossed on.
4. **THE GATE, FRESH.** Only then — with installs about to begin —
   :func:`charon.toll.require_paid_toll` re-verifies the toll
   against the live PC. Not a cached "paid" flag from earlier in
   the session: the gate enumerates the PC's restore points at
   apply time and the record must still prove out. This is the
   design answer to the gate→apply gap the Phase 3 audit recorded:
   the check and the crossing are one breath apart, and the gate
   runs inside it.
5. **One crossing at a time.** Each update installs alone; after
   each, the device is checked (still reporting in? carrying the
   new version? no longer offered as not-installed?). A driver
   whose install fails — or whose device doesn't come up healthy —
   **stops the line**: the remaining drivers are recorded as held
   back, not attempted. The way back (the toll's restore point)
   is named in the summary.

Further laws of the crossing:

- An update whose license terms Phase 2 did not record as accepted
  is never installed — it is recorded ``eula_not_accepted`` and
  reported. Paperwork first, always.
- A restart Windows Update asks for is **reported, never forced**.
  No code path here can restart a PC.
- Every verdict lands in the ledger through the same locked,
  atomic writer the download and toll records use — per update,
  never as a blanket claim.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .download import (
    LEDGER_FORMAT_VERSION,
    ProgressEvent,
    RESULT_NO_LONGER_AVAILABLE,
    STAGED_STATUSES,
    _atomic_write_ledger,
    _iso,
    _ledger_lock,
    _same_update,
    _utc_now,
    default_ledger_path,
    load_ledger,
)
from .inventory import merge_inventories, normalize_update
from .model import UpdateCandidate, compare_versions
from .platform_inventory import InventoryUnavailable, collect_inventory
from .toll import (
    TollAdminRequired,
    TollBackendError,
    TollRecord,
    TollUnpaid,
    _ADMIN_MESSAGE,
    _ADMIN_NEXT_STEP,
    _default_backend,
    require_paid_toll,
)
from .wua import WuaUnavailable, search_driver_updates
from .wua_install import (
    WuaInstallUnavailable,
    install_update,
    update_is_downloaded,
    update_requires_eula,
)

log = logging.getLogger("charon.apply")

# ----------------------------------------------------------------------
# Result vocabulary (the apply ledger's)
# ----------------------------------------------------------------------

RESULT_SUCCEEDED = "succeeded"
RESULT_SUCCEEDED_WITH_ERRORS = "succeeded_with_errors"
RESULT_FAILED = "failed"
RESULT_ABORTED = "aborted"
RESULT_SKIPPED = "skipped"
RESULT_EULA_NOT_ACCEPTED = "eula_not_accepted"

#: Statuses that mean the driver genuinely crossed.
APPLIED_STATUSES = frozenset({RESULT_SUCCEEDED, RESULT_SUCCEEDED_WITH_ERRORS})

APPLY_STATUSES = APPLIED_STATUSES | {
    RESULT_FAILED,
    RESULT_ABORTED,
    RESULT_SKIPPED,
    RESULT_EULA_NOT_ACCEPTED,
    RESULT_NO_LONGER_AVAILABLE,
}

# ----------------------------------------------------------------------
# Progress events (Phase 5's animated UI consumes these)
# ----------------------------------------------------------------------

EVENT_CROSSING_STARTED = "crossing_started"
EVENT_CROSSING_COMPLETED = "crossing_completed"
EVENT_APPLY_HELD = "apply_held"
EVENT_REBOOT_REQUIRED = "reboot_required"
EVENT_APPLY_COMPLETED = "apply_completed"

EmitFn = Callable[[ProgressEvent], None]

# A device check: given the group's staged entries and the update
# that just crossed, answer (healthy, note). Injected so tests
# never touch a real inventory; the default is below.
VerifyFn = Callable[["list[StagedEntry]", UpdateCandidate], "tuple[bool, str]"]


def _noop_emit(event: ProgressEvent) -> None:
    pass


# ----------------------------------------------------------------------
# The staged set (from the ledger — the only source of candidates)
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class StagedEntry:
    """One staged driver, as the download ledger recorded it."""

    device_id: str
    device_name: str
    update_title: str
    update_version: str
    eula_accepted: bool = False

    @property
    def identity(self) -> str:
        """Stable identity across download and apply records."""
        device = (self.device_id or self.device_name).strip().lower()
        return (
            f"{device}|{self.update_title.strip().lower()}|"
            f"{self.update_version or ''}"
        )

    @property
    def group_key(self) -> str:
        """Updates are crossed once per distinct update, however
        many devices ride on the same driver package."""
        return (
            f"{self.update_title.strip().lower()}|{self.update_version or ''}"
        )


def _staged_from_row(row) -> StagedEntry | None:
    if not isinstance(row, dict):
        return None
    # A non-string result (a hand-edited or corrupted ledger) is
    # simply not a staged row — and an unhashable one must not be
    # allowed to crash the membership test itself.
    if not isinstance(row.get("result"), str):
        return None
    if row.get("result") not in STAGED_STATUSES:
        return None
    return StagedEntry(
        device_id=str(row.get("device_id") or ""),
        device_name=str(row.get("device_name") or ""),
        update_title=str(row.get("update_title") or ""),
        update_version=str(row.get("update_version") or ""),
        # Only a real JSON true counts as acceptance — a string
        # like "false" must never read as accepted paperwork.
        eula_accepted=row.get("eula_accepted") is True,
    )


def load_staged_set(ledger: dict) -> list[StagedEntry]:
    """What Phase 2 staged and no crossing has applied yet.

    Every download run in the ledger contributes its staged rows
    (later runs refresh earlier ones for the same driver), and
    every apply run's successful crossings are subtracted — the
    ferry never carries the same passenger twice.
    """
    staged: dict[str, StagedEntry] = {}
    runs = ledger.get("runs")
    if isinstance(runs, list):
        for run in runs:
            if not isinstance(run, dict):
                continue
            entries = run.get("entries")
            if not isinstance(entries, list):
                continue
            for row in entries:
                entry = _staged_from_row(row)
                if entry is not None:
                    staged[entry.identity] = entry
    applied: set[str] = set()
    apply_runs = ledger.get("apply_runs")
    if isinstance(apply_runs, list):
        for run in apply_runs:
            if not isinstance(run, dict):
                continue
            entries = run.get("entries")
            if not isinstance(entries, list):
                continue
            for row in entries:
                if not isinstance(row, dict):
                    continue
                if not isinstance(row.get("result"), str):
                    continue
                if row.get("result") not in APPLIED_STATUSES:
                    continue
                probe = StagedEntry(
                    device_id=str(row.get("device_id") or ""),
                    device_name=str(row.get("device_name") or ""),
                    update_title=str(row.get("update_title") or ""),
                    update_version=str(row.get("update_version") or ""),
                )
                applied.add(probe.identity)
    return [e for key, e in staged.items() if key not in applied]


# ----------------------------------------------------------------------
# Results
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ApplyResult:
    """The final word on one staged driver for this crossing."""

    entry: StagedEntry
    status: str  # an APPLY_STATUSES member
    detail: str = ""
    reboot_required: bool = False
    result_code: int | None = None
    hresult: int | None = None

    @property
    def applied(self) -> bool:
        return self.status in APPLIED_STATUSES


# ----------------------------------------------------------------------
# The device check (default verification after a crossing)
# ----------------------------------------------------------------------


def _default_verify(searcher, runner) -> VerifyFn:
    """Build the standard post-crossing device check.

    Healthy means all three: Windows Update no longer offers the
    update as not-installed (the install landed), the device still
    appears in the PC's driver list (it didn't vanish), and — where
    versions can be compared — it reports the new version. Anything
    Charon cannot confirm counts as NOT healthy: after a crossing,
    an unanswered question stops the line just as a bad answer does.
    """

    def verify(entries: list[StagedEntry], candidate: UpdateCandidate):
        first = entries[0]
        try:
            fresh_raw = search_driver_updates(searcher, keep_objects=False)
        except WuaUnavailable:
            return (
                False,
                "Charon couldn't ask Windows Update to confirm the "
                "install.",
            )
        plan_view = UpdateCandidate(
            title=candidate.title, version=candidate.version
        )
        for raw in fresh_raw:
            if _same_update(plan_view, normalize_update(raw)):
                return (
                    False,
                    "Windows Update still offers this driver as not "
                    "installed, so the install did not land.",
                )
        try:
            collected = collect_inventory(runner=runner)
        except InventoryUnavailable:
            return (
                False,
                "Charon couldn't read this PC's driver list to "
                "confirm the device is healthy.",
            )
        drivers = merge_inventories(
            collected.cim_entries, collected.pnputil_entries
        )
        device = None
        for driver in drivers:
            if (
                first.device_id
                and driver.device_id
                and first.device_id.lower() == driver.device_id.lower()
            ):
                device = driver
                break
            if driver.device_name == first.device_name:
                device = driver
        if device is None:
            return (
                False,
                f"the {first.device_name} no longer appears in this "
                f"PC's driver list.",
            )
        if candidate.version and device.version:
            order = compare_versions(device.version, candidate.version)
            if order is not None and order < 0:
                return (
                    False,
                    f"the {first.device_name} still reports the "
                    f"older driver version {device.version}.",
                )
        return True, ""

    return verify


# ----------------------------------------------------------------------
# The crossing
# ----------------------------------------------------------------------


def apply_staged(
    ledger_path: Path | None = None,
    *,
    toll_backend=None,
    searcher=None,
    installer=None,
    verify: VerifyFn | None = None,
    inventory_runner=None,
    emit: EmitFn | None = None,
    record: TollRecord | None = None,
) -> tuple[list[ApplyResult], TollRecord | None]:
    """Apply everything Phase 2 staged, behind the toll gate.

    Returns ``(results, toll_record)`` — one result per staged
    driver, in manifest order, and the toll the gate verified.
    ``results`` is empty when nothing is staged. Raises the
    designed failures: :class:`TollUnpaid` (the gate — including
    :class:`TollAdminRequired`), :class:`WuaUnavailable` (the
    fresh search couldn't run), :class:`WuaInstallUnavailable`
    (an install couldn't begin; the partial truth is already in
    the ledger by then).
    """
    emit = emit or _noop_emit
    path = Path(ledger_path) if ledger_path else default_ledger_path()
    started = _utc_now()
    staged = load_staged_set(load_ledger(path))
    if not staged:
        return [], None

    backend = toll_backend if toll_backend is not None else _default_backend()

    # Precondition: administrator rights (the toll seam's probe is
    # the package's one elevation check).
    try:
        probe = backend.probe()
    except TollBackendError as exc:
        raise TollUnpaid(
            "Charon couldn't check this PC's settings, so the "
            "crossing stays closed.",
            detail=exc.detail,
        ) from exc
    if not probe.is_admin:
        raise TollAdminRequired(
            _ADMIN_MESSAGE,
            detail="backend probe reports the process is not elevated",
            next_step=_ADMIN_NEXT_STEP,
        )

    # Revalidation: a fresh search, run moments before the crossing.
    fresh_raw = search_driver_updates(searcher, keep_objects=True)
    fresh: list[tuple[UpdateCandidate, Any]] = [
        (normalize_update(raw), raw.get("com_update")) for raw in fresh_raw
    ]

    # THE GATE — fresh, here, with installs about to begin. The
    # record from an earlier step is never taken on trust: the gate
    # enumerates this PC's restore points now and the toll must
    # still prove out, or the ferry doesn't move.
    toll_record = require_paid_toll(
        backend, ledger_path=path, record=record
    )

    if verify is None:
        verify = _default_verify(searcher, inventory_runner)

    # Group by update: one crossing per distinct update, however
    # many devices ride on the same package (mirroring Phase 2's
    # documented shared-download design).
    groups: list[tuple[str, list[StagedEntry]]] = []
    by_key: dict[str, list[StagedEntry]] = {}
    for entry in staged:
        bucket = by_key.setdefault(entry.group_key, [])
        if not bucket:
            groups.append((entry.group_key, bucket))
        bucket.append(entry)

    results: dict[str, ApplyResult] = {}
    held_from: str | None = None  # device whose crossing stopped the line

    def record_group(entries, status, detail="", **fields):
        for entry in entries:
            results[entry.identity] = ApplyResult(
                entry=entry, status=status, detail=detail, **fields
            )

    try:
        for _key, entries in groups:
            first = entries[0]
            if held_from is not None:
                record_group(
                    entries,
                    RESULT_SKIPPED,
                    detail=(
                        f"Held back: the crossing stopped at the "
                        f"{held_from} driver, so this one was not "
                        f"attempted. Nothing is wrong with this "
                        f"driver — Charon stops the line when a "
                        f"crossing fails, and the way back is open."
                    ),
                )
                continue

            plan_view = UpdateCandidate(
                title=first.update_title, version=first.update_version
            )
            hit = next(
                (
                    (cand, obj)
                    for cand, obj in fresh
                    if _same_update(plan_view, cand)
                ),
                None,
            )
            if hit is None:
                record_group(
                    entries,
                    RESULT_NO_LONGER_AVAILABLE,
                    detail=(
                        f"Windows Update no longer offers version "
                        f"{first.update_version or 'unknown'} of the "
                        f"{first.device_name} driver. It may have "
                        f"been withdrawn or replaced since it was "
                        f"downloaded. Charon installed nothing in "
                        f"its place."
                    ),
                )
                continue
            candidate, com_update = hit
            if com_update is None:
                record_group(
                    entries,
                    RESULT_FAILED,
                    detail=(
                        "Windows Update offered this driver but gave "
                        "Charon nothing to install for it."
                    ),
                )
                held_from = first.device_name
                emit(
                    ProgressEvent(
                        kind=EVENT_APPLY_HELD,
                        device_name=first.device_name,
                        title=first.update_title,
                        status="held",
                        message=(
                            "The ferry is holding position — the "
                            "remaining drivers were not attempted."
                        ),
                    )
                )
                continue

            # Paperwork first: terms the download step did not
            # record as accepted are never installed behind.
            if update_requires_eula(com_update) and not any(
                e.eula_accepted for e in entries
            ):
                record_group(
                    entries,
                    RESULT_EULA_NOT_ACCEPTED,
                    detail=(
                        "This driver's license terms were not "
                        "accepted when it was downloaded, so it was "
                        "not installed. Run the download step again "
                        "so the terms can be accepted and recorded, "
                        "then cross."
                    ),
                )
                continue

            if not update_is_downloaded(com_update):
                record_group(
                    entries,
                    RESULT_FAILED,
                    detail=(
                        "The downloaded copy of this driver is no "
                        "longer in the Windows Update cache, so "
                        "there is nothing staged to install. Run "
                        "the download step again, then cross."
                    ),
                )
                continue

            emit(
                ProgressEvent(
                    kind=EVENT_CROSSING_STARTED,
                    device_name=first.device_name,
                    title=first.update_title,
                )
            )
            outcome = install_update(
                com_update, installer=installer, emit=emit
            )
            fields = {
                "reboot_required": outcome.reboot_required,
                "result_code": outcome.result_code,
                "hresult": outcome.hresult,
            }
            if outcome.status in APPLIED_STATUSES:
                healthy, note = verify(entries, candidate)
                if healthy:
                    record_group(
                        entries, outcome.status, outcome.detail, **fields
                    )
                else:
                    record_group(
                        entries,
                        RESULT_FAILED,
                        detail=(
                            "The driver was installed, but the "
                            f"device check failed — {note} Charon "
                            "stopped the line here; the restore "
                            "point from the toll is the way back."
                        ),
                        **fields,
                    )
                    held_from = first.device_name
            else:
                record_group(
                    entries, outcome.status, outcome.detail, **fields
                )
                held_from = first.device_name
            done = results[first.identity]
            emit(
                ProgressEvent(
                    kind=EVENT_CROSSING_COMPLETED,
                    device_name=first.device_name,
                    title=first.update_title,
                    status=done.status,
                    message=done.detail,
                )
            )
            if held_from == first.device_name:
                emit(
                    ProgressEvent(
                        kind=EVENT_APPLY_HELD,
                        device_name=first.device_name,
                        title=first.update_title,
                        status="held",
                        message=(
                            "The ferry is holding position — the "
                            "remaining drivers were not attempted."
                        ),
                    )
                )
    except WuaInstallUnavailable:
        # An install that couldn't begin: the crossings already
        # made still get their ledger record before the designed
        # failure reaches the user.
        for _key, entries in groups:
            for entry in entries:
                if entry.identity not in results:
                    results[entry.identity] = ApplyResult(
                        entry=entry,
                        status=RESULT_FAILED,
                        detail=(
                            "Windows Update couldn't start this "
                            "install, so the driver was not crossed."
                        ),
                    )
        append_apply_run_to_ledger(
            path,
            [results[e.identity] for e in staged],
            toll_record=toll_record,
            started_at=started,
            finished_at=_utc_now(),
        )
        raise

    ordered = [results[e.identity] for e in staged]
    append_apply_run_to_ledger(
        path,
        ordered,
        toll_record=toll_record,
        started_at=started,
        finished_at=_utc_now(),
    )
    reboot_count = sum(1 for r in ordered if r.reboot_required)
    if reboot_count:
        emit(
            ProgressEvent(
                kind=EVENT_REBOOT_REQUIRED,
                status="reboot_required",
                message=(
                    f"{reboot_count} driver"
                    f"{'s' if reboot_count != 1 else ''} need"
                    f"{'s' if reboot_count == 1 else ''} a restart "
                    f"to finish. Restart your PC when it suits "
                    f"you — Charon never restarts it for you."
                ),
            )
        )
    applied = sum(1 for r in ordered if r.applied)
    emit(
        ProgressEvent(
            kind=EVENT_APPLY_COMPLETED,
            status="complete",
            message=(
                f"{applied} of {len(ordered)} drivers crossed"
                + (
                    f"; the line stopped at the {held_from} driver"
                    if held_from
                    else ""
                )
                + "."
            ),
        )
    )
    return ordered, toll_record


# ----------------------------------------------------------------------
# The apply ledger (same file, same locked writer)
# ----------------------------------------------------------------------


def _apply_row(result: ApplyResult, moment: datetime) -> dict:
    return {
        "device_id": result.entry.device_id,
        "device_name": result.entry.device_name,
        "update_title": result.entry.update_title,
        "update_version": result.entry.update_version,
        "result": result.status,
        "reboot_required": result.reboot_required,
        "result_code": result.result_code,
        "hresult": result.hresult,
        "applied_at": _iso(moment) if result.applied else None,
        "detail": result.detail,
    }


def append_apply_run_to_ledger(
    path: Path,
    results: list[ApplyResult],
    *,
    toll_record: TollRecord | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> dict:
    """Append one apply run to the ledger, atomically, under the
    same OS-level lock the download and toll writers hold — an
    interleaved record is never erased by this rewrite."""
    finished = finished_at or _utc_now()
    started = started_at or finished
    run = {
        "started_at": _iso(started),
        "finished_at": _iso(finished),
        "source": "apply",
        "toll_sequence": (
            toll_record.sequence_number if toll_record else None
        ),
        "counts": {
            "planned": len(results),
            "applied": sum(1 for r in results if r.applied),
            "failed": sum(
                1
                for r in results
                if r.status in (RESULT_FAILED, RESULT_ABORTED)
            ),
            "skipped": sum(
                1 for r in results if r.status == RESULT_SKIPPED
            ),
            "no_longer_available": sum(
                1
                for r in results
                if r.status == RESULT_NO_LONGER_AVAILABLE
            ),
            "eula_not_accepted": sum(
                1
                for r in results
                if r.status == RESULT_EULA_NOT_ACCEPTED
            ),
            "reboot_required": sum(
                1 for r in results if r.reboot_required
            ),
        },
        "entries": [_apply_row(r, finished) for r in results],
    }
    path = Path(path)
    with _ledger_lock(path):
        ledger = load_ledger(path)
        ledger.setdefault("format", LEDGER_FORMAT_VERSION)
        apply_runs = ledger.get("apply_runs")
        if not isinstance(apply_runs, list):
            apply_runs = []
            ledger["apply_runs"] = apply_runs
        apply_runs.append(run)
        _atomic_write_ledger(path, ledger)
    return run


# ----------------------------------------------------------------------
# The honest summary
# ----------------------------------------------------------------------


def summarize_apply_results(
    results: list[ApplyResult],
    toll_record: TollRecord | None = None,
) -> str:
    """One honest sentence-set about a finished crossing."""
    if not results:
        return (
            "Nothing was staged to apply. Run the download step "
            "first — the ferry only carries what's aboard."
        )
    total = len(results)
    applied = [r for r in results if r.applied]
    lines: list[str] = []
    if len(applied) == total:
        lines.append(
            f"All {total} driver{'s' if total != 1 else ''} crossed "
            f"safely — the new drivers are installed."
        )
    else:
        parts = []
        for r in results:
            if r.applied:
                continue
            if r.status == RESULT_NO_LONGER_AVAILABLE:
                parts.append(
                    f"the {r.entry.device_name} driver is no "
                    f"longer offered by Windows Update"
                )
            elif r.status == RESULT_EULA_NOT_ACCEPTED:
                parts.append(
                    f"the {r.entry.device_name} driver's license "
                    f"terms weren't accepted at download time"
                )
            elif r.status == RESULT_SKIPPED:
                parts.append(
                    f"the {r.entry.device_name} driver was held "
                    f"back when the line stopped"
                )
            else:
                parts.append(
                    f"the {r.entry.device_name} driver didn't cross"
                )
        lines.append(
            f"{len(applied)} of {total} drivers crossed; "
            + "; ".join(parts)
            + "."
        )
    needs_reboot = [r for r in results if r.reboot_required]
    if needs_reboot:
        names = ", ".join(r.entry.device_name for r in needs_reboot)
        lines.append(
            f"A restart is needed to finish: {names}. Restart "
            f"your PC when it suits you — Charon never restarts "
            f"it for you."
        )
    failed = [r for r in results if r.status in (RESULT_FAILED, RESULT_ABORTED)]
    if failed and toll_record is not None:
        lines.append(
            f"The way back is open: restore point "
            f"#{toll_record.sequence_number} was created and "
            f"verified before this crossing. If a crossed driver "
            f"misbehaves, open System Restore and choose that "
            f"point to undo the changes."
        )
    return " ".join(lines)
