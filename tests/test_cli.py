"""CLI: end-to-end scan over fakes, JSON export, designed failures."""

import json

import pytest

from charon import cli
from charon.platform_inventory import InventoryUnavailable
from conftest import FakeSearcher, RecordingRunner


def _wire(monkeypatch, runner, searcher):
    """Point cli's seam references at the real functions driven by fakes."""
    from charon.platform_inventory import collect_inventory
    from charon.wua import search_driver_updates

    fake_runner = runner
    fake_searcher = searcher
    monkeypatch.setattr(
        cli,
        "collect_inventory",
        lambda runner=None: collect_inventory(runner=fake_runner),
    )
    monkeypatch.setattr(
        cli,
        "search_driver_updates",
        lambda searcher=None: search_driver_updates(fake_searcher),
    )


def test_run_scan_end_to_end(monkeypatch, cim_text, pnputil_text, wua_updates):
    runner = RecordingRunner(cim_text=cim_text, pnputil_text=pnputil_text)
    searcher = FakeSearcher(updates=wua_updates)
    _wire(monkeypatch, runner, searcher)

    report = cli.run_scan()
    assert report.total == 9
    assert report.updates_available == 4
    assert report.up_to_date == 3
    assert report.unknown == 2
    assert report.unsigned == 1
    assert "Windows Update driver search" in report.sources


def test_run_scan_wua_down_degrades_to_unknown(monkeypatch, cim_text, pnputil_text):
    runner = RecordingRunner(cim_text=cim_text, pnputil_text=pnputil_text)
    searcher = FakeSearcher(error=RuntimeError("no WU today"))
    _wire(monkeypatch, runner, searcher)

    report = cli.run_scan()
    assert report.total == 9
    assert report.unknown == 9  # nobody gets marked outdated on a guess
    assert report.updates_available == 0
    assert any("never guesses" in w for w in report.warnings)


def test_render_report_is_plain_language(cim_text, pnputil_text, wua_updates):
    from charon.cli import build_report
    from charon.platform_inventory import parse_cim_json, parse_pnputil_output

    report = build_report(
        parse_cim_json(cim_text),
        parse_pnputil_output(pnputil_text),
        wua_updates,
        computer="TEST-PC",
        sources=["device query"],
    )
    text = cli.render_report(report)
    assert "CHARON.EXE" in text
    assert "UPDATE AVAILABLE" in text
    assert "Intel(R) Wi-Fi 6 AX201 160MHz" in text
    assert "23.60.1.2" in text
    assert "the toll is paid first" in text
    assert "Traceback" not in text


def test_scan_command_json_export(
    monkeypatch, tmp_path, capsys, cim_text, pnputil_text, wua_updates
):
    runner = RecordingRunner(cim_text=cim_text, pnputil_text=pnputil_text)
    searcher = FakeSearcher(updates=wua_updates)
    _wire(monkeypatch, runner, searcher)

    out_json = tmp_path / "report.json"
    code = cli.main(["scan", "--json", str(out_json)])
    assert code == 0
    printed = capsys.readouterr().out
    assert "Full report written to" in printed

    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert data["summary"]["total"] == 9
    assert data["summary"]["updates_available"] == 4
    names = {d["driver"]["device_name"] for d in data["devices"]}
    assert "Contoso Test Widget" in names


def test_scan_command_inventory_failure_is_designed(monkeypatch, capsys):
    def fail(runner=None):
        raise InventoryUnavailable(
            "Charon couldn't read this PC's driver list.",
            detail="secret technical detail",
        )

    monkeypatch.setattr(cli, "collect_inventory", fail)
    code = cli.main(["scan"])
    assert code == cli.EXIT_DESIGNED_FAILURE == 2
    out = capsys.readouterr().out
    assert "couldn't read this PC's driver list" in out
    assert "Traceback" not in out
    assert "secret technical detail" not in out  # detail goes to the log


def test_main_no_args_prints_help(capsys):
    assert cli.main([]) == 0
    assert "scan" in capsys.readouterr().out


def test_not_windows_runner_refuses_cleanly():
    # On this Linux test box, the real collect_inventory must produce
    # the designed not-Windows error rather than running anything.
    from charon.platform_inventory import collect_inventory

    if cli.sys.platform.startswith("win"):
        pytest.skip("test box is Windows")
    with pytest.raises(InventoryUnavailable) as excinfo:
        collect_inventory()
    assert "isn't running Windows" in excinfo.value.user_message



# ----------------------------------------------------------------------
# Bug hunt #2 regressions: CLI file errors are designed failures
# ----------------------------------------------------------------------


def test_scan_json_export_to_unwritable_path_is_designed(
    monkeypatch, tmp_path, capsys
):
    from datetime import datetime

    from charon.model import ScanReport

    monkeypatch.setattr(
        cli,
        "run_scan",
        lambda: ScanReport(generated_at=datetime(2026, 10, 19, 12, 0)),
    )
    # A directory as the --json target: write_text raises OSError.
    code = cli.main(["scan", "--json", str(tmp_path)])
    assert code == cli.EXIT_DESIGNED_FAILURE == 2
    out = capsys.readouterr().out
    assert "couldn't write the report file" in out
    assert "Traceback" not in out


def test_load_report_rejects_non_list_devices(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"devices": 5}), encoding="utf-8")
    with pytest.raises(InventoryUnavailable):
        cli.load_report(bad)
