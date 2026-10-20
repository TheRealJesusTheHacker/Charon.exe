"""CHARON.EXE — Windows platform seam for the driver inventory.

This is the ONLY module in the package that talks to the operating
system about installed drivers, and it is strictly query-only:

- PowerShell ``Get-CimInstance Win32_PnPSignedDriver`` (read-only query)
- ``pnputil /enum-drivers`` (read-only enumeration, cross-check/fallback)

There is deliberately no code path here that adds, removes, stages, or
modifies a driver — Phase 1 only looks. The exact commands are pinned
by the test suite.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from dataclasses import dataclass, field

log = logging.getLogger("charon.platform_inventory")

# The one and only pnputil invocation this package will ever make in
# Phase 1: enumerate, nothing else.
PNPUTIL_ENUM_ARGS = ["pnputil", "/enum-drivers"]

# The one and only PowerShell query: a read-only CIM select piped to
# JSON. No cmdlets that change state appear in this script, by design
# and by test.
CIM_QUERY_SCRIPT = (
    "Get-CimInstance Win32_PnPSignedDriver | "
    "Select-Object DeviceName, DeviceClass, DriverProviderName, "
    "DriverVersion, DriverDate, InfName, DeviceID, HardWareID, IsSigned | "
    "ConvertTo-Json -Depth 4"
)

POWERSHELL_ARGS = [
    "powershell",
    "-NoProfile",
    "-NonInteractive",
    "-Command",
    CIM_QUERY_SCRIPT,
]


class InventoryUnavailable(Exception):
    """The inventory could not be collected at all (designed failure).

    Carries a plain-language message safe to show a non-technical user;
    the technical detail belongs in the log, not on screen.
    """

    def __init__(self, user_message: str, detail: str = ""):
        super().__init__(user_message)
        self.user_message = user_message
        self.detail = detail


@dataclass
class InventoryResult:
    """Raw (not yet normalized) inventory entries plus provenance."""

    cim_entries: list[dict] = field(default_factory=list)
    pnputil_entries: list[dict] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def is_windows() -> bool:
    return sys.platform.startswith("win")


class SubprocessRunner:
    """Default runner: executes the pinned read-only commands.

    Kept behind a tiny interface so tests can substitute a recording
    fake and pin exactly what Charon asks the OS to do.
    """

    def run(self, args: list[str]) -> str:
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
                args,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            # A missing tool or a hung query is this seam's designed
            # failure, same as the toll seam's runner converts it —
            # not a raw exception for callers to trip over.
            raise InventoryUnavailable(
                "Windows didn't answer the driver inventory query.",
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc
        if proc.returncode != 0:
            raise InventoryUnavailable(
                "Windows didn't answer the driver inventory query.",
                detail=f"{args[0]} exited {proc.returncode}: "
                f"{proc.stderr.strip()[:400]}",
            )
        return proc.stdout


# ----------------------------------------------------------------------
# Pure parsers (testable anywhere)
# ----------------------------------------------------------------------


def parse_cim_json(text: str) -> list[dict]:
    """Parse the ConvertTo-Json output of the CIM query.

    PowerShell emits a bare object (not an array) when exactly one
    driver exists; both shapes are accepted.
    """
    text = (text or "").strip()
    if not text:
        return []
    data = json.loads(text)
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    return []


_PNPUTIL_FIELD_MAP = {
    "published name": "inf_name",
    "original name": "original_name",
    "provider name": "provider",
    "class name": "device_class",
    "driver version": "version_and_date",
    "signer name": "signer",
}


def parse_pnputil_output(text: str) -> list[dict]:
    """Parse ``pnputil /enum-drivers`` text into raw entry dicts.

    The output is a series of blocks separated by blank lines, preceded
    by a "Microsoft PnP Utility" banner line. ``Driver Version`` packs
    date and version together ("03/15/2021 22.40.0.7"); they are split
    here into ``driver_date_text`` and ``version``.
    """
    entries: list[dict] = []
    block: dict = {}

    def flush() -> None:
        nonlocal block
        if block.get("inf_name"):
            entries.append(block)
        block = {}

    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            flush()
            continue
        if ":" not in line:
            continue  # banner and other noise
        key, _, value = line.partition(":")
        target = _PNPUTIL_FIELD_MAP.get(key.strip().lower())
        if target is None:
            continue
        value = value.strip()
        if target == "version_and_date":
            parts = value.split()
            if len(parts) >= 2:
                block["driver_date_text"] = parts[0]
                block["version"] = parts[1]
            elif parts:
                block["version"] = parts[0]
        else:
            block[target] = value
    flush()
    return entries


# ----------------------------------------------------------------------
# Collection
# ----------------------------------------------------------------------


def collect_inventory(runner=None) -> InventoryResult:
    """Collect raw driver entries from both query sources.

    The CIM device query is the primary source. pnputil is collected as
    a cross-check and becomes the fallback if the CIM query fails. If
    both fail, raises :class:`InventoryUnavailable` with a plain-
    language message — a designed failure state, not a traceback.
    """
    if runner is None:
        if not is_windows():
            raise InventoryUnavailable(
                "Charon scans Windows PCs, and this device isn't "
                "running Windows.",
                detail=f"sys.platform={sys.platform}",
            )
        runner = SubprocessRunner()

    result = InventoryResult()
    cim_error = ""

    try:
        raw = runner.run(list(POWERSHELL_ARGS))
        result.cim_entries = parse_cim_json(raw)
        result.sources.append("device query (Win32_PnPSignedDriver)")
    except InventoryUnavailable as exc:
        cim_error = exc.detail or str(exc)
        log.warning("CIM inventory query failed: %s", cim_error)
    except Exception as exc:  # malformed JSON etc. — fall back, don't die
        cim_error = f"{type(exc).__name__}: {exc}"
        log.warning("CIM inventory parse failed: %s", cim_error)

    try:
        raw = runner.run(list(PNPUTIL_ENUM_ARGS))
        result.pnputil_entries = parse_pnputil_output(raw)
        result.sources.append("driver store enumeration (pnputil)")
    except Exception as exc:
        detail = getattr(exc, "detail", "") or str(exc)
        log.warning("pnputil enumeration failed: %s", detail)
        result.errors.append(f"pnputil: {detail}")

    if not result.cim_entries and not result.pnputil_entries:
        raise InventoryUnavailable(
            "Charon couldn't read this PC's driver list. This can "
            "happen when Windows blocks the query — try running "
            "Charon as an administrator.",
            detail=f"CIM: {cim_error or 'no entries'}; "
            f"errors: {'; '.join(result.errors) or 'none'}",
        )
    if cim_error:
        result.errors.append(f"device query: {cim_error}")
    return result
