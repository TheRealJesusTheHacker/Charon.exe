"""Platform seam: parsers, exact-command pinning, fallback behavior."""

import pytest

from charon.platform_inventory import (
    CIM_QUERY_SCRIPT,
    PNPUTIL_ENUM_ARGS,
    InventoryUnavailable,
    collect_inventory,
    parse_cim_json,
    parse_pnputil_output,
)
from conftest import RecordingRunner


def test_parse_cim_json_array(cim_text):
    rows = parse_cim_json(cim_text)
    assert len(rows) == 9
    assert rows[0]["DeviceName"] == "Intel(R) Wi-Fi 6 AX201 160MHz"


def test_parse_cim_json_single_object_shape():
    rows = parse_cim_json('{"DeviceName": "Only Driver"}')
    assert rows == [{"DeviceName": "Only Driver"}]


def test_parse_cim_json_empty():
    assert parse_cim_json("") == []
    assert parse_cim_json("   ") == []


def test_parse_pnputil_output(pnputil_text):
    entries = parse_pnputil_output(pnputil_text)
    assert len(entries) == 5
    wifi = entries[0]
    assert wifi["inf_name"] == "oem42.inf"
    assert wifi["original_name"] == "netwtw08.inf"
    assert wifi["provider"] == "Intel"
    assert wifi["device_class"] == "Net"
    # The packed "date version" field is split apart.
    assert wifi["driver_date_text"] == "03/15/2021"
    assert wifi["version"] == "22.40.0.7"
    assert "Hardware Compatibility" in wifi["signer"]


def test_parse_pnputil_ignores_banner_and_garbage():
    assert parse_pnputil_output("Microsoft PnP Utility\n\n") == []
    assert parse_pnputil_output("") == []


def test_collect_inventory_uses_exact_pinned_commands(cim_text, pnputil_text):
    runner = RecordingRunner(cim_text=cim_text, pnputil_text=pnputil_text)
    result = collect_inventory(runner=runner)

    assert len(result.cim_entries) == 9
    assert len(result.pnputil_entries) == 5
    assert result.errors == []

    # Exactly two commands were issued, in order, byte-for-byte the
    # pinned read-only invocations.
    assert len(runner.calls) == 2
    ps_call, pnp_call = runner.calls
    assert pnp_call == PNPUTIL_ENUM_ARGS == ["pnputil", "/enum-drivers"]
    assert ps_call[0] == "powershell"
    assert ps_call[1:4] == ["-NoProfile", "-NonInteractive", "-Command"]
    assert ps_call[4] == CIM_QUERY_SCRIPT
    assert "Get-CimInstance Win32_PnPSignedDriver" in CIM_QUERY_SCRIPT
    assert "ConvertTo-Json" in CIM_QUERY_SCRIPT


def test_collect_inventory_cim_failure_falls_back_to_pnputil(pnputil_text):
    runner = RecordingRunner(pnputil_text=pnputil_text, fail={"powershell"})
    result = collect_inventory(runner=runner)
    assert result.cim_entries == []
    assert len(result.pnputil_entries) == 5
    assert any("device query" in e for e in result.errors)


def test_collect_inventory_both_fail_is_designed_error():
    runner = RecordingRunner(fail={"powershell", "pnputil"})
    with pytest.raises(InventoryUnavailable) as excinfo:
        collect_inventory(runner=runner)
    # Plain language for the user, and no technical debris in it.
    message = excinfo.value.user_message
    assert "couldn't read this PC's driver list" in message
    assert "Traceback" not in message


def test_collect_inventory_pnputil_failure_still_returns_cim(cim_text):
    runner = RecordingRunner(cim_text=cim_text, fail={"pnputil"})
    result = collect_inventory(runner=runner)
    assert len(result.cim_entries) == 9
    assert result.pnputil_entries == []
    assert any("pnputil" in e for e in result.errors)
