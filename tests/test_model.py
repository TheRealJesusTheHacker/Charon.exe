"""Model helpers: version/date parsing and report serialization."""

import json
from datetime import date, datetime, timezone

from charon.model import (
    DeviceReport,
    InstalledDriver,
    ScanReport,
    UpdateCandidate,
    compare_versions,
    parse_driver_date,
    parse_version,
)


def test_parse_version_basic():
    assert parse_version("22.40.0.7") == (22, 40, 0, 7)
    assert parse_version("6.0.9239.1") == (6, 0, 9239, 1)
    assert parse_version(" 10.0.22621.2506 ") == (10, 0, 22621, 2506)


def test_parse_version_unparseable_is_none_not_zero():
    assert parse_version("") is None
    assert parse_version(None) is None
    assert parse_version("Unknown") is None


def test_compare_versions():
    assert compare_versions("23.60.1.2", "22.40.0.7") == 1
    assert compare_versions("19.3.4.219", "19.5.35.54") == -1
    assert compare_versions("31.0.15.3640", "31.0.15.3640") == 0
    # Trailing components pad with zeros.
    assert compare_versions("1.2", "1.2.0.0") == 0
    assert compare_versions("1.2.1", "1.2") == 1
    # Unparseable on either side → not comparable.
    assert compare_versions("", "1.0") is None
    assert compare_versions("1.0", "") is None


def test_parse_driver_date_shapes():
    assert parse_driver_date("/Date(1615766400000)/") == date(2021, 3, 15)
    assert parse_driver_date("20200619000000.000000+000") == date(2020, 6, 19)
    assert parse_driver_date("2023-06-14T07:00:00") == date(2023, 6, 14)
    assert parse_driver_date("2021-09-21T00:00:00-05:00") == date(2021, 9, 21)
    assert parse_driver_date("2022-11-02") == date(2022, 11, 2)
    assert parse_driver_date("03/15/2021") == date(2021, 3, 15)
    assert parse_driver_date(datetime(2024, 5, 6, 12, 30)) == date(2024, 5, 6)
    assert parse_driver_date(date(2020, 1, 30)) == date(2020, 1, 30)


def test_parse_driver_date_garbage_is_none():
    assert parse_driver_date("") is None
    assert parse_driver_date(None) is None
    assert parse_driver_date("someday") is None


def _report() -> ScanReport:
    wifi = InstalledDriver(
        device_name="Intel(R) Wi-Fi 6 AX201 160MHz",
        device_class="Net",
        version="22.40.0.7",
        driver_date=date(2021, 3, 15),
        is_signed=True,
    )
    upd = UpdateCandidate(
        title="Intel Net Driver Update",
        version="23.60.1.2",
        driver_date=date(2024, 5, 6),
    )
    widget = InstalledDriver(
        device_name="Contoso Test Widget", version="1.0.0.0", is_signed=False
    )
    return ScanReport(
        generated_at=datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc),
        computer="TEST-PC",
        sources=["device query"],
        devices=[
            DeviceReport(
                driver=wifi,
                status="update_available",
                update=upd,
                confidence="high",
                basis="hardware_id",
                explanation="newer available",
            ),
            DeviceReport(driver=widget, status="unknown", explanation="no word"),
        ],
    )


def test_report_counts_and_summary():
    report = _report()
    assert report.total == 2
    assert report.updates_available == 1
    assert report.up_to_date == 0
    assert report.unknown == 1
    assert report.unsigned == 1
    line = report.summary_line()
    assert "2 drivers checked" in line
    assert "1 with updates available" in line


def test_report_to_dict_is_json_serializable():
    data = _report().to_dict()
    text = json.dumps(data)  # must not raise
    assert data["computer"] == "TEST-PC"
    assert data["summary"] == {
        "total": 2,
        "updates_available": 1,
        "up_to_date": 0,
        "unknown": 1,
        "unsigned": 1,
    }
    first = data["devices"][0]
    assert first["status"] == "update_available"
    assert first["driver"]["driver_date"] == "2021-03-15"
    assert first["update"]["version"] == "23.60.1.2"
    assert data["devices"][1]["update"] is None
    assert "Z" in data["generated_at"] or "+00:00" in data["generated_at"]
    assert json.loads(text) == data
