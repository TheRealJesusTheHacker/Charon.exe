"""Matching: hardware-ID first, model fallback labeled, dates distrusted,
and ``unknown`` whenever Windows Update has no word — never a guess."""

from datetime import date

from charon.inventory import merge_inventories, normalize_update
from charon.matching import match_device, match_devices
from charon.model import InstalledDriver, UpdateCandidate
from charon.platform_inventory import parse_pnputil_output
from conftest import BS

WIFI_HWID = f"PCI{BS}VEN_8086&DEV_34F0"


def driver(**kw) -> InstalledDriver:
    base = dict(
        device_name="Intel(R) Wi-Fi 6 AX201 160MHz",
        device_class="Net",
        provider="Intel",
        version="22.40.0.7",
        driver_date=date(2021, 3, 15),
        hardware_ids=(WIFI_HWID,),
        is_signed=True,
    )
    base.update(kw)
    return InstalledDriver(**base)


def update(**kw) -> UpdateCandidate:
    base = dict(
        title="Intel Net Driver Update",
        driver_model="Intel(R) Wi-Fi 6 AX201 160MHz",
        driver_class="Net",
        provider="Intel",
        version="23.60.1.2",
        driver_date=date(2024, 5, 6),
        hardware_ids=(WIFI_HWID,),
    )
    base.update(kw)
    return UpdateCandidate(**base)


def test_hwid_match_newer_version_is_update_available():
    report = match_device(driver(), [update()])
    assert report.status == "update_available"
    assert report.basis == "hardware_id"
    assert report.confidence == "high"
    assert "22.40" in report.explanation
    assert "23.60.1.2" in report.explanation
    assert "available from Windows Update" in report.explanation


def test_hwid_match_is_case_insensitive():
    lower = update(hardware_ids=(WIFI_HWID.lower(),))
    report = match_device(driver(), [lower])
    assert report.status == "update_available"


def test_equal_version_is_up_to_date():
    report = match_device(driver(), [update(version="22.40.0.7")])
    assert report.status == "up_to_date"
    assert report.confidence == "high"


def test_offered_older_than_installed_is_up_to_date_with_note():
    report = match_device(driver(version="19.5.35.54"), [update(version="19.3.4.219")])
    assert report.status == "up_to_date"
    assert any("newer than the one Windows Update offers" in n for n in report.notes)


def test_no_candidate_is_unknown_and_never_guesses():
    report = match_device(driver(), [])
    assert report.status == "unknown"
    assert report.update is None
    assert report.confidence == "none"
    assert "never guesses" in report.explanation


def test_unrelated_update_does_not_match():
    other = update(
        driver_model="Qualcomm Atheros AR938x",
        hardware_ids=(f"PCI{BS}VEN_168C&DEV_0036",),
    )
    report = match_device(driver(), [other])
    assert report.status == "unknown"


def test_model_match_only_is_labeled_lower_confidence():
    name_only = update(hardware_ids=())
    report = match_device(driver(), [name_only])
    assert report.status == "update_available"
    assert report.basis == "model"
    assert report.confidence == "medium"
    assert any("device name only" in n for n in report.notes)


def test_date_only_difference_is_low_confidence_with_caveat():
    d = driver(version="", driver_date=date(2020, 6, 19))
    u = update(version="", driver_date=date(2023, 2, 14))
    report = match_device(d, [u])
    assert report.status == "update_available"
    assert report.basis == "date"
    assert report.confidence == "low"
    assert any("dates can be" in n and "misleading" in n for n in report.notes)


def test_date_only_older_offer_is_up_to_date():
    d = driver(version="", driver_date=date(2023, 6, 19))
    u = update(version="", driver_date=date(2020, 2, 14))
    report = match_device(d, [u])
    assert report.status == "up_to_date"
    assert report.basis == "date"


def test_best_candidate_wins_among_several():
    older = update(version="23.10.0.1", title="older")
    newer = update(version="23.60.1.2", title="newer")
    report = match_device(driver(), [older, newer])
    assert report.update.title == "newer"


def test_duplicate_hardware_ids_match_each_device():
    hub_a = driver(
        device_name="Generic USB Hub (Port 1)",
        version="10.0.22621.2506",
        hardware_ids=(f"USB{BS}ROOT_HUB30",),
    )
    hub_b = driver(
        device_name="Generic USB Hub (Port 2)",
        version="10.0.22621.2506",
        hardware_ids=(f"USB{BS}ROOT_HUB30",),
    )
    hub_update = update(
        driver_model="USB Root Hub (USB 3.0)",
        version="10.0.22621.3000",
        hardware_ids=(f"USB{BS}ROOT_HUB30",),
    )
    reports = match_devices([hub_a, hub_b], [hub_update])
    assert [r.status for r in reports] == ["update_available", "update_available"]


def test_full_fixture_machine(cim_entries, pnputil_text, wua_updates):
    """End-to-end over the recorded machine: every designed case lands."""
    drivers = merge_inventories(cim_entries, parse_pnputil_output(pnputil_text))
    updates = [normalize_update(u) for u in wua_updates]
    reports = match_devices(drivers, updates)
    by_name = {r.driver.device_name: r for r in reports}

    wifi = by_name["Intel(R) Wi-Fi 6 AX201 160MHz"]
    assert (wifi.status, wifi.basis, wifi.confidence) == (
        "update_available",
        "hardware_id",
        "high",
    )

    nvidia = by_name["NVIDIA GeForce RTX 3060"]
    assert nvidia.status == "up_to_date"

    realtek = by_name["Realtek High Definition Audio"]
    assert realtek.basis == "model"  # no hardware ID in the WUA result
    assert realtek.status == "up_to_date"

    sata = by_name["Standard SATA AHCI Controller"]
    assert (sata.status, sata.basis, sata.confidence) == (
        "update_available",
        "date",
        "low",
    )

    for port in ("Generic USB Hub (Port 1)", "Generic USB Hub (Port 2)"):
        assert by_name[port].status == "update_available"

    touchpad = by_name["Synaptics TouchPad"]
    assert touchpad.status == "up_to_date"  # WUA offers an older driver

    # Nothing offered → unknown, and the unsigned widget stays unknown.
    assert by_name["Contoso Test Widget"].status == "unknown"
    assert by_name["Intel(R) Wireless Bluetooth(R)"].status == "unknown"

    # Exactly the designed split: 4 updates (Wi-Fi, SATA, both hubs),
    # 3 up to date (NVIDIA, Realtek, touchpad), 2 unknown.
    statuses = [r.status for r in reports]
    assert statuses.count("update_available") == 4
    assert statuses.count("up_to_date") == 3
    assert statuses.count("unknown") == 2


def test_model_match_needs_word_boundaries():
    """Sweep #3: a shared fragment of a word is not a shared model.

    "net" inside "network" (and "audio" inside "audiophile")
    used to count as a model match, offering one device another
    device's driver at medium confidence. Containment now counts
    only at word boundaries; genuine part-name matches still do.
    """
    from charon.matching import _models_match

    assert _models_match("Net", "Network Adapter") is False
    assert _models_match("Audiophile", "Audio Device") is False
    assert _models_match("Realtek Audio", "Realtek Audio Device") is True
    assert _models_match("Intel Wi-Fi 6", "Wi-Fi 6") is True
    assert _models_match("Same Model", "Same Model") is True
    assert _models_match("", "Anything") is False
