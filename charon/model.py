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
    @classmethod
    def from_dict(cls, data: dict) -> "ScanReport":
        """Rebuild a report from :meth:`to_dict` output.

        Defensive about everything except the shape itself: a saved
        scan report is user-facing data a human may have moved or
        trimmed, so missing fields degrade to empties rather than
        raising. Used by ``charon download --from-report``.
        """
        generated = data.get("generated_at") or ""
        try:
            generated_at = datetime.fromisoformat(
                str(generated).replace("Z", "+00:00")
            )
        except ValueError:
            generated_at = datetime.now().astimezone()
        devices = []
        for raw in _list_or_empty(data.get("devices")):
            if not isinstance(raw, dict):
                continue
            devices.append(_device_from_dict(raw))
        return cls(
            generated_at=generated_at,
            computer=str(data.get("computer") or ""),
            sources=[str(s) for s in _list_or_empty(data.get("sources"))],
            warnings=[str(w) for w in _list_or_empty(data.get("warnings"))],
            devices=devices,
        )

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


def _list_or_empty(value) -> list:
    """The value as a list, or empty — a saved report is user-facing
    data that may have been trimmed by hand, so a field of the wrong
    type (an int where a list belongs, a bare string) degrades to
    empty here instead of raising TypeError mid-load."""
    return list(value) if isinstance(value, (list, tuple)) else []


def _driver_from_dict(data: dict) -> InstalledDriver:
    if not isinstance(data, dict):
        data = {}
    return InstalledDriver(
        device_name=str(data.get("device_name") or "Unknown device"),
        device_class=str(data.get("device_class") or ""),
        provider=str(data.get("provider") or ""),
        version=str(data.get("version") or ""),
        driver_date=parse_driver_date(data.get("driver_date")),
        inf_name=str(data.get("inf_name") or ""),
        device_id=str(data.get("device_id") or ""),
        hardware_ids=tuple(
            normalize_hardware_id(str(h))
            for h in _list_or_empty(data.get("hardware_ids"))
            if str(h).strip()
        ),
        is_signed=data.get("is_signed")
        if isinstance(data.get("is_signed"), bool)
        else None,
        notes=tuple(str(n) for n in _list_or_empty(data.get("notes"))),
    )


def _update_from_dict(data: dict) -> UpdateCandidate:
    if not isinstance(data, dict):
        data = {}
    return UpdateCandidate(
        title=str(data.get("title") or ""),
        driver_model=str(data.get("driver_model") or ""),
        driver_class=str(data.get("driver_class") or ""),
        provider=str(data.get("provider") or ""),
        version=str(data.get("version") or ""),
        driver_date=parse_driver_date(data.get("driver_date")),
        hardware_ids=tuple(
            normalize_hardware_id(str(h))
            for h in _list_or_empty(data.get("hardware_ids"))
            if str(h).strip()
        ),
    )


def _device_from_dict(data: dict) -> DeviceReport:
    if not isinstance(data, dict):
        data = {}
    update_raw = data.get("update")
    driver_raw = data.get("driver")
    return DeviceReport(
        driver=_driver_from_dict(
            driver_raw if isinstance(driver_raw, dict) else {}
        ),
        status=str(data.get("status") or STATUS_UNKNOWN),
        update=_update_from_dict(update_raw)
        if isinstance(update_raw, dict)
        else None,
        confidence=str(data.get("confidence") or CONFIDENCE_NONE),
        basis=str(data.get("basis") or BASIS_NONE),
        explanation=str(data.get("explanation") or ""),
        notes=tuple(str(n) for n in _list_or_empty(data.get("notes"))),
    )


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
        if len(digits.group(0)) > 18:
            # An absurd component (no real driver version has one):
            # int() itself refuses digit strings past a few thousand
            # characters, so treat the whole version as unparseable
            # — "unknown", the honest answer — instead of raising.
            return None
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
