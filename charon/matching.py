"""CHARON.EXE — matching: installed drivers ↔ offered updates.

Pure logic. For every installed driver, find the best update Windows
Update offers for it and classify honestly:

- ``update_available`` — a newer driver is offered.
- ``up_to_date`` — what's installed is at least as new as what's offered.
- ``unknown`` — Windows Update reported nothing for this device.
  Charon never guesses an outdated verdict it cannot back up.

Matching prefers hardware IDs (they identify the actual device).
Falling back to the device-model name is allowed but is labeled with a
lower confidence. Version comparison is preferred over dates: driver
dates lie (vendors post-date releases), so a difference visible only
in dates is reported at low confidence, with the caveat in the notes.
"""

from __future__ import annotations

import re

from .model import (
    BASIS_DATE,
    BASIS_HARDWARE_ID,
    BASIS_MODEL,
    BASIS_NONE,
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    CONFIDENCE_NONE,
    DeviceReport,
    InstalledDriver,
    STATUS_UNKNOWN,
    STATUS_UP_TO_DATE,
    STATUS_UPDATE_AVAILABLE,
    UpdateCandidate,
    compare_versions,
    format_date,
    parse_version,
)


def _model_key(text: str) -> str:
    """Loose key for model-name matching: lowercase alphanumerics only."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _models_match(device_name: str, update_model: str) -> bool:
    a, b = _model_key(device_name), _model_key(update_model)
    if not a or not b:
        return False
    return a == b or a in b or b in a


def _best(candidates: list[UpdateCandidate]) -> UpdateCandidate:
    """Pick the newest candidate; unparseable versions sort last."""

    def key(u: UpdateCandidate):
        parsed = parse_version(u.version)
        return (parsed is not None, parsed or ())

    return max(candidates, key=key)


def _classify(
    driver: InstalledDriver,
    update: UpdateCandidate,
    basis: str,
    confidence: str,
) -> DeviceReport:
    cmp = compare_versions(update.version, driver.version)
    notes: list[str] = []
    if basis == BASIS_HARDWARE_ID:
        notes.append("Matched to this exact device by its hardware ID.")
    elif basis == BASIS_MODEL:
        notes.append(
            "Matched by device name only — Windows Update did not tie "
            "this update to the device's hardware ID."
        )

    if cmp is not None:
        if cmp > 0:
            explanation = (
                f"Your {driver.device_name} driver is version "
                f"{driver.version or 'unknown'} from "
                f"{format_date(driver.driver_date)}. A newer one — "
                f"version {update.version} from "
                f"{format_date(update.driver_date)} — is available "
                f"from Windows Update."
            )
            return DeviceReport(
                driver=driver,
                status=STATUS_UPDATE_AVAILABLE,
                update=update,
                confidence=confidence,
                basis=basis,
                explanation=explanation,
                notes=tuple(notes),
            )
        if cmp < 0:
            notes.append(
                "The installed driver is newer than the one Windows "
                "Update offers — this happens when a driver was "
                "installed directly from the manufacturer."
            )
        explanation = (
            f"Your {driver.device_name} driver (version "
            f"{driver.version or 'unknown'}) is the newest one "
            f"Windows Update offers for this device."
        )
        return DeviceReport(
            driver=driver,
            status=STATUS_UP_TO_DATE,
            update=update,
            confidence=confidence,
            basis=basis,
            explanation=explanation,
            notes=tuple(notes),
        )

    # Versions could not be compared — fall back to dates, cautiously.
    if driver.driver_date and update.driver_date:
        if update.driver_date > driver.driver_date:
            notes.append(
                "Decided by driver dates alone, because the version "
                "numbers could not be compared. Driver dates can be "
                "misleading — vendors sometimes date a release later "
                "than it is — so treat this one as a maybe."
            )
            explanation = (
                f"Your {driver.device_name} driver is dated "
                f"{format_date(driver.driver_date)}, and Windows "
                f"Update offers one dated "
                f"{format_date(update.driver_date)}. The version "
                f"numbers could not be compared, so this is based on "
                f"dates alone."
            )
            return DeviceReport(
                driver=driver,
                status=STATUS_UPDATE_AVAILABLE,
                update=update,
                confidence=CONFIDENCE_LOW,
                basis=BASIS_DATE,
                explanation=explanation,
                notes=tuple(notes),
            )
        explanation = (
            f"Your {driver.device_name} driver (dated "
            f"{format_date(driver.driver_date)}) is at least as new "
            f"as the one Windows Update offers."
        )
        return DeviceReport(
            driver=driver,
            status=STATUS_UP_TO_DATE,
            update=update,
            confidence=CONFIDENCE_LOW,
            basis=BASIS_DATE,
            explanation=explanation,
            notes=tuple(notes),
        )

    # An update exists for this device but nothing can be compared.
    notes.append(
        "Windows Update offers a driver for this device, but neither "
        "its version nor its date could be compared with the "
        "installed one."
    )
    explanation = (
        f"Windows Update offers a driver for your "
        f"{driver.device_name}, but Charon couldn't compare it with "
        f"the installed version {driver.version or 'unknown'}, so "
        f"this one needs a human look before anyone acts on it."
    )
    return DeviceReport(
        driver=driver,
        status=STATUS_UNKNOWN,
        update=update,
        confidence=CONFIDENCE_LOW,
        basis=basis,
        explanation=explanation,
        notes=tuple(notes),
    )


def _unknown(driver: InstalledDriver) -> DeviceReport:
    explanation = (
        f"Windows Update didn't report any driver for your "
        f"{driver.device_name}, so Charon can't tell whether it's "
        f"outdated. An old driver with no successor on Windows Update "
        f"is a fact, not a failure — and Charon never guesses."
    )
    return DeviceReport(
        driver=driver,
        status=STATUS_UNKNOWN,
        update=None,
        confidence=CONFIDENCE_NONE,
        basis=BASIS_NONE,
        explanation=explanation,
    )


def match_device(
    driver: InstalledDriver, updates: list[UpdateCandidate]
) -> DeviceReport:
    """Classify one installed driver against all offered updates."""
    driver_hwids = set(driver.hardware_ids)
    by_hwid = [
        u for u in updates if driver_hwids & set(u.hardware_ids)
    ]
    if by_hwid:
        return _classify(driver, _best(by_hwid), BASIS_HARDWARE_ID, CONFIDENCE_HIGH)

    by_model = [
        u for u in updates if _models_match(driver.device_name, u.driver_model)
    ]
    if by_model:
        return _classify(driver, _best(by_model), BASIS_MODEL, CONFIDENCE_MEDIUM)

    return _unknown(driver)


def match_devices(
    drivers: list[InstalledDriver], updates: list[UpdateCandidate]
) -> list[DeviceReport]:
    """Classify every installed driver. Order follows the manifest."""
    return [match_device(d, updates) for d in drivers]
