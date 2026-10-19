"""CHARON.EXE — the toll (Phase 3): a restore point, created AND verified.

Nobody crosses without paying the toll. This module is the toll
booth and the gate in one:

- :func:`pay_toll` creates a System Restore point through the one
  seam that may touch restore points, then **verifies** it exists —
  "creation requested" is not "toll paid."
- :func:`require_paid_toll` is the gate the Phase 4 apply engine
  must call first, every time. No verified toll record → it raises
  :class:`TollUnpaid`, and the ferry doesn't move.

Why verification is the load-bearing half: Windows silently skips
creating a new restore point when a recent one already exists (the
24-hour rule). The create call can return happy and *nothing new
exists*. So payment is proven only by enumeration afterwards: a
restore point qualifies iff

1. its description carries this run's marker — a unique token plus
   the run's timestamp, minted fresh inside :func:`pay_toll`, so a
   point from any earlier run cannot be mistaken for this one; and
2. its creation time is at or after the moment this run's creation
   call began (with a two-second tolerance for clock granularity —
   a point is stamped while the call runs, so the window opens at
   call start; the marker is the primary discriminator, the clock
   is the second, independent leg).

Every failure — skipped creation, a vanished point, a query that
fails — resolves to :class:`TollUnpaid`. Charon fails toward not
moving the ferry, always.

The boundary, stated plainly: Charon detects whether System
Protection is on and guides the owner to enable it if it isn't.
Charon never flips that setting itself — changing the machine's
protection state is the owner's deliberate act, not a side effect
of a driver tool.

Seam discipline mirrors the rest of the package: restore-point
commands live ONLY in this module, behind a runner protocol, with
the exact PowerShell scripts pinned as constants and by test.
Progress is reported as :class:`~charon.download.ProgressEvent`
beats — the same event shape Phase 2 established — so Phase 5's
screens can render the toll being paid.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Protocol

from .download import ProgressEvent, default_ledger_path, load_ledger

log = logging.getLogger("charon.toll")

# ----------------------------------------------------------------------
# Progress events (same shape as Phase 2's download events)
# ----------------------------------------------------------------------

EVENT_TOLL_CHECKING = "toll_checking"
EVENT_TOLL_CREATING = "toll_creating"
EVENT_TOLL_VERIFYING = "toll_verifying"
EVENT_TOLL_PAID = "toll_paid"
EVENT_TOLL_UNPAID = "toll_unpaid"

EmitFn = Callable[[ProgressEvent], None]


def _noop_emit(event: ProgressEvent) -> None:
    pass


# ----------------------------------------------------------------------
# Designed failures
# ----------------------------------------------------------------------


class TollUnpaid(Exception):
    """The toll was not paid, so nothing may cross (designed failure).

    Carries a plain-language message safe to show a non-technical
    user; the technical detail belongs in the log. Every subclass is
    still a ``TollUnpaid`` — the gate treats them identically: the
    ferry doesn't move.
    """

    def __init__(self, user_message: str, detail: str = "", next_step: str = ""):
        super().__init__(user_message)
        self.user_message = user_message
        self.detail = detail
        self.next_step = next_step


class SystemProtectionOff(TollUnpaid):
    """System Protection is off on the system drive — refused upstream.

    Detected BEFORE any creation attempt whenever the probe can tell,
    and mapped from the creation error when it can't. The message
    carries the how-to; enabling protection is the owner's act
    (Charon never changes that setting itself).
    """


class TollAdminRequired(TollUnpaid):
    """Restore points are administrator work; Charon isn't elevated."""


class TollBackendError(Exception):
    """Internal: the restore-point seam itself failed.

    ``detail`` holds the raw backend text (stderr, COM errors). The
    public functions convert this into :class:`TollUnpaid` — raw
    backend text must never reach a user's screen.
    """

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


# ----------------------------------------------------------------------
# Data
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class RestorePointInfo:
    """One restore point as the enumeration reports it."""

    sequence_number: int
    description: str
    created_at: datetime | None
    restore_point_type: int | None = None


@dataclass(frozen=True)
class TollProbe:
    """What the seam learned about this PC before any creation."""

    is_admin: bool
    protection_enabled: bool | None  # None = the probe couldn't tell
    system_drive: str = ""


@dataclass(frozen=True)
class TollRecord:
    """Proof of a paid toll: the restore point, verified, on record."""

    sequence_number: int
    description: str
    marker: str
    created_at: datetime
    verified_at: datetime
    computer: str = ""

    def to_dict(self) -> dict:
        return {
            "sequence_number": self.sequence_number,
            "description": self.description,
            "marker": self.marker,
            "created_at": _iso_z(self.created_at),
            "verified_at": _iso_z(self.verified_at),
            "computer": self.computer,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TollRecord":
        return cls(
            sequence_number=int(data["sequence_number"]),
            description=str(data["description"]),
            marker=str(data["marker"]),
            created_at=_parse_iso(str(data["created_at"])),
            verified_at=_parse_iso(str(data["verified_at"])),
            computer=str(data.get("computer", "")),
        )


def _iso_z(moment: datetime) -> str:
    return (
        moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    )


def _parse_iso(text: str) -> datetime:
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# ----------------------------------------------------------------------
# The marker
# ----------------------------------------------------------------------

MARKER_PREFIX = "CHARON.EXE crossing checkpoint"
_MARKER_SAFE = re.compile(r"^[A-Za-z0-9 .:#+\-]+$")


def build_marker(now: datetime | None = None, token: str | None = None) -> str:
    """The description this run's restore point must carry.

    ``CHARON.EXE crossing checkpoint <ISO timestamp> #<token>`` —
    the token makes the marker unique to this run, so a restore
    point from an earlier crossing can never be passed off as this
    one's toll. The charset is deliberately tame (no quotes, no
    shell punctuation): the marker is substituted into the pinned
    creation script verbatim.
    """
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    stamp = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{MARKER_PREFIX} {stamp} #{token or secrets.token_hex(4)}"


# ----------------------------------------------------------------------
# The seam
# ----------------------------------------------------------------------


class RestorePointBackend(Protocol):
    """The seam: anything that can work restore points on a PC.

    Implementations raise :class:`TollBackendError` for raw backend
    failures; the designed-failure translation lives in this module.
    """

    def probe(self) -> TollProbe:
        ...

    def create_restore_point(self, description: str) -> None:
        ...

    def list_restore_points(self) -> list[RestorePointInfo]:
        ...


# The exact PowerShell this module may run. Pinned here and by test:
# changing what Charon asks Windows about restore points is a
# deliberate, reviewable act.
TOLL_PROBE_SCRIPT = (
    "$principal = New-Object Security.Principal.WindowsPrincipal("
    "[Security.Principal.WindowsIdentity]::GetCurrent()); "
    "$admin = $principal.IsInRole("
    "[Security.Principal.WindowsBuiltInRole]::Administrator); "
    "$drive = $env:SystemDrive; "
    "$protection = $null; "
    "try { "
    "$vol = Get-CimInstance Win32_Volume "
    "-Filter \"DriveLetter='$drive'\" -ErrorAction Stop; "
    "$storage = Get-CimInstance Win32_ShadowStorage -ErrorAction Stop; "
    "$protection = $false; "
    "foreach ($s in $storage) { "
    "if ($s.Volume.DeviceID -eq $vol.DeviceID) { $protection = $true } "
    "} } catch { $protection = $null }; "
    "[PSCustomObject]@{ is_admin = $admin; system_drive = $drive; "
    "protection_enabled = $protection } | ConvertTo-Json -Compress"
)

RP_LIST_SCRIPT = (
    "Get-ComputerRestorePoint | "
    "Select-Object SequenceNumber, Description, CreationTime, "
    "RestorePointType | ConvertTo-Json -Depth 4"
)

RP_CREATE_SCRIPT_TEMPLATE = (
    "Checkpoint-Computer -Description '{description}' "
    "-RestorePointType 'MODIFY_SETTINGS'"
)


def _ps_args(script: str) -> list[str]:
    return ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]


class PowerShellRunner:
    """Runs the pinned PowerShell scripts; failures are backend errors.

    Kept behind the same tiny interface style as the inventory seam,
    so tests can substitute a recording fake and pin exactly what
    Charon asks the OS to do about restore points.
    """

    def run(self, args: list[str], timeout: int = 120) -> str:
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TollBackendError(f"{type(exc).__name__}: {exc}") from exc
        if proc.returncode != 0:
            raise TollBackendError(
                f"{args[0]} exited {proc.returncode}: "
                f"{proc.stderr.strip()[:600]}"
            )
        return proc.stdout


class PowerShellRestoreBackend:
    """The real Windows backend: probe, create, enumerate — pinned
    scripts only, nothing else."""

    def __init__(self, runner=None):
        self._runner = runner if runner is not None else PowerShellRunner()

    def probe(self) -> TollProbe:
        raw = self._runner.run(_ps_args(TOLL_PROBE_SCRIPT))
        return parse_probe_json(raw)

    def create_restore_point(self, description: str) -> None:
        if not _MARKER_SAFE.match(description):
            # The description is substituted into the pinned script;
            # anything outside the tame charset is a programming
            # error, refused before anything runs.
            raise ValueError(f"unsafe restore-point description: {description!r}")
        script = RP_CREATE_SCRIPT_TEMPLATE.format(description=description)
        # A checkpoint can take minutes on a busy machine.
        self._runner.run(_ps_args(script), timeout=900)

    def list_restore_points(self) -> list[RestorePointInfo]:
        raw = self._runner.run(_ps_args(RP_LIST_SCRIPT))
        return parse_restore_points_json(raw)


# ----------------------------------------------------------------------
# Pure parsers (testable anywhere)
# ----------------------------------------------------------------------


def parse_probe_json(text: str) -> TollProbe:
    """Parse the probe script's JSON into a :class:`TollProbe`."""
    try:
        data = json.loads((text or "").strip())
    except ValueError as exc:
        raise TollBackendError(f"probe returned unparseable output: {exc}") from exc
    if not isinstance(data, dict):
        raise TollBackendError("probe returned an unexpected shape")
    protection = data.get("protection_enabled")
    return TollProbe(
        is_admin=bool(data.get("is_admin")),
        protection_enabled=protection if isinstance(protection, bool) else None,
        system_drive=str(data.get("system_drive") or ""),
    )


_WMI_DT_RE = re.compile(
    r"^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(?:\.(\d{1,6}))?([+-])(\d{3,4})$"
)
_MS_DATE_RE = re.compile(r"/Date\((-?\d+)\)/")


def parse_restore_point_time(value) -> datetime | None:
    """Parse the shapes a restore point's creation time arrives in.

    Accepts: aware/naive ``datetime`` objects, WMI datetime strings
    (``yyyymmddHHMMSS.ffffff±UUU``, offset in minutes), PowerShell's
    ``/Date(<ms>)/`` JSON form, and ISO 8601 strings. Anything else
    → ``None`` (an undatable point can never prove a toll).
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    text = str(value).strip()
    if not text:
        return None

    wmi = _WMI_DT_RE.match(text)
    if wmi:
        year, month, day, hour, minute, second = (
            int(wmi.group(i)) for i in range(1, 7)
        )
        micros = int((wmi.group(7) or "0").ljust(6, "0"))
        sign = 1 if wmi.group(8) == "+" else -1
        offset = timezone(timedelta(minutes=sign * int(wmi.group(9))))
        try:
            parsed = datetime(
                year, month, day, hour, minute, second, micros, tzinfo=offset
            )
        except ValueError:
            return None
        return parsed.astimezone(timezone.utc)

    ms = _MS_DATE_RE.search(text)
    if ms:
        try:
            return datetime.fromtimestamp(
                int(ms.group(1)) / 1000, tz=timezone.utc
            )
        except (OverflowError, OSError, ValueError):
            return None

    try:
        return _parse_iso(text)
    except ValueError:
        return None


def parse_restore_points_json(text: str) -> list[RestorePointInfo]:
    """Parse the enumeration script's JSON output.

    PowerShell emits a bare object (not an array) when exactly one
    restore point exists, and nothing at all when there are none;
    all three shapes are accepted. Rows whose creation time won't
    parse keep ``created_at=None`` — they simply can never qualify.
    """
    text = (text or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise TollBackendError(
            f"restore-point list returned unparseable output: {exc}"
        ) from exc
    if isinstance(data, dict):
        rows = [data]
    elif isinstance(data, list):
        rows = [row for row in data if isinstance(row, dict)]
    else:
        return []
    points: list[RestorePointInfo] = []
    for row in rows:
        try:
            sequence = int(row.get("SequenceNumber"))
        except (TypeError, ValueError):
            continue
        rp_type = row.get("RestorePointType")
        points.append(
            RestorePointInfo(
                sequence_number=sequence,
                description=str(row.get("Description") or ""),
                created_at=parse_restore_point_time(row.get("CreationTime")),
                restore_point_type=rp_type if isinstance(rp_type, int) else None,
            )
        )
    return points


# ----------------------------------------------------------------------
# The ledger (the same file Phase 2's download ledger lives in)
# ----------------------------------------------------------------------


def _write_ledger(path: Path, ledger: dict) -> None:
    """Rewrite the shared ledger atomically (temp sibling + replace),
    the same pattern the download ledger writes with."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def record_toll(path: Path, record: TollRecord) -> None:
    """Append a paid toll to the ledger's ``tolls`` list."""
    ledger = load_ledger(path)
    tolls = ledger.get("tolls")
    if not isinstance(tolls, list):
        tolls = []
        ledger["tolls"] = tolls
    tolls.append(record.to_dict())
    _write_ledger(path, ledger)


def latest_toll(path: Path) -> TollRecord | None:
    """The most recent paid toll on record, or ``None``."""
    tolls = load_ledger(path).get("tolls")
    if not isinstance(tolls, list) or not tolls:
        return None
    try:
        return TollRecord.from_dict(tolls[-1])
    except (KeyError, TypeError, ValueError) as exc:
        log.warning("latest toll record in %s is unreadable: %s", path, exc)
        return None


# ----------------------------------------------------------------------
# Paying the toll
# ----------------------------------------------------------------------

#: A point is stamped while the creation call runs, so the qualifying
#: window opens at call start; two seconds absorbs clock granularity
#: (WMI timestamps are second-shaped in practice). The unique marker
#: remains the primary discriminator.
_VERIFY_TOLERANCE = timedelta(seconds=2)

_SKIP_MESSAGE = (
    "Windows skipped creating the restore point — it does that when "
    "a restore point was already made in the last 24 hours. The toll "
    "is not paid, so the ferry doesn't move."
)
_SKIP_NEXT_STEP = (
    "You can wait until a day has passed since the last restore "
    "point, or create one by hand in System Protection settings — "
    "then run Charon again and it will verify the toll before "
    "crossing."
)

_PROTECTION_OFF_MESSAGE = (
    "System Protection is turned off on your system drive — Charon "
    "can't create the restore point until it's on, and Charon never "
    "changes that setting for you."
)
_PROTECTION_OFF_NEXT_STEP = (
    "To turn it on: open the Start menu and search for \"Create a "
    "restore point\", select your system drive, choose Configure, "
    "turn on system protection, and apply. Then run Charon again."
)

_ADMIN_MESSAGE = (
    "Creating a restore point needs administrator rights, and "
    "Charon isn't running as an administrator."
)
_ADMIN_NEXT_STEP = "Right-click Charon and choose \"Run as administrator\", then try again."


def _default_backend() -> RestorePointBackend:
    if not sys.platform.startswith("win"):
        raise TollUnpaid(
            "Charon pays its toll on a Windows PC, and this device "
            "isn't running Windows.",
            detail=f"sys.platform={sys.platform}",
        )
    return PowerShellRestoreBackend()


_session_toll: TollRecord | None = None


def session_toll() -> TollRecord | None:
    """The toll paid in this process, if any."""
    return _session_toll


def clear_session_toll() -> None:
    """Forget this process's toll (a fresh run; also used by tests)."""
    global _session_toll
    _session_toll = None


def _is_protection_off_error(detail: str) -> bool:
    text = detail.lower()
    return any(
        phrase in text
        for phrase in (
            "system restore is disabled",
            "system restore is turned off",
            "system protection is disabled",
            "system protection is turned off",
            "is not enabled",
            "not been enabled",
        )
    )


def _is_frequency_error(detail: str) -> bool:
    text = detail.lower()
    return any(
        phrase in text
        for phrase in (
            "24 hours",
            "1440",
            "frequency",
            "already been created",
            "already created within",
        )
    )


def _find_qualifying(
    points: list[RestorePointInfo], marker: str, not_before: datetime
) -> RestorePointInfo | None:
    """The verification, whole: this run's marker, stamped no earlier
    than this run's creation call (within tolerance)."""
    for point in points:
        if marker not in point.description:
            continue
        if point.created_at is None:
            continue
        if point.created_at >= not_before:
            return point
    return None


def pay_toll(
    backend: RestorePointBackend | None = None,
    *,
    ledger_path: Path | None = None,
    emit: EmitFn | None = None,
    marker: str | None = None,
    computer: str | None = None,
    verify_attempts: int = 3,
    verify_delay: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> TollRecord:
    """Create the restore point and verify it exists. The toll.

    Returns the verified :class:`TollRecord` — also written to the
    ledger and held as this session's toll. Raises :class:`TollUnpaid`
    (or a subclass) for every way the toll can fail to be paid: the
    ferry doesn't move on a promise.
    """
    global _session_toll
    emit = emit or _noop_emit
    if backend is None:
        backend = _default_backend()

    def unpaid(exc: TollUnpaid) -> TollUnpaid:
        emit(
            ProgressEvent(
                kind=EVENT_TOLL_UNPAID,
                status="unpaid",
                message=exc.user_message,
            )
        )
        return exc

    emit(
        ProgressEvent(
            kind=EVENT_TOLL_CHECKING,
            message="Checking that this PC can pay the toll…",
        )
    )
    try:
        probe = backend.probe()
    except TollBackendError as exc:
        raise unpaid(
            TollUnpaid(
                "Charon couldn't check this PC's restore-point "
                "settings, so the toll can't be paid safely.",
                detail=exc.detail,
            )
        ) from exc
    if not probe.is_admin:
        raise unpaid(
            TollAdminRequired(
                _ADMIN_MESSAGE,
                detail="backend probe reports the process is not elevated",
                next_step=_ADMIN_NEXT_STEP,
            )
        )
    if probe.protection_enabled is False:
        raise unpaid(
            SystemProtectionOff(
                _PROTECTION_OFF_MESSAGE,
                detail=f"probe: no shadow storage for {probe.system_drive or 'the system drive'}",
                next_step=_PROTECTION_OFF_NEXT_STEP,
            )
        )
    # protection_enabled=None means the probe couldn't tell. Creation
    # is attempted anyway — verification below is the arbiter, and a
    # disabled-protection failure at creation maps back to the same
    # designed refusal.

    marker = marker or build_marker()
    emit(
        ProgressEvent(
            kind=EVENT_TOLL_CREATING,
            message="Creating the restore point — this can take a minute…",
        )
    )
    started_at = datetime.now(timezone.utc)
    try:
        backend.create_restore_point(marker)
    except TollBackendError as exc:
        if _is_protection_off_error(exc.detail):
            raise unpaid(
                SystemProtectionOff(
                    _PROTECTION_OFF_MESSAGE,
                    detail=exc.detail,
                    next_step=_PROTECTION_OFF_NEXT_STEP,
                )
            ) from exc
        if _is_frequency_error(exc.detail):
            raise unpaid(
                TollUnpaid(
                    _SKIP_MESSAGE,
                    detail=exc.detail,
                    next_step=_SKIP_NEXT_STEP,
                )
            ) from exc
        raise unpaid(
            TollUnpaid(
                "Windows couldn't create the restore point, so the "
                "toll is not paid and the ferry doesn't move.",
                detail=exc.detail,
                next_step="Try again in a few minutes; if it keeps "
                "failing, restart the PC and run Charon again.",
            )
        ) from exc

    emit(
        ProgressEvent(
            kind=EVENT_TOLL_VERIFYING,
            message="Verifying the restore point is really there…",
        )
    )
    not_before = started_at - _VERIFY_TOLERANCE
    hit: RestorePointInfo | None = None
    attempts = max(1, verify_attempts)
    for attempt in range(attempts):
        try:
            points = backend.list_restore_points()
        except TollBackendError as exc:
            # Fail toward not moving the ferry: an unverifiable toll
            # is an unpaid toll.
            raise unpaid(
                TollUnpaid(
                    "Charon couldn't read back this PC's restore "
                    "points to verify the toll, so it counts as "
                    "unpaid — the ferry doesn't move on a promise.",
                    detail=exc.detail,
                )
            ) from exc
        hit = _find_qualifying(points, marker, not_before)
        if hit is not None:
            break
        if attempt < attempts - 1:
            sleep(verify_delay)

    if hit is None:
        raise unpaid(
            TollUnpaid(
                _SKIP_MESSAGE,
                detail=(
                    f"no restore point bearing this run's marker "
                    f"({marker!r}) appeared after creation"
                ),
                next_step=_SKIP_NEXT_STEP,
            )
        )

    record = TollRecord(
        sequence_number=hit.sequence_number,
        description=hit.description,
        marker=marker,
        created_at=hit.created_at,
        verified_at=datetime.now(timezone.utc),
        computer=computer if computer is not None else socket.gethostname(),
    )
    path = Path(ledger_path) if ledger_path else default_ledger_path()
    try:
        record_toll(path, record)
    except OSError as exc:
        # A toll Charon can't prove on disk is no toll at all when
        # Phase 4 comes asking — fail closed.
        raise unpaid(
            TollUnpaid(
                "The restore point was created, but Charon couldn't "
                "write the proof into its ledger, so the crossing "
                "stays closed.",
                detail=f"{type(exc).__name__}: {exc}",
            )
        ) from exc

    _session_toll = record
    emit(
        ProgressEvent(
            kind=EVENT_TOLL_PAID,
            status="paid",
            message=(
                f"Toll paid — restore point "
                f"#{record.sequence_number}, verified."
            ),
        )
    )
    return record


# ----------------------------------------------------------------------
# The gate
# ----------------------------------------------------------------------


def require_paid_toll(
    backend: RestorePointBackend | None = None,
    *,
    ledger_path: Path | None = None,
    record: TollRecord | None = None,
) -> TollRecord:
    """The gate: a verified toll, or :class:`TollUnpaid`. Always.

    The Phase 4 apply engine calls this first, every time. The
    candidate record comes from — in order — the explicit ``record``
    argument, this session's toll, or the ledger's latest toll. The
    candidate is then **re-verified against the PC itself**: a
    restore point with the same sequence number still carrying this
    run's marker must still exist. A record whose point was deleted,
    or a PC that can't be queried, fails the gate — proof on paper
    is never enough on its own.
    """
    candidate = record or _session_toll
    if candidate is None:
        path = Path(ledger_path) if ledger_path else default_ledger_path()
        candidate = latest_toll(path)
    if candidate is None:
        raise TollUnpaid(
            "No toll has been paid for this crossing — there is no "
            "verified restore point on record.",
            next_step="Run the toll step first; the ferry only "
            "crosses behind a verified restore point.",
        )
    if backend is None:
        backend = _default_backend()
    try:
        points = backend.list_restore_points()
    except TollBackendError as exc:
        raise TollUnpaid(
            "Charon couldn't verify the toll against this PC's "
            "restore points, so the crossing stays closed.",
            detail=exc.detail,
        ) from exc
    for point in points:
        if (
            point.sequence_number == candidate.sequence_number
            and candidate.marker in point.description
        ):
            return candidate
    raise TollUnpaid(
        "The restore point that paid this crossing's toll can't be "
        "found on this PC anymore — it may have been deleted. The "
        "toll counts as unpaid, and the ferry doesn't move.",
        detail=f"sequence #{candidate.sequence_number} not in enumeration",
        next_step="Pay the toll again so a fresh restore point "
        "backs this crossing.",
    )
