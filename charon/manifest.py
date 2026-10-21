"""CHARON.EXE — the passenger manifest (Phase 6): a record to keep.

Phase 1 named the scan report the "passenger manifest" — the list
of every driver aboard. This module turns that list into the
keepable export the roadmap's backlog promised (B-5): one file the
owner can save, print, or hand to a technician, holding

- every driver the scan found, exactly as the inventory reported
  it (the manifest reuses the scan report's own serialization —
  it may not paraphrase the cargo), and
- the crossing record, when Charon's ledger has one: what the
  download step staged, the tolls that were paid (the restore
  points that guarded the crossings), and what the apply step
  carried across — the cargo record of what was carried.

Honesty rules, in the fleet's style:

- Counts are computed here, from the rows the manifest carries —
  a summary that disagrees with its own rows is a lie, so no
  caller ever supplies one.
- A scan that could not hear from every source yields a manifest
  marked ``complete: False``, with the scan's warnings carried
  verbatim. A crossing record that could not be read is flagged
  the same way. A partial manifest is never dressed as complete.
- The write is atomic (a unique temp sibling, then a replace), so
  a failed export leaves NO partial file at the destination —
  and never touches a good manifest already saved there. Every
  failure raises :class:`ManifestExportError`, a designed failure
  with a plain-language message; the technical detail goes to
  the log, never to the owner's screen.

This module owns no capability against the PC: it reads no
driver, asks Windows Update nothing, and touches no restore
point. It only arranges records Charon already made and writes
the one file the owner asked for.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .download import default_ledger_path
from .model import (
    STATUS_UNKNOWN,
    STATUS_UPDATE_AVAILABLE,
    ScanReport,
    format_date,
)

log = logging.getLogger("charon.manifest")

#: The manifest document's own format version. A reader that meets
#: a higher number than it knows should say so, not guess.
MANIFEST_FORMAT_VERSION = 1

#: The file name the window's save button uses, in the owner's
#: Documents folder — a record to keep belongs where the owner
#: keeps records, not in Charon's app-state folder with the ledger.
MANIFEST_FILENAME = "Charon-Passenger-Manifest.json"

_FORMAT_JSON = "json"
_FORMAT_TEXT = "text"

_STATUS_LABEL = {
    STATUS_UPDATE_AVAILABLE: "UPDATE AVAILABLE",
    "up_to_date": "UP TO DATE",
    STATUS_UNKNOWN: "NO WORD FROM WINDOWS UPDATE",
}


class ManifestExportError(Exception):
    """The manifest could not be written (designed failure).

    Carries a plain-language message safe to show a non-technical
    user; the technical detail belongs in the log.
    """

    def __init__(self, user_message: str, detail: str = ""):
        super().__init__(user_message)
        self.user_message = user_message
        self.detail = detail


def _iso(moment: datetime) -> str:
    return (
        moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    )


# ----------------------------------------------------------------------
# The crossing record (read from the ledger, never invented)
# ----------------------------------------------------------------------


def load_crossing_ledger(
    path: Path | None,
) -> tuple[dict | None, list[str], bool]:
    """Read the ledger for the crossing record.

    Returns ``(ledger_data, warnings, complete)``:

    - No path, or a path that doesn't exist yet: ``(None, [], True)``
      — no crossings recorded is a fact, not a gap.
    - A ledger that exists but can't be read or doesn't parse:
      ``(None, [warning], False)`` — the record may exist and be
      missing from the manifest, and the manifest must say so.
    """
    if path is None:
        return None, [], True
    path = Path(path)
    if not path.exists():
        return None, [], True
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("ledger at %s unreadable for manifest: %s", path, exc)
        return (
            None,
            [
                "Charon's ledger could not be read, so the crossing "
                "record in this manifest may be missing entries."
            ],
            False,
        )
    if not isinstance(data, dict):
        log.warning("ledger at %s has an unexpected shape", path)
        return (
            None,
            [
                "Charon's ledger could not be read, so the crossing "
                "record in this manifest may be missing entries."
            ],
            False,
        )
    return data, [], True


def _crossing_from_ledger(ledger: dict | None, complete: bool) -> dict:
    """Flatten the ledger's runs into the manifest's crossing record.

    Rows are carried as the ledger recorded them — per update,
    never as a blanket claim. Anything in the ledger that isn't a
    list of rows is ignored rather than guessed at.
    """
    if ledger is None:
        return {
            "recorded": False,
            "complete": complete,
            "counts": {
                "downloaded": 0,
                "tolls_paid": 0,
                "apply_runs": 0,
                "applied": 0,
            },
            "downloads": [],
            "tolls": [],
            "applies": [],
        }
    downloads: list[dict] = []
    runs = ledger.get("runs")
    if isinstance(runs, list):
        for run in runs:
            if not isinstance(run, dict):
                continue
            entries = run.get("entries")
            if not isinstance(entries, list):
                continue
            for row in entries:
                if isinstance(row, dict):
                    downloads.append(dict(row))
    tolls: list[dict] = []
    raw_tolls = ledger.get("tolls")
    if isinstance(raw_tolls, list):
        tolls = [dict(t) for t in raw_tolls if isinstance(t, dict)]
    applies: list[dict] = []
    apply_runs = ledger.get("apply_runs")
    run_count = 0
    if isinstance(apply_runs, list):
        run_count = len([r for r in apply_runs if isinstance(r, dict)])
        for run in apply_runs:
            if not isinstance(run, dict):
                continue
            entries = run.get("entries")
            if not isinstance(entries, list):
                continue
            for row in entries:
                if isinstance(row, dict):
                    applies.append(dict(row))
    return {
        "recorded": bool(downloads or tolls or applies),
        "complete": complete,
        "counts": {
            "downloaded": sum(
                1
                for d in downloads
                if d.get("result") in ("succeeded", "succeeded_with_errors")
            ),
            "tolls_paid": len(tolls),
            "apply_runs": run_count,
            "applied": sum(
                1
                for a in applies
                if a.get("result") in ("succeeded", "succeeded_with_errors")
            ),
        },
        "downloads": downloads,
        "tolls": tolls,
        "applies": applies,
    }


# ----------------------------------------------------------------------
# The manifest document
# ----------------------------------------------------------------------


@dataclass
class PassengerManifest:
    """The keepable record: one scan, plus its crossing record."""

    generated_at: datetime
    scan_generated_at: datetime
    computer: str = ""
    sources: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    complete: bool = True
    summary: dict = field(default_factory=dict)
    devices: list[dict] = field(default_factory=list)
    crossing: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "format": MANIFEST_FORMAT_VERSION,
            "kind": "charon-passenger-manifest",
            "generated_at": _iso(self.generated_at),
            "scan_generated_at": _iso(self.scan_generated_at),
            "computer": self.computer,
            "sources": list(self.sources),
            "warnings": list(self.warnings),
            "complete": self.complete,
            "summary": dict(self.summary),
            "devices": list(self.devices),
            "crossing": self.crossing,
        }


def build_manifest(
    report: ScanReport,
    ledger: dict | None = None,
    *,
    generated_at: datetime | None = None,
    ledger_warnings: list[str] | None = None,
    crossing_complete: bool = True,
) -> PassengerManifest:
    """Assemble the manifest from a scan report and (optionally)
    the ledger's data.

    The driver rows are the scan report's own serialization of
    itself — the manifest inherits the scan's accuracy instead of
    re-deriving it. The scan half is complete only when the scan
    raised no warnings: a source that didn't answer means some
    verdict in the manifest rests on less than the full picture,
    and the manifest says so on its face.
    """
    report_data = report.to_dict()
    warnings = list(report.warnings) + list(ledger_warnings or [])
    return PassengerManifest(
        generated_at=generated_at or datetime.now(timezone.utc),
        scan_generated_at=report.generated_at,
        computer=report.computer,
        sources=list(report.sources),
        warnings=warnings,
        complete=not report.warnings,
        summary=report_data["summary"],
        devices=report_data["devices"],
        crossing=_crossing_from_ledger(ledger, crossing_complete),
    )


# ----------------------------------------------------------------------
# The readable rendering (plain language, for keeping and printing)
# ----------------------------------------------------------------------


def render_manifest_text(manifest: PassengerManifest) -> str:
    """The manifest as a document a person can read and keep."""
    data = manifest.to_dict()
    summary = data["summary"]
    lines: list[str] = []
    lines.append("CHARON.EXE — passenger manifest")
    lines.append("Every driver on this PC, and what Charon carried across.")
    if data["computer"]:
        lines.append(f"PC: {data['computer']}")
    scan_when = manifest.scan_generated_at.astimezone().strftime(
        "%Y-%m-%d %H:%M"
    )
    lines.append(f"Scan taken: {scan_when}")
    lines.append(
        f"Manifest written: "
        f"{manifest.generated_at.astimezone().strftime('%Y-%m-%d %H:%M')}"
    )
    lines.append("")
    if not data["complete"]:
        lines.append(
            "THIS MANIFEST IS NOT COMPLETE. One or more information "
            "sources did not answer during the scan, so some driver "
            "verdicts below rest on less than the full picture:"
        )
        for warning in manifest.warnings:
            lines.append(f"  - {warning}")
        lines.append("")
    lines.append(
        f"{summary['total']} drivers checked: "
        f"{summary['updates_available']} with updates available, "
        f"{summary['up_to_date']} up to date, "
        f"{summary['unknown']} Windows Update had no word on."
    )
    if summary["unsigned"]:
        lines.append(
            f"{summary['unsigned']} driver"
            f"{'s are' if summary['unsigned'] != 1 else ' is'} "
            f"not signed."
        )
    lines.append("")
    lines.append("The drivers:")
    for row in data["devices"]:
        driver = row["driver"]
        status = _STATUS_LABEL.get(row["status"], row["status"])
        signed = driver.get("is_signed")
        signed_text = (
            "signed" if signed is True else "not signed" if signed is False
            else "signer unknown"
        )
        line = (
            f"- {driver['device_name']} — version "
            f"{driver['version'] or 'unknown'}"
            f" (dated {format_date(_as_date(driver.get('driver_date')))}), "
            f"from {driver['provider'] or 'an unknown provider'}, "
            f"{signed_text}: {status}"
        )
        update = row.get("update")
        if update and row["status"] == STATUS_UPDATE_AVAILABLE:
            line += (
                f"; update offered: version "
                f"{update['version'] or 'unknown'}"
            )
        lines.append(line)

    lines.append("")
    lines.append("The crossing record:")
    crossing = data["crossing"]
    if not crossing["complete"]:
        lines.append(
            "  The crossing record below may be missing entries — "
            "Charon's ledger could not be read when this manifest "
            "was written."
        )
    if not crossing["recorded"]:
        lines.append(
            "  No crossings are recorded yet — Charon's ledger holds "
            "no downloads, tolls, or crossings for this PC so far."
        )
    else:
        counts = crossing["counts"]
        lines.append(
            f"  {counts['downloaded']} download record"
            f"{'s' if counts['downloaded'] != 1 else ''} marked staged, "
            f"{counts['tolls_paid']} toll"
            f"{'s' if counts['tolls_paid'] != 1 else ''} paid, "
            f"{counts['applied']} driver record"
            f"{'s' if counts['applied'] != 1 else ''} carried across."
        )
        for toll in crossing["tolls"]:
            lines.append(
                f"  - The toll: restore point "
                f"#{toll.get('sequence_number')} created "
                f"{toll.get('created_at') or 'at an unknown time'}, "
                f"verified before the crossing."
            )
        for entry in crossing["downloads"]:
            lines.append(
                f"  - Downloaded: {entry.get('device_name') or 'a driver'}"
                f" — {entry.get('update_title') or 'an update'}: "
                f"{entry.get('result') or 'no result recorded'}"
            )
        for entry in crossing["applies"]:
            lines.append(
                f"  - Crossed: {entry.get('device_name') or 'a driver'}"
                f" — {entry.get('update_title') or 'an update'}: "
                f"{entry.get('result') or 'no result recorded'}"
            )
    lines.append("")
    lines.append(
        "This manifest is a record only. Writing it downloaded, "
        "changed, and applied nothing — and the restore points it "
        "names are still the way back from their crossings."
    )
    return "\n".join(lines)


def _as_date(value):
    """A serialized driver date (ISO string) back to a date, for
    the readable rendering; anything unexpected reads as unknown."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


# ----------------------------------------------------------------------
# Writing the file (atomic; a failure is a designed failure)
# ----------------------------------------------------------------------


def default_manifest_path() -> Path:
    """Where the window saves the manifest: the owner's Documents."""
    return Path.home() / "Documents" / MANIFEST_FILENAME


def _render(manifest: PassengerManifest, fmt: str) -> str:
    if fmt == _FORMAT_JSON:
        return json.dumps(manifest.to_dict(), indent=2) + "\n"
    if fmt == _FORMAT_TEXT:
        return render_manifest_text(manifest) + "\n"
    raise ValueError(f"unknown manifest format: {fmt!r}")


def write_manifest(
    manifest: PassengerManifest, path, *, fmt: str = _FORMAT_JSON
) -> Path:
    """Write the manifest to ``path``, atomically.

    The text is staged in a unique temp sibling and moved over the
    destination with a replace, so the destination only ever holds
    a whole manifest or the one that was already there — never a
    partial file dressed as a finished one. Any failure raises
    :class:`ManifestExportError` and leaves no temp litter behind.
    """
    path = Path(path)
    text = _render(manifest, fmt)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(
            f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        )
        try:
            tmp.write_text(text, encoding="utf-8")
            last_exc: OSError | None = None
            for _attempt in range(5):
                try:
                    os.replace(tmp, path)
                    return path
                except OSError as exc:
                    last_exc = exc
                    time.sleep(0.05)
            raise last_exc  # type: ignore[misc]
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError as exc:
        raise ManifestExportError(
            "Charon couldn't write the passenger manifest to "
            f"{path}. Check the path is right and the folder is "
            "writable, then try again — no manifest was saved.",
            detail=f"{type(exc).__name__}: {exc}",
        ) from exc


def export_manifest(
    report: ScanReport,
    path,
    *,
    fmt: str = _FORMAT_JSON,
    ledger_path=None,
) -> PassengerManifest:
    """Build the manifest for ``report`` and write it to ``path``.

    The crossing record comes from the ledger at ``ledger_path``
    (Charon's default ledger when none is named). Returns the
    manifest that was written; raises :class:`ManifestExportError`
    when the file could not be written.
    """
    ledger_file = (
        Path(ledger_path) if ledger_path else default_ledger_path()
    )
    ledger, warnings, crossing_complete = load_crossing_ledger(ledger_file)
    manifest = build_manifest(
        report,
        ledger,
        ledger_warnings=warnings,
        crossing_complete=crossing_complete,
    )
    write_manifest(manifest, path, fmt=fmt)
    return manifest
