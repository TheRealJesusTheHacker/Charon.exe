"""Phase 6 — the passenger manifest (B-5): a keepable export.

The manifest is the cargo record of the crossing: every driver the
scan found (exactly as the inventory seams reported it), plus —
when Charon's ledger has them — the download, toll, and apply
records of what was actually carried. Everything here runs against
the same fake seams the rest of the suite uses (RecordingRunner +
FakeSearcher), so manifest accuracy is proven against the real
inventory path, not against hand-built report objects alone.

Honesty pins, in the fleet's style:

- counts in the manifest are computed from the rows it carries —
  a manifest whose summary disagrees with its own rows is a lie;
- a scan that couldn't hear from every source produces a manifest
  MARKED not complete — never a partial manifest dressed as whole;
- a failed export raises a designed error and leaves no partial
  file at the destination (and never trashes a good earlier one).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from charon import cli
from charon.download import append_run_to_ledger, DownloadPlanEntry, StagedResult
from charon.manifest import (
    ManifestExportError,
    build_manifest,
    export_manifest,
    render_manifest_text,
    write_manifest,
)
from charon.model import (
    STATUS_UPDATE_AVAILABLE,
    DeviceReport,
    InstalledDriver,
    ScanReport,
    UpdateCandidate,
)
from charon.toll import TollRecord, record_toll
from conftest import FakeSearcher, RecordingRunner


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _report(*devices, warnings=()):
    return ScanReport(
        generated_at=datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc),
        computer="TEST-PC",
        sources=["device query (Win32_PnPSignedDriver)"],
        warnings=list(warnings),
        devices=list(devices),
    )


WIFI = DeviceReport(
    driver=InstalledDriver(
        device_name="Intel(R) Wi-Fi 6 AX201 160MHz",
        device_class="Net",
        provider="Intel",
        version="23.50.0.5",
        device_id="WIFI-1",
        inf_name="oem12.inf",
        is_signed=True,
        hardware_ids=("PCI\\VEN_8086&DEV_34F0",),
    ),
    status=STATUS_UPDATE_AVAILABLE,
    update=UpdateCandidate(
        title="Intel Net Driver Update (23.60.1.2)",
        version="23.60.1.2",
        hardware_ids=("PCI\\VEN_8086&DEV_34F0",),
    ),
    explanation="A newer Wi-Fi driver is available.",
)

CHIPSET = DeviceReport(
    driver=InstalledDriver(
        device_name="Chipset",
        device_class="System",
        provider="Intel",
        version="10.1.0.0",
        device_id="CHI-1",
        is_signed=False,
    ),
    status="up_to_date",
)


def _scan_report_via_fakes(monkeypatch, cim_text, pnputil_text, wua_updates):
    """A report built by the real scan path over the fake seams."""
    from charon.platform_inventory import collect_inventory
    from charon.wua import search_driver_updates

    fake_runner = RecordingRunner(
        cim_text=cim_text, pnputil_text=pnputil_text
    )
    fake_searcher = FakeSearcher(updates=wua_updates)
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
    return cli.run_scan()


# ----------------------------------------------------------------------
# Accuracy against the real inventory path
# ----------------------------------------------------------------------


def test_manifest_matches_the_real_scan_exactly(
    monkeypatch, cim_text, pnputil_text, wua_updates
):
    report = _scan_report_via_fakes(
        monkeypatch, cim_text, pnputil_text, wua_updates
    )
    manifest = build_manifest(report)
    data = manifest.to_dict()

    assert data["format"] == 1
    assert data["computer"] == report.computer
    assert data["summary"]["total"] == report.total == 9
    assert data["summary"]["updates_available"] == report.updates_available == 4
    assert data["summary"]["up_to_date"] == report.up_to_date == 3
    assert data["summary"]["unknown"] == report.unknown == 2
    # Every row the scan produced is in the manifest, byte-for-byte
    # the scan report's own serialization of that row — the manifest
    # may not paraphrase the cargo.
    assert data["devices"] == report.to_dict()["devices"]
    assert len(data["devices"]) == data["summary"]["total"]
    assert data["complete"] is True


def test_manifest_rows_carry_the_keepable_facts():
    manifest = build_manifest(_report(WIFI, CHIPSET))
    data = manifest.to_dict()
    wifi = data["devices"][0]["driver"]
    assert wifi["device_name"] == "Intel(R) Wi-Fi 6 AX201 160MHz"
    assert wifi["version"] == "23.50.0.5"
    assert wifi["provider"] == "Intel"
    assert wifi["inf_name"] == "oem12.inf"
    assert wifi["is_signed"] is True
    assert data["devices"][0]["update"]["version"] == "23.60.1.2"
    assert data["devices"][1]["driver"]["is_signed"] is False
    assert data["summary"]["unsigned"] == 1


# ----------------------------------------------------------------------
# The crossing record (from the ledger)
# ----------------------------------------------------------------------


def _ledger_with_a_crossing(tmp_path):
    path = tmp_path / "ledger.json"
    append_run_to_ledger(
        path,
        [
            StagedResult(
                entry=DownloadPlanEntry(
                    update=UpdateCandidate(
                        title="Intel Net Driver Update (23.60.1.2)",
                        version="23.60.1.2",
                    ),
                    device_name="Intel(R) Wi-Fi 6 AX201 160MHz",
                    device_id="WIFI-1",
                ),
                status="succeeded",
            )
        ],
    )
    now = datetime(2026, 10, 21, 12, 5, tzinfo=timezone.utc)
    record_toll(
        path,
        TollRecord(
            sequence_number=41,
            description="CHARON.EXE crossing checkpoint test",
            marker="test-marker",
            created_at=now,
            verified_at=now,
            computer="TEST-PC",
        ),
    )
    return path


def test_manifest_includes_the_crossing_record(tmp_path):
    ledger = _ledger_with_a_crossing(tmp_path)
    manifest = export_manifest(
        _report(WIFI), tmp_path / "manifest.json", ledger_path=ledger
    )
    data = manifest.to_dict()
    crossing = data["crossing"]
    assert crossing["recorded"] is True
    assert crossing["complete"] is True
    assert crossing["counts"] == {
        "downloaded": 1,
        "tolls_paid": 1,
        "apply_runs": 0,
        "applied": 0,
    }
    download = crossing["downloads"][0]
    assert download["device_name"] == "Intel(R) Wi-Fi 6 AX201 160MHz"
    assert download["result"] == "succeeded"
    assert crossing["tolls"][0]["sequence_number"] == 41

    text = render_manifest_text(manifest)
    assert "restore point #41" in text
    assert "Intel(R) Wi-Fi 6 AX201 160MHz" in text


def test_manifest_without_a_ledger_says_so_plainly(tmp_path):
    manifest = export_manifest(
        _report(WIFI),
        tmp_path / "manifest.json",
        ledger_path=tmp_path / "no-ledger.json",
    )
    data = manifest.to_dict()
    assert data["crossing"]["recorded"] is False
    assert data["crossing"]["complete"] is True
    text = render_manifest_text(manifest)
    assert "No crossings are recorded" in text


def test_manifest_with_an_unreadable_ledger_is_marked_not_complete(tmp_path):
    ledger = tmp_path / "ledger.json"
    ledger.write_text("{not json", encoding="utf-8")
    manifest = export_manifest(
        _report(WIFI), tmp_path / "manifest.json", ledger_path=ledger
    )
    data = manifest.to_dict()
    # The scan half is still whole; the crossing half is honestly
    # flagged — the manifest must not quietly drop the record and
    # call itself complete.
    assert data["complete"] is True
    assert data["crossing"]["complete"] is False
    assert any("ledger" in w.lower() for w in data["warnings"])
    text = render_manifest_text(manifest)
    assert "may be missing" in text


# ----------------------------------------------------------------------
# Completeness honesty
# ----------------------------------------------------------------------


def test_a_scan_with_warnings_marks_the_manifest_not_complete():
    report = _report(
        WIFI,
        warnings=[
            "Windows Update didn't answer, so nothing here is marked "
            "outdated — Charon never guesses."
        ],
    )
    manifest = build_manifest(report)
    data = manifest.to_dict()
    assert data["complete"] is False
    assert data["warnings"] == report.warnings
    text = render_manifest_text(manifest)
    assert "not complete" in text.lower()
    assert "never guesses" in text


def test_text_manifest_is_plain_language():
    manifest = build_manifest(_report(WIFI, CHIPSET))
    text = render_manifest_text(manifest)
    assert "CHARON.EXE — passenger manifest" in text
    assert "2 drivers checked" in text
    assert "UPDATE AVAILABLE" in text
    assert "23.60.1.2" in text
    assert "Traceback" not in text


# ----------------------------------------------------------------------
# Export: atomic, honest failures
# ----------------------------------------------------------------------


def test_write_manifest_json_round_trips(tmp_path):
    manifest = build_manifest(_report(WIFI, CHIPSET))
    out = tmp_path / "nested" / "manifest.json"
    returned = write_manifest(manifest, out)
    assert returned == out
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data == manifest.to_dict()
    # No temp sibling is left behind after an atomic replace.
    assert [p.name for p in out.parent.iterdir()] == ["manifest.json"]


def test_write_manifest_text(tmp_path):
    manifest = build_manifest(_report(WIFI))
    out = tmp_path / "manifest.txt"
    write_manifest(manifest, out, fmt="text")
    assert "passenger manifest" in out.read_text(encoding="utf-8")


def test_failed_export_leaves_no_partial_file(tmp_path):
    manifest = build_manifest(_report(WIFI))
    out = tmp_path / "manifest.json"
    out.mkdir()  # a directory where the file should go: replace fails
    with pytest.raises(ManifestExportError) as excinfo:
        write_manifest(manifest, out)
    assert excinfo.value.user_message  # plain language, safe to show
    assert out.is_dir()  # nothing partial dressed as a manifest
    assert list(tmp_path.iterdir()) == [out]  # and no temp litter


def test_failed_export_preserves_an_earlier_good_manifest(
    tmp_path, monkeypatch
):
    manifest = build_manifest(_report(WIFI))
    out = tmp_path / "manifest.json"
    write_manifest(manifest, out)
    good = out.read_bytes()

    import charon.manifest as manifest_module

    def exploding_replace(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(manifest_module.os, "replace", exploding_replace)
    with pytest.raises(ManifestExportError):
        write_manifest(manifest, out)
    assert out.read_bytes() == good  # the good copy was never touched


# ----------------------------------------------------------------------
# CLI end to end
# ----------------------------------------------------------------------


def _wire_scan(monkeypatch, runner, searcher):
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


def test_cli_manifest_end_to_end(
    monkeypatch, tmp_path, capsys, cim_text, pnputil_text, wua_updates
):
    runner = RecordingRunner(cim_text=cim_text, pnputil_text=pnputil_text)
    searcher = FakeSearcher(updates=wua_updates)
    _wire_scan(monkeypatch, runner, searcher)

    out = tmp_path / "keep" / "manifest.json"
    ledger = tmp_path / "ledger.json"
    code = cli.main(
        ["manifest", "--out", str(out), "--ledger", str(ledger)]
    )
    assert code == 0
    printed = capsys.readouterr().out
    assert "Passenger manifest written to" in printed
    assert "9 drivers checked" in printed
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["summary"]["total"] == 9
    assert data["devices"][0]["driver"]["device_name"]


def test_cli_manifest_from_report(
    monkeypatch, tmp_path, capsys, cim_text, pnputil_text, wua_updates
):
    report_file = tmp_path / "scan.json"
    report = cli.build_report(
        __import__(
            "charon.platform_inventory", fromlist=["parse_cim_json"]
        ).parse_cim_json(cim_text),
        __import__(
            "charon.platform_inventory", fromlist=["parse_pnputil_output"]
        ).parse_pnputil_output(pnputil_text),
        wua_updates,
        computer="TEST-PC",
    )
    report_file.write_text(json.dumps(report.to_dict()), encoding="utf-8")
    out = tmp_path / "manifest.json"
    code = cli.main(
        [
            "manifest",
            "--from-report",
            str(report_file),
            "--out",
            str(out),
            "--ledger",
            str(tmp_path / "ledger.json"),
        ]
    )
    assert code == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["summary"]["total"] == 9


def test_cli_manifest_without_out_is_a_designed_failure(capsys):
    code = cli.main(["manifest"])
    assert code == cli.EXIT_DESIGNED_FAILURE == 2
    out = capsys.readouterr().out
    assert "--out" in out
    assert "Traceback" not in out


def test_cli_manifest_write_failure_is_designed(
    monkeypatch, tmp_path, capsys, cim_text, pnputil_text, wua_updates
):
    runner = RecordingRunner(cim_text=cim_text, pnputil_text=pnputil_text)
    searcher = FakeSearcher(updates=wua_updates)
    _wire_scan(monkeypatch, runner, searcher)
    out = tmp_path / "manifest.json"
    out.mkdir()
    code = cli.main(
        ["manifest", "--out", str(out), "--ledger", str(tmp_path / "l.json")]
    )
    assert code == cli.EXIT_DESIGNED_FAILURE == 2
    printed = capsys.readouterr().out
    assert "couldn't write the passenger manifest" in printed
    assert "Traceback" not in printed
