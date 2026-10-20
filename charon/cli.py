"""CHARON.EXE — command line.

``python -m charon scan`` prints the passenger manifest: every
installed driver, and — where Windows Update has a say — whether a
newer one waits on the other side. ``--json <path>`` exports the full
report. Scan is read-only.

``python -m charon download`` runs Phase 2 of the Crossing: a fresh
scan (or ``--from-report`` with an exported one), a download plan, a
revalidation against a search run moments before the fetch, then the
staging itself — into the Windows Update cache, recorded in
Charon's download ledger. **Nothing is installed.**

``python -m charon toll`` runs Phase 3 of the Crossing: it creates
a System Restore point and **verifies** it exists — Windows
silently skips a requested point when a recent one already exists,
so a requested toll is not a paid toll. The verified toll is
written to the same ledger, and the Phase 4 apply step will be
required to present it at the gate before anything crosses.

Every failure a user can hit is a designed state with a
plain-language message; technical detail goes to the log
(``--debug``), never to a traceback on screen.
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .download import (
    EVENT_BATCH_COMPLETED,
    EVENT_UPDATE_COMPLETED,
    EVENT_UPDATE_PROGRESS,
    EVENT_UPDATE_STARTED,
    ProgressEvent,
    StagedResult,
    append_run_to_ledger,
    build_download_plan,
    default_ledger_path,
    revalidate_plan,
    stage_plan,
    summarize_results,
)
from .inventory import merge_inventories, normalize_update
from .matching import match_devices
from .model import (
    STATUS_UNKNOWN,
    STATUS_UPDATE_AVAILABLE,
    ScanReport,
    format_date,
)
from .platform_inventory import InventoryUnavailable, collect_inventory
from .toll import (
    EVENT_TOLL_CHECKING,
    EVENT_TOLL_CREATING,
    EVENT_TOLL_VERIFYING,
    TollUnpaid,
    pay_toll,
)
from .wua import WuaUnavailable, search_driver_updates
from .wua_download import WuaDownloadUnavailable, download_updates

log = logging.getLogger("charon.cli")

EXIT_OK = 0
EXIT_DESIGNED_FAILURE = 2

_STATUS_LABEL = {
    STATUS_UPDATE_AVAILABLE: "UPDATE AVAILABLE",
    "up_to_date": "UP TO DATE",
    STATUS_UNKNOWN: "NO WORD FROM WINDOWS UPDATE",
}


def build_report(
    cim_entries: list[dict],
    pnputil_entries: list[dict],
    raw_updates: list[dict] | None,
    *,
    computer: str = "",
    sources: list[str] | None = None,
    warnings: list[str] | None = None,
) -> ScanReport:
    """Pure assembly: raw sources → classified report.

    ``raw_updates=None`` means Windows Update could not be asked; the
    manifest still builds and every device classifies ``unknown``.
    """
    drivers = merge_inventories(cim_entries, pnputil_entries)
    updates = [normalize_update(u) for u in (raw_updates or [])]
    devices = match_devices(drivers, updates)
    return ScanReport(
        generated_at=datetime.now().astimezone(),
        computer=computer,
        sources=list(sources or []),
        warnings=list(warnings or []),
        devices=devices,
    )


def run_scan(runner=None, searcher=None) -> ScanReport:
    """Collect → normalize → match. Raises designed-failure errors."""
    result = collect_inventory(runner=runner)
    warnings: list[str] = []
    for err in result.errors:
        warnings.append(f"One information source didn't answer ({err}).")
    try:
        raw_updates = search_driver_updates(searcher)
    except WuaUnavailable as exc:
        log.warning("WUA unavailable: %s", exc.detail or exc)
        warnings.append(
            "Windows Update didn't answer, so nothing here is marked "
            "outdated — Charon never guesses."
        )
        raw_updates = None
    return build_report(
        result.cim_entries,
        result.pnputil_entries,
        raw_updates,
        computer=socket.gethostname(),
        sources=result.sources
        + (["Windows Update driver search"] if raw_updates is not None else []),
        warnings=warnings,
    )


def render_report(report: ScanReport) -> str:
    """The readable table a human gets on screen."""
    lines: list[str] = []
    lines.append("CHARON.EXE — driver scan (Phase 1: scan only, nothing changes)")
    if report.computer:
        lines.append(f"PC: {report.computer}")
    lines.append(f"When: {report.generated_at.strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"Sources: {', '.join(report.sources) or 'none'}")
    lines.append("")
    lines.append(report.summary_line())
    for warning in report.warnings:
        lines.append(f"Note: {warning}")
    lines.append("")

    name_w = max([len("DEVICE")] + [len(d.driver.device_name) for d in report.devices])
    ver_w = max([len("INSTALLED")] + [len(d.driver.version or "—") for d in report.devices])
    header = (
        f"{'DEVICE':<{name_w}}  {'CLASS':<10}  {'INSTALLED':<{ver_w}}  "
        f"{'STATUS':<28}  AVAILABLE"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for d in report.devices:
        available = "—"
        if d.update and d.status == STATUS_UPDATE_AVAILABLE:
            available = d.update.version or (
                f"dated {format_date(d.update.driver_date)}"
            )
        lines.append(
            f"{d.driver.device_name:<{name_w}}  "
            f"{(d.driver.device_class or '—'):<10}  "
            f"{(d.driver.version or '—'):<{ver_w}}  "
            f"{_STATUS_LABEL.get(d.status, d.status):<28}  {available}"
        )

    attention = [
        d for d in report.devices if d.status == STATUS_UPDATE_AVAILABLE
    ]
    if attention:
        lines.append("")
        lines.append("What this means:")
        for d in attention:
            lines.append(f"- {d.explanation}")
    lines.append("")
    lines.append(
        "Charon only looked. Nothing was downloaded, installed, or "
        "changed — the crossing comes later, and the toll is paid first."
    )
    return "\n".join(lines)


def _scan_command(args) -> int:
    try:
        report = run_scan()
    except InventoryUnavailable as exc:
        log.error("inventory unavailable: %s", exc.detail or exc)
        print(exc.user_message)
        return EXIT_DESIGNED_FAILURE

    print(render_report(report))
    if args.json:
        path = Path(args.json)
        try:
            path.write_text(
                json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            log.error("report export failed: %s", exc)
            print(
                f"\nThe scan worked, but Charon couldn't write the "
                f"report file to {path}. Check the path is right "
                f"and the folder is writable, then run the scan again."
            )
            return EXIT_DESIGNED_FAILURE
        print(f"\nFull report written to {path}")
    return EXIT_OK


# ----------------------------------------------------------------------
# Phase 2 — download staging
# ----------------------------------------------------------------------


def load_report(path) -> ScanReport:
    """Load an exported scan report for ``download --from-report``."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InventoryUnavailable(
            "Charon couldn't read that scan report file. Export a "
            "fresh one with: python -m charon scan --json <file>",
            detail=f"{type(exc).__name__}: {exc}",
        ) from exc
    if not isinstance(data, dict) or not isinstance(
        data.get("devices"), list
    ):
        raise InventoryUnavailable(
            "That file doesn't look like a Charon scan report. "
            "Export one with: python -m charon scan --json <file>",
            detail="missing or non-list 'devices' in report JSON",
        )
    return ScanReport.from_dict(data)


def run_download(
    report: ScanReport,
    selection: list[str] | None = None,
    *,
    searcher=None,
    downloader=None,
    emit=None,
    ledger_path=None,
    source: str = "scan",
) -> tuple[list[StagedResult], Path]:
    """Plan → revalidate against a fresh search → stage → ledger.

    Raises the designed-failure errors (``WuaUnavailable`` /
    ``WuaDownloadUnavailable``) when Windows Update can't be reached;
    per-driver failures come back as results, never as exceptions.
    """
    started = datetime.now(timezone.utc)
    plan = build_download_plan(report, selection)
    if plan.empty:
        return [], Path(ledger_path) if ledger_path else default_ledger_path()

    fresh_raw = search_driver_updates(searcher, keep_objects=True)
    resolved = revalidate_plan(plan, fresh_raw)

    def fetch(objects, emit=None):
        return download_updates(objects, downloader=downloader, emit=emit)

    results = stage_plan(resolved, fetch, emit=emit)
    path = Path(ledger_path) if ledger_path else default_ledger_path()
    append_run_to_ledger(
        path,
        results,
        source=source,
        started_at=started,
        finished_at=datetime.now(timezone.utc),
    )
    return results, path


def _render_event(event: ProgressEvent) -> str | None:
    """One transcript line per event; progress notes stay quiet
    unless Windows Update actually reports a percentage."""
    if event.kind == EVENT_UPDATE_STARTED:
        return f"  Loading aboard: {event.device_name} — {event.title}"
    if event.kind == EVENT_UPDATE_PROGRESS:
        if event.percent is not None:
            return f"    …{event.title}: {event.percent}%"
        return None
    if event.kind == EVENT_UPDATE_COMPLETED:
        if event.status in ("succeeded", "succeeded_with_errors"):
            extra = (
                " (staged, with warnings from Windows Update)"
                if event.status == "succeeded_with_errors"
                else ""
            )
            return f"  Aboard: {event.device_name}{extra}"
        return f"  Not aboard: {event.device_name} — {event.message}"
    if event.kind == EVENT_BATCH_COMPLETED:
        return None  # the summary block speaks for the batch
    return None


def _download_command(args) -> int:
    if args.from_report:
        try:
            report = load_report(args.from_report)
        except InventoryUnavailable as exc:
            log.error("report load failed: %s", exc.detail or exc)
            print(exc.user_message)
            return EXIT_DESIGNED_FAILURE
        source = "report"
        print(
            "CHARON.EXE — download staging (Phase 2: nothing is installed)"
        )
        print(
            f"Scan report from {report.generated_at.strftime('%Y-%m-%d %H:%M')} "
            f"({report.computer or 'unknown PC'}): {report.summary_line()}"
        )
    else:
        try:
            report = run_scan()
        except InventoryUnavailable as exc:
            log.error("inventory unavailable: %s", exc.detail or exc)
            print(exc.user_message)
            return EXIT_DESIGNED_FAILURE
        source = "scan"
        print(
            "CHARON.EXE — download staging (Phase 2: nothing is installed)"
        )
        print(f"Fresh scan: {report.summary_line()}")

    plan = build_download_plan(report, args.only)
    for token in plan.not_found:
        print(f"Note: no outdated driver matched '{token}' — skipped.")
    if plan.empty:
        if args.only:
            print(
                "Nothing to download: none of the selected drivers "
                "has an update waiting."
            )
        else:
            print(
                "Nothing to download: Windows Update offers no newer "
                "drivers for this PC right now."
            )
        return EXIT_OK

    print(f"Download plan: {len(plan.entries)} driver(s) selected.")
    for entry in plan.entries:
        print(
            f"  - {entry.device_name}: version "
            f"{entry.update.version or 'unknown'}"
        )
    print("Checking with Windows Update once more before loading…")

    def emit(event: ProgressEvent) -> None:
        line = _render_event(event)
        if line:
            print(line)

    try:
        results, ledger = run_download(
            report,
            args.only,
            emit=emit,
            ledger_path=args.ledger,
            source=source,
        )
    except WuaUnavailable as exc:
        log.error("WUA unavailable at download time: %s", exc.detail or exc)
        print(
            "Windows Update didn't answer when Charon checked again, "
            "so nothing was downloaded. A download is never staged "
            "from a stale scan — try again in a few minutes."
        )
        return EXIT_DESIGNED_FAILURE
    except WuaDownloadUnavailable as exc:
        log.error("download unavailable: %s", exc.detail or exc)
        print(exc.user_message)
        return EXIT_DESIGNED_FAILURE
    except OSError as exc:
        # The remaining way a run can fail in the open is writing
        # its records (the ledger) — a full disk or a folder Charon
        # can't write to. Designed failure, not a traceback; and
        # still nothing was installed.
        log.error("download run records failed: %s", exc)
        print(
            "Charon couldn't finish writing its download records, "
            "so this run counts as not completed. Nothing was "
            "installed — check the disk has room and try again."
        )
        return EXIT_DESIGNED_FAILURE

    print("")
    print(summarize_results(results))
    for result in results:
        if result.status == "no_longer_available":
            print(f"Note: {result.detail}")
    print(f"Download ledger updated: {ledger}")
    return EXIT_OK


# ----------------------------------------------------------------------
# Phase 3 — the toll
# ----------------------------------------------------------------------


def _render_toll_event(event: ProgressEvent) -> str | None:
    """One transcript line per toll beat; the paid/unpaid verdicts
    speak for themselves in the receipt and the refusal."""
    if event.kind in (
        EVENT_TOLL_CHECKING,
        EVENT_TOLL_CREATING,
        EVENT_TOLL_VERIFYING,
    ):
        return event.message or None
    return None


def _toll_command(args) -> int:
    print(
        "CHARON.EXE — the toll (Phase 3: a restore point, created "
        "and verified)"
    )

    def emit(event: ProgressEvent) -> None:
        line = _render_toll_event(event)
        if line:
            print(line)

    try:
        record = pay_toll(emit=emit, ledger_path=args.ledger)
    except TollUnpaid as exc:
        log.error("toll unpaid: %s", exc.detail or exc.user_message)
        print(exc.user_message)
        if exc.next_step:
            print(exc.next_step)
        return EXIT_DESIGNED_FAILURE

    created_local = record.created_at.astimezone().strftime(
        "%Y-%m-%d %H:%M %Z"
    )
    print(
        f"Toll paid — restore point #{record.sequence_number} "
        f"created at {created_local}, verified ✓"
    )
    path = Path(args.ledger) if args.ledger else default_ledger_path()
    print(f"Recorded in the ledger: {path}")
    print(
        "The ferry may cross — nothing has been applied; the apply "
        "step only ever runs behind this verified toll."
    )
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="charon",
        description="CHARON.EXE — Sentinel Safeguard driver updater. "
        "Scan, download (staged only — nothing is installed), and "
        "the toll: a restore point, created and verified. Nobody "
        "crosses without paying it.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--debug",
        action="store_true",
        help="show technical detail in the log output",
    )
    sub = parser.add_subparsers(dest="command")
    scan = sub.add_parser(
        "scan", help="list installed drivers and flag outdated ones"
    )
    scan.add_argument(
        "--json",
        metavar="PATH",
        help="also write the full report as JSON to PATH",
    )
    download = sub.add_parser(
        "download",
        help="download outdated drivers and stage them (installs nothing)",
    )
    download.add_argument(
        "--from-report",
        metavar="PATH",
        help="stage from an exported scan report instead of scanning now "
        "(the plan is still re-checked against Windows Update first)",
    )
    download.add_argument(
        "--only",
        metavar="DEVICE",
        action="append",
        default=None,
        help="download only this device's driver (by device ID or name); "
        "repeat for several",
    )
    download.add_argument(
        "--ledger",
        metavar="PATH",
        help="download ledger location (default: Charon's app-state folder)",
    )
    toll = sub.add_parser(
        "toll",
        help="pay the toll: create a System Restore point and verify "
        "it exists (applies nothing)",
    )
    toll.add_argument(
        "--ledger",
        metavar="PATH",
        help="ledger location (default: Charon's app-state folder)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.command == "scan":
        return _scan_command(args)
    if args.command == "download":
        return _download_command(args)
    if args.command == "toll":
        return _toll_command(args)
    parser.print_help()
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
