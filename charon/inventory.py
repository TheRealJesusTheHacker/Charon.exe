"""CHARON.EXE — normalization: raw seam output → clean model objects.

Pure logic, no OS access. Both inventory sources (the CIM device query
and the pnputil driver-store enumeration) and the Windows Update
search results are normalized here into the dataclasses in ``model``.
"""

from __future__ import annotations

from dataclasses import replace

from .model import (
    InstalledDriver,
    UpdateCandidate,
    normalize_hardware_id,
    parse_driver_date,
)


def _text(value) -> str:
    return str(value).strip() if value is not None else ""


def _hardware_ids(values) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        values = [values]
    out: list[str] = []
    for v in values:
        hid = normalize_hardware_id(str(v))
        if hid and hid not in out:
            out.append(hid)
    return tuple(out)


def _as_bool(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("true", "1", "yes"):
        return True
    if text in ("false", "0", "no"):
        return False
    return None


def normalize_cim_entry(raw: dict) -> InstalledDriver:
    """One Win32_PnPSignedDriver row → InstalledDriver."""
    return InstalledDriver(
        device_name=_text(raw.get("DeviceName")) or "Unknown device",
        device_class=_text(raw.get("DeviceClass")),
        provider=_text(raw.get("DriverProviderName")),
        version=_text(raw.get("DriverVersion")),
        driver_date=parse_driver_date(raw.get("DriverDate")),
        inf_name=_text(raw.get("InfName")),
        device_id=_text(raw.get("DeviceID")),
        hardware_ids=_hardware_ids(raw.get("HardWareID")),
        is_signed=_as_bool(raw.get("IsSigned")),
    )


def normalize_pnputil_entry(raw: dict) -> InstalledDriver:
    """One pnputil driver-store package → InstalledDriver.

    pnputil describes packages, not devices: there is no device name
    or hardware ID, so the INF's original name stands in as the label
    and the hardware ID list stays empty. A package counts as signed
    when pnputil names a real signer.
    """
    signer = _text(raw.get("signer"))
    signed = bool(signer) and "unknown" not in signer.lower()
    label = (
        _text(raw.get("original_name"))
        or _text(raw.get("inf_name"))
        or "Unknown driver package"
    )
    return InstalledDriver(
        device_name=label,
        device_class=_text(raw.get("device_class")),
        provider=_text(raw.get("provider")),
        version=_text(raw.get("version")),
        driver_date=parse_driver_date(raw.get("driver_date_text")),
        inf_name=_text(raw.get("inf_name")),
        device_id="",
        hardware_ids=(),
        is_signed=signed,
    )


def normalize_update(raw: dict) -> UpdateCandidate:
    """One Windows Update search result → UpdateCandidate."""
    date_value = raw.get("driver_date", raw.get("driver_date_text"))
    return UpdateCandidate(
        title=_text(raw.get("title")),
        driver_model=_text(raw.get("driver_model")),
        driver_class=_text(raw.get("driver_class")),
        provider=_text(raw.get("provider")),
        version=_text(raw.get("version")),
        driver_date=parse_driver_date(date_value),
        hardware_ids=_hardware_ids(raw.get("hardware_ids")),
    )


def merge_inventories(
    cim_entries: list[dict], pnputil_entries: list[dict]
) -> list[InstalledDriver]:
    """Build the passenger manifest from the raw sources.

    The CIM device query is authoritative (it knows devices, hardware
    IDs, signer status). pnputil corroborates per INF: when the two
    disagree about a version, the disagreement is recorded as a note
    on the entry rather than silently resolved — audits beat guesses.
    When the CIM query produced nothing, the pnputil packages become
    the manifest (fallback mode).
    """
    pnp_by_inf: dict[str, InstalledDriver] = {}
    for raw in pnputil_entries:
        entry = normalize_pnputil_entry(raw)
        if entry.inf_name:
            pnp_by_inf[entry.inf_name.lower()] = entry

    if not cim_entries:
        return [normalize_pnputil_entry(raw) for raw in pnputil_entries]

    drivers: list[InstalledDriver] = []
    for raw in cim_entries:
        driver = normalize_cim_entry(raw)
        twin = pnp_by_inf.get(driver.inf_name.lower()) if driver.inf_name else None
        if twin is not None:
            notes = list(driver.notes)
            if twin.version and driver.version and twin.version != driver.version:
                notes.append(
                    f"Driver store lists version {twin.version} for "
                    f"{driver.inf_name}; the device reports "
                    f"{driver.version}."
                )
            if driver.is_signed is None and twin.is_signed is not None:
                driver = replace(driver, is_signed=twin.is_signed)
            if notes:
                driver = replace(driver, notes=tuple(notes))
        drivers.append(driver)
    return drivers
