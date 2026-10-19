"""CHARON.EXE — command line for the Phase 1 scan engine.

``python -m charon scan`` prints the passenger manifest: every
installed driver, and — where Windows Update has a say — whether a
newer one waits on the other side. ``--json <path>`` exports the full
report.

Phase 1 is read-only: this CLI cannot change a driver, and it does
not try. Every failure a user can hit is a designed state with a
plain-language message; technical detail goes to the log (``--debug``),
never to a traceback on screen.
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import sys
from datetime import datetime
from pathlib import Path

from . import __version__
from .inventory import merge_inventories, normalize_update
from .matching import match_devices
from .model import (
    STATUS_UNKNOWN,
    STATUS_UPDATE_AVAILABLE,
    ScanReport,
    format_date,
)
from .platform_inventory import InventoryUnavailable, collect_inventory
from .wua import WuaUnavailable, search_driver_updates

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
        path.write_text(
            json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8"
        )
        print(f"\nFull report written to {path}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="charon",
        description="CHARON.EXE — Sentinel Safeguard driver updater. "
        "Phase 1: scan only. Nobody crosses without paying the toll.",
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
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.command == "scan":
        return _scan_command(args)
    parser.print_help()
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
