"""CHARON.EXE — data model for the Phase 1 scan report.

Pure data + pure helpers. Nothing in this module touches the OS, the
network, or the filesystem; the Windows-only seams live in
``platform_inventory`` and ``wua``, and the pure logic that fills these
dataclasses lives in ``inventory`` and ``matching``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

# Device classification outcomes. ``unknown`` is a first-class result:
# when Windows Update reports nothing for a device, Charon says so and
# never guesses.
STATUS_UPDATE_AVAILABLE = "update_available"
STATUS_UP_TO_DATE = "up_to_date"
STATUS_UNKNOWN = "unknown"

# How sure Charon is about a match / classification.
CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"
CONFIDENCE_NONE = "none"

# How an installed driver was paired with an offered update.
BASIS_HARDWARE_ID = "hardware_id"
BASIS_MODEL = "model"
BASIS_DATE = "date"
BASIS_NONE = "none"


@dataclass(frozen=True)
class InstalledDriver:
    """One installed driver, normalized from the platform seams."""

    device_name: str
    device_class: str = ""
    provider: str = ""
    version: str = ""
    driver_date: date | None = None
    inf_name: str = ""
    device_id: str = ""
    hardware_ids: tuple[str, ...] = ()
    is_signed: bool | None = None
    # Free-form corroboration notes (e.g. "the driver-store listing
    # reports version X; the device query reports Y"). Never shown raw
    # to end users without the plain-language layer, but kept in the
    # JSON export.
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class UpdateCandidate:
    """One driver update offered by Windows Update (search results only)."""

    title: str
    driver_model: str = ""
    driver_class: str = ""
    provider: str = ""
    version: str = ""
    driver_date: date | None = None
    hardware_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeviceReport:
    """The verdict for one installed driver."""

    driver: InstalledDriver
    status: str
    update: UpdateCandidate | None = None
    confidence: str = CONFIDENCE_NONE
    basis: str = BASIS_NONE
    explanation: str = ""
    notes: tuple[str, ...] = ()


@dataclass
class ScanReport:
    """The whole passenger manifest for one machine scan."""

    generated_at: datetime
    computer: str = ""
    sources: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    devices: list[DeviceReport] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Summary helpers
    # ------------------------------------------------------------------
    def count(self, status: str) -> int:
        return sum(1 for d in self.devices if d.status == status)

    @property
    def total(self) -> int:
        return len(self.devices)

    @property
    def updates_available(self) -> int:
        return self.count(STATUS_UPDATE_AVAILABLE)

    @property
    def up_to_date(self) -> int:
        return self.count(STATUS_UP_TO_DATE)

    @property
    def unknown(self) -> int:
        return self.count(STATUS_UNKNOWN)

    @property
    def unsigned(self) -> int:
        return sum(1 for d in self.devices if d.driver.is_signed is False)

    def summary_line(self) -> str:
        return (
            f"{self.total} drivers checked: "
            f"{self.updates_available} with updates available, "
            f"{self.up_to_date} up to date, "
            f"{self.unknown} Windows Update had no word on."
        )

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "computer": self.computer,
            "sources": list(self.sources),
            "warnings": list(self.warnings),
            "summary": {
                "total": self.total,
                "updates_available": self.updates_available,
                "up_to_date": self.up_to_date,
                "unknown": self.unknown,
                "unsigned": self.unsigned,
            },
            "devices": [_device_to_dict(d) for d in self.devices],
        }


def _driver_to_dict(d: InstalledDriver) -> dict:
    return {
        "device_name": d.device_name,
        "device_class": d.device_class,
        "provider": d.provider,
        "version": d.version,
        "driver_date": d.driver_date.isoformat() if d.driver_date else None,
        "inf_name": d.inf_name,
        "device_id": d.device_id,
        "hardware_ids": list(d.hardware_ids),
        "is_signed": d.is_signed,
        "notes": list(d.notes),
    }


def _update_to_dict(u: UpdateCandidate) -> dict:
    return {
        "title": u.title,
        "driver_model": u.driver_model,
        "driver_class": u.driver_class,
        "provider": u.provider,
        "version": u.version,
        "driver_date": u.driver_date.isoformat() if u.driver_date else None,
        "hardware_ids": list(u.hardware_ids),
    }


def _device_to_dict(d: DeviceReport) -> dict:
    return {
        "driver": _driver_to_dict(d.driver),
        "status": d.status,
        "confidence": d.confidence,
        "basis": d.basis,
        "explanation": d.explanation,
        "notes": list(d.notes),
        "update": _update_to_dict(d.update) if d.update else None,
    }


# ----------------------------------------------------------------------
# Pure parsing / comparison helpers
# ----------------------------------------------------------------------

_VERSION_SPLIT = re.compile(r"[.\-_+]")


def parse_version(text: str | None) -> tuple[int, ...] | None:
    """Parse a dotted driver version into a comparable tuple of ints.

    Returns ``None`` when there is nothing numeric to compare — callers
    must treat that as "unknown", never as zero.
    """
    if not text:
        return None
    parts: list[int] = []
    for chunk in _VERSION_SPLIT.split(str(text).strip()):
        digits = re.match(r"\d+", chunk)
        if digits is None:
            # A non-numeric component (e.g. "1.2b3") — keep what we have
            # only if something numeric came before it.
            break
        parts.append(int(digits.group(0)))
    return tuple(parts) if parts else None


def compare_versions(a: str | None, b: str | None) -> int | None:
    """-1 if a < b, 0 if equal, 1 if a > b; ``None`` if not comparable."""
    pa, pb = parse_version(a), parse_version(b)
    if pa is None or pb is None:
        return None
    width = max(len(pa), len(pb))
    pa = pa + (0,) * (width - len(pa))
    pb = pb + (0,) * (width - len(pb))
    if pa < pb:
        return -1
    if pa > pb:
        return 1
    return 0


_MS_DATE_RE = re.compile(r"/Date\((-?\d+)\)/")
_WMI_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})")


def parse_driver_date(value) -> date | None:
    """Best-effort parse of the many shapes driver dates arrive in.

    Accepts: ``date``/``datetime`` objects, ISO strings, PowerShell's
    ``/Date(1615766400000)/`` JSON form, raw WMI ``yyyymmddHHMMSS...``
    strings, and pnputil's ``MM/DD/YYYY`` form. Anything else → None.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None

    ms = _MS_DATE_RE.search(text)
    if ms:
        try:
            return datetime.fromtimestamp(
                int(ms.group(1)) / 1000, tz=timezone.utc
            ).date()
        except (OverflowError, OSError, ValueError):
            return None

    # Raw WMI datetime: "yyyymmddHHMMSS.mmmmmm+offset" (digits continue
    # right after the 8-digit date). A bare "yyyymmdd" also counts.
    wmi = _WMI_DATE_RE.match(text)
    if wmi and (len(text) == 8 or text[8].isdigit()):
        try:
            return date(int(wmi.group(1)), int(wmi.group(2)), int(wmi.group(3)))
        except ValueError:
            return None

    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(text[:19] if "T" in text else text, fmt).date()
        except ValueError:
            continue
    # ISO with timezone / fractional seconds.
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def normalize_hardware_id(text: str | None) -> str:
    """Hardware IDs compare case-insensitively; canonical form is upper."""
    return (text or "").strip().upper()


def format_date(d: date | None) -> str:
    return d.isoformat() if d else "an unknown date"
