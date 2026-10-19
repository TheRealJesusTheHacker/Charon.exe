"""Normalization + merge: raw sources become the passenger manifest."""

from datetime import date

from charon.inventory import (
    merge_inventories,
    normalize_cim_entry,
    normalize_pnputil_entry,
    normalize_update,
)
from charon.platform_inventory import parse_pnputil_output


def test_normalize_cim_entry(cim_entries):
    wifi = normalize_cim_entry(cim_entries[0])
    assert wifi.device_name == "Intel(R) Wi-Fi 6 AX201 160MHz"
    assert wifi.device_class == "Net"
    assert wifi.provider == "Intel"
    assert wifi.version == "22.40.0.7"
    assert wifi.driver_date == date(2021, 3, 15)  # from /Date(...)/ form
    assert wifi.inf_name == "oem42.inf"
    assert wifi.is_signed is True
    expected_hwids = tuple(h.upper() for h in cim_entries[0]["HardWareID"])
    assert wifi.hardware_ids == expected_hwids


def test_normalize_cim_entry_date_formats(cim_entries):
    by_name = {e["DeviceName"]: normalize_cim_entry(e) for e in cim_entries}
    # ISO string form.
    assert by_name["NVIDIA GeForce RTX 3060"].driver_date == date(2023, 6, 14)
    # Raw WMI datetime string form.
    assert by_name["Standard SATA AHCI Controller"].driver_date == date(2020, 6, 19)
    # Plain yyyy-mm-dd form.
    assert by_name["Synaptics TouchPad"].driver_date == date(2022, 11, 2)


def test_normalize_cim_unsigned_preserved(cim_entries):
    widget = next(
        normalize_cim_entry(e)
        for e in cim_entries
        if e["DeviceName"] == "Contoso Test Widget"
    )
    assert widget.is_signed is False


def test_normalize_pnputil_entry():
    raw = {
        "inf_name": "oem42.inf",
        "original_name": "netwtw08.inf",
        "provider": "Intel",
        "device_class": "Net",
        "driver_date_text": "03/15/2021",
        "version": "22.40.0.7",
        "signer": "Microsoft Windows Hardware Compatibility Publisher",
    }
    entry = normalize_pnputil_entry(raw)
    assert entry.device_name == "netwtw08.inf"  # packages have no device name
    assert entry.driver_date == date(2021, 3, 15)
    assert entry.is_signed is True
    assert entry.hardware_ids == ()
    assert entry.device_id == ""


def test_normalize_pnputil_unknown_signer_is_unsigned():
    raw = {
        "inf_name": "oem1.inf",
        "original_name": "x.inf",
        "driver_date_text": "01/01/2020",
        "version": "1.0",
        "signer": "<Unknown>",
    }
    assert normalize_pnputil_entry(raw).is_signed is False


def test_normalize_update(wua_updates):
    upd = normalize_update(wua_updates[0])
    assert upd.driver_model == "Intel(R) Wi-Fi 6 AX201 160MHz"
    assert upd.version == "23.60.1.2"
    assert upd.driver_date == date(2024, 5, 6)
    expected = tuple(h.upper() for h in wua_updates[0]["hardware_ids"])
    assert upd.hardware_ids == expected


def test_merge_cim_primary_with_pnputil_corroboration(cim_entries, pnputil_text):
    pnp_raw = parse_pnputil_output(pnputil_text)
    drivers = merge_inventories(cim_entries, pnp_raw)
    # CIM is the manifest: 9 devices, pnputil's package-only printer
    # (oem99) does not invent a tenth device.
    assert len(drivers) == 9
    names = {d.device_name for d in drivers}
    assert "contosoprint.inf" not in names


def test_merge_records_version_disagreement_as_note(cim_entries, pnputil_text):
    pnp_raw = parse_pnputil_output(pnputil_text)
    drivers = merge_inventories(cim_entries, pnp_raw)
    touchpad = next(d for d in drivers if d.device_name == "Synaptics TouchPad")
    # CIM says 19.5.35.54, the driver store says 19.5.35.31 — the
    # disagreement is recorded, not silently resolved.
    assert any("19.5.35.31" in n for n in touchpad.notes)


def test_merge_agreement_produces_no_note(cim_entries, pnputil_text):
    pnp_raw = parse_pnputil_output(pnputil_text)
    drivers = merge_inventories(cim_entries, pnp_raw)
    wifi = next(d for d in drivers if "Wi-Fi" in d.device_name)
    assert wifi.notes == ()


def test_merge_falls_back_to_pnputil_when_cim_empty(pnputil_text):
    pnp_raw = parse_pnputil_output(pnputil_text)
    drivers = merge_inventories([], pnp_raw)
    assert len(drivers) == 5
    assert {d.inf_name for d in drivers} == {
        "oem42.inf",
        "oem18.inf",
        "oem7.inf",
        "oem55.inf",
        "oem99.inf",
    }
