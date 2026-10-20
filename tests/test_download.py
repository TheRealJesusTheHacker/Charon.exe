"""Phase 2 download staging: plan, revalidation, EULA, partial
failure, ledger, progress events, and the CLI end to end.

Everything runs against fake WUA seams on Linux. The behavioral
claim under test: staging downloads what a fresh search still
offers, reports every per-update verdict honestly, records EULA
acceptances, and installs nothing.
"""

from __future__ import annotations

import json

import pytest

from charon import cli
from charon.download import (
    EVENT_BATCH_COMPLETED,
    EVENT_UPDATE_COMPLETED,
    EVENT_UPDATE_PROGRESS,
    EVENT_UPDATE_STARTED,
    RESULT_NO_LONGER_AVAILABLE,
    ProgressEvent,
    append_run_to_ledger,
    build_download_plan,
    load_ledger,
    revalidate_plan,
    stage_plan,
    summarize_results,
)
from charon.model import (
    DeviceReport,
    InstalledDriver,
    ScanReport,
    UpdateCandidate,
)
from charon.wua import WUA_SEARCH_CRITERIA
from charon.wua_download import (
    ComUpdateDownloader,
    WuaDownloadUnavailable,
    download_updates,
)
from conftest import FakeComSession, FakeComUpdate, FakeSearcher


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _device(device_name, device_id, update):
    return DeviceReport(
        driver=InstalledDriver(
            device_name=device_name,
            device_id=device_id,
            version="1.0.0.0",
            hardware_ids=update.hardware_ids if update else (),
        ),
        status="update_available" if update else "up_to_date",
        update=update,
    )


def _report(*devices):
    from datetime import datetime

    return ScanReport(
        generated_at=datetime(2026, 10, 19, 12, 0),
        computer="TEST-PC",
        devices=list(devices),
    )


WIFI = UpdateCandidate(
    title="Intel Net Driver Update (23.60.1.2)",
    driver_model="Intel(R) Wi-Fi 6 AX201 160MHz",
    version="23.60.1.2",
    hardware_ids=("PCI\\VEN_8086&DEV_34F0",),
)
TOUCH = UpdateCandidate(
    title="Synaptics Mouse Driver Update (19.3.4.219)",
    driver_model="Synaptics TouchPad",
    version="19.3.4.219",
    hardware_ids=("ACPI\\SYN3602",),
)
BT = UpdateCandidate(
    title="Contoso Bluetooth Driver Update (7.1.0.0)",
    driver_model="Contoso Bluetooth Adapter",
    version="7.1.0.0",
    hardware_ids=("USB\\VID_1234&PID_5678",),
)


def _wifi_com(**kwargs):
    fields = {
        "title": WIFI.title,
        "driver_model": WIFI.driver_model,
        "version": WIFI.version,
        "hardware_ids": WIFI.hardware_ids,
    }
    fields.update(kwargs)
    return FakeComUpdate(**fields)


def _touch_com(**kwargs):
    fields = {
        "title": TOUCH.title,
        "driver_model": TOUCH.driver_model,
        "version": TOUCH.version,
        "hardware_ids": TOUCH.hardware_ids,
    }
    fields.update(kwargs)
    return FakeComUpdate(**fields)


def _stage(resolved, session, events):
    """Drive stage_plan with the real seam wrapped around the fake."""

    def fetch(objects, emit=None):
        return download_updates(
            objects, downloader=ComUpdateDownloader(session, collection_factory=session.make_collection), emit=emit
        )

    return stage_plan(resolved, fetch, emit=events.append)


# ----------------------------------------------------------------------
# Plan building
# ----------------------------------------------------------------------


def test_plan_takes_only_update_available():
    report = _report(
        _device("Wi-Fi", "dev-wifi", WIFI),
        _device("Display", "dev-gpu", None),  # up_to_date — not planned
        _device("TouchPad", "dev-touch", TOUCH),
    )
    plan = build_download_plan(report)
    assert [e.device_name for e in plan.entries] == ["Wi-Fi", "TouchPad"]
    assert plan.not_found == []


def test_plan_subset_selection_by_device_id_and_name():
    report = _report(
        _device("Wi-Fi", "dev-wifi", WIFI),
        _device("TouchPad", "dev-touch", TOUCH),
    )
    by_id = build_download_plan(report, ["dev-touch"])
    assert [e.device_name for e in by_id.entries] == ["TouchPad"]

    by_name = build_download_plan(report, ["wi-fi"])
    assert [e.device_name for e in by_name.entries] == ["Wi-Fi"]

    mixed = build_download_plan(report, ["dev-touch", "no-such-device"])
    assert [e.device_name for e in mixed.entries] == ["TouchPad"]
    assert mixed.not_found == ["no-such-device"]


def test_plan_from_full_fixture_report(cim_text, pnputil_text, wua_updates):
    from charon.cli import build_report
    from charon.platform_inventory import parse_cim_json, parse_pnputil_output

    report = build_report(
        parse_cim_json(cim_text),
        parse_pnputil_output(pnputil_text),
        wua_updates,
    )
    plan = build_download_plan(report)
    assert len(plan.entries) == 4  # exactly the scan's update_available


# ----------------------------------------------------------------------
# Revalidation
# ----------------------------------------------------------------------


def test_revalidate_resolves_against_fresh_objects():
    report = _report(_device("Wi-Fi", "dev-wifi", WIFI))
    plan = build_download_plan(report)
    com = _wifi_com()
    resolved = revalidate_plan(plan, [com.raw()])
    assert len(resolved) == 1
    assert resolved[0].available is True
    assert resolved[0].com_update is com  # the FRESH object, not a handle


def test_revalidate_superseded_update_downloads_nothing_in_its_place():
    report = _report(_device("Wi-Fi", "dev-wifi", WIFI))
    plan = build_download_plan(report)
    # Windows Update now offers a NEWER version under the same title:
    # a different offer — the scan's ticket must not silently upgrade.
    superseded = _wifi_com(version="24.0.0.1")
    resolved = revalidate_plan(plan, [superseded.raw()])
    assert resolved[0].available is False
    assert resolved[0].note.startswith("Windows Update no longer offers")
    assert "downloaded nothing in its place" in resolved[0].note


def test_revalidate_withdrawn_update_comes_back_unavailable():
    report = _report(
        _device("Wi-Fi", "dev-wifi", WIFI),
        _device("TouchPad", "dev-touch", TOUCH),
    )
    plan = build_download_plan(report)
    resolved = revalidate_plan(plan, [_wifi_com().raw()])
    by_name = {r.entry.device_name: r for r in resolved}
    assert by_name["Wi-Fi"].available is True
    assert by_name["TouchPad"].available is False


def test_revalidate_title_match_needs_hwid_overlap_when_both_name_them():
    report = _report(_device("Wi-Fi", "dev-wifi", WIFI))
    plan = build_download_plan(report)
    stranger = FakeComUpdate(
        title=WIFI.title,
        driver_model="Some other adapter",
        version=WIFI.version,
        hardware_ids=("PCI\\VEN_9999&DEV_0000",),
    )
    resolved = revalidate_plan(plan, [stranger.raw()])
    assert resolved[0].available is False


# ----------------------------------------------------------------------
# Staging through the real seam over fakes
# ----------------------------------------------------------------------


def test_stage_all_succeed_events_and_ledger(tmp_path):
    report = _report(
        _device("Wi-Fi", "dev-wifi", WIFI),
        _device("TouchPad", "dev-touch", TOUCH),
    )
    plan = build_download_plan(report)
    resolved = revalidate_plan(plan, [_wifi_com().raw(), _touch_com().raw()])
    session = FakeComSession()
    events: list[ProgressEvent] = []
    results = _stage(resolved, session, events)

    assert [r.status for r in results] == ["succeeded", "succeeded"]
    assert session.downloader_constructed == 1
    assert len(session.download_calls) == 1
    assert len(session.download_calls[0]) == 2  # one batch, both aboard

    kinds = [e.kind for e in events]
    assert kinds == [
        EVENT_UPDATE_STARTED,
        EVENT_UPDATE_STARTED,
        EVENT_UPDATE_COMPLETED,
        EVENT_UPDATE_COMPLETED,
        EVENT_BATCH_COMPLETED,
    ]
    completed = [e for e in events if e.kind == EVENT_UPDATE_COMPLETED]
    assert [e.device_name for e in completed] == ["Wi-Fi", "TouchPad"]
    batch = events[-1]
    assert "2 of 2 drivers downloaded" in batch.message
    assert "Nothing was installed" in batch.message

    ledger_path = tmp_path / "ledger.json"
    run = append_run_to_ledger(ledger_path, results, source="scan")
    assert run["counts"] == {
        "planned": 2,
        "downloaded": 2,
        "failed": 0,
        "no_longer_available": 0,
    }
    rows = run["entries"]
    assert rows[0]["update_title"] == WIFI.title
    assert rows[0]["result"] == "succeeded"
    assert rows[0]["downloaded_at"] is not None
    assert rows[0]["eula_accepted"] is False


def test_stage_one_failure_mid_batch_does_not_poison_the_rest():
    report = _report(
        _device("Wi-Fi", "dev-wifi", WIFI),
        _device("TouchPad", "dev-touch", TOUCH),
        _device("Bluetooth", "dev-bt", BT),
    )
    plan = build_download_plan(report)
    bt_com = FakeComUpdate(
        title=BT.title,
        driver_model=BT.driver_model,
        version=BT.version,
        hardware_ids=BT.hardware_ids,
    )
    resolved = revalidate_plan(
        plan, [_wifi_com().raw(), _touch_com().raw(), bt_com.raw()]
    )
    session = FakeComSession(codes_by_title={TOUCH.title: 4})  # failed
    events: list[ProgressEvent] = []
    results = _stage(resolved, session, events)

    by_name = {r.entry.device_name: r for r in results}
    assert by_name["Wi-Fi"].status == "succeeded"
    assert by_name["TouchPad"].status == "failed"
    assert by_name["TouchPad"].result_code == 4
    assert by_name["TouchPad"].hresult not in (0, None)
    assert by_name["Bluetooth"].status == "succeeded"
    # The failure is reported AS a failure — never as success.
    summary = summarize_results(results)
    assert "2 of 3 drivers downloaded" in summary
    assert "the TouchPad driver failed" in summary
    assert "Nothing was installed" in summary


def test_stage_succeeded_with_errors_is_not_called_plain_success():
    report = _report(_device("Wi-Fi", "dev-wifi", WIFI))
    plan = build_download_plan(report)
    resolved = revalidate_plan(plan, [_wifi_com().raw()])
    session = FakeComSession(codes_by_title={WIFI.title: 3})
    results = _stage(resolved, session, [])
    assert results[0].status == "succeeded_with_errors"
    assert results[0].staged is True
    assert "reported errors" in results[0].detail


def test_stage_claimed_success_but_not_downloaded_counts_as_failed():
    report = _report(_device("Wi-Fi", "dev-wifi", WIFI))
    plan = build_download_plan(report)
    com = _wifi_com(is_downloaded=False)  # WUA says 2, stages nothing
    resolved = revalidate_plan(plan, [com.raw()])
    results = _stage(resolved, FakeComSession(), [])
    assert results[0].status == "failed"
    assert "not staged" in results[0].detail


def test_stage_skips_gone_updates_and_records_them():
    report = _report(
        _device("Wi-Fi", "dev-wifi", WIFI),
        _device("TouchPad", "dev-touch", TOUCH),
    )
    plan = build_download_plan(report)
    resolved = revalidate_plan(plan, [_wifi_com().raw()])  # touch is gone
    session = FakeComSession()
    results = _stage(resolved, session, [])
    by_name = {r.entry.device_name: r for r in results}
    assert by_name["Wi-Fi"].status == "succeeded"
    assert by_name["TouchPad"].status == RESULT_NO_LONGER_AVAILABLE
    # The gone update was never handed to the downloader.
    assert len(session.download_calls) == 1
    assert [u.Title for u in session.download_calls[0]] == [WIFI.title]
    summary = summarize_results(results)
    assert "1 of 2 drivers downloaded" in summary


# ----------------------------------------------------------------------
# EULA handling
# ----------------------------------------------------------------------


def test_eula_acceptance_is_explicit_and_recorded(tmp_path):
    report = _report(_device("Wi-Fi", "dev-wifi", WIFI))
    plan = build_download_plan(report)
    com = _wifi_com(requires_eula=True)
    resolved = revalidate_plan(plan, [com.raw()])
    results = _stage(resolved, FakeComSession(), [])
    assert com.eula_accepted is True  # the seam accepted, out loud
    assert results[0].status == "succeeded"
    assert results[0].eula_accepted is True

    ledger_path = tmp_path / "ledger.json"
    run = append_run_to_ledger(ledger_path, results)
    assert run["entries"][0]["eula_accepted"] is True


def test_eula_acceptance_failure_fails_only_that_update():
    report = _report(
        _device("Wi-Fi", "dev-wifi", WIFI),
        _device("TouchPad", "dev-touch", TOUCH),
    )
    plan = build_download_plan(report)
    resolved = revalidate_plan(
        plan,
        [
            _wifi_com(requires_eula=True, accept_eula_raises=True).raw(),
            _touch_com().raw(),
        ],
    )
    results = _stage(resolved, FakeComSession(), [])
    by_name = {r.entry.device_name: r for r in results}
    assert by_name["Wi-Fi"].status == "failed"
    assert "license terms" in by_name["Wi-Fi"].detail
    assert by_name["TouchPad"].status == "succeeded"


# ----------------------------------------------------------------------
# Ledger persistence
# ----------------------------------------------------------------------


def test_ledger_write_and_reload_round_trip(tmp_path):
    report = _report(_device("Wi-Fi", "dev-wifi", WIFI))
    plan = build_download_plan(report)
    resolved = revalidate_plan(plan, [_wifi_com().raw()])
    results = _stage(resolved, FakeComSession(), [])

    path = tmp_path / "nested" / "ledger.json"
    append_run_to_ledger(path, results, source="report")
    append_run_to_ledger(path, results, source="scan")  # second run appends
    ledger = load_ledger(path)
    assert ledger["format"] == 1
    assert len(ledger["runs"]) == 2
    assert ledger["runs"][0]["source"] == "report"
    entry = ledger["runs"][0]["entries"][0]
    assert entry["device_id"] == "dev-wifi"
    assert entry["update_version"] == WIFI.version
    # No temp file is left behind after an atomic replace.
    assert not (tmp_path / "nested" / "ledger.json.tmp").exists()
    # The file on disk is valid JSON (it was replaced whole, not torn).
    json.loads(path.read_text(encoding="utf-8"))


def test_ledger_tolerates_garbage(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_ledger(path) == {"format": 1, "runs": []}


# ----------------------------------------------------------------------
# Seam contract edges
# ----------------------------------------------------------------------


def test_download_updates_off_windows_is_designed_failure(monkeypatch):
    import charon.wua_download as wd

    monkeypatch.setattr(wd.sys, "platform", "linux")
    with pytest.raises(WuaDownloadUnavailable) as excinfo:
        download_updates([object()], downloader=None)
    assert "Windows PC" in excinfo.value.user_message


def test_stage_rejects_short_result_list():
    report = _report(_device("Wi-Fi", "dev-wifi", WIFI))
    plan = build_download_plan(report)
    resolved = revalidate_plan(plan, [_wifi_com().raw()])

    def lying_fetch(objects, emit=None):
        return []

    with pytest.raises(ValueError):
        stage_plan(resolved, lying_fetch)


def test_progress_event_shape_supports_phase5():
    event = ProgressEvent(
        kind=EVENT_UPDATE_PROGRESS, title="X", percent=50
    )
    assert event.percent == 50
    started = ProgressEvent(kind=EVENT_UPDATE_STARTED, device_name="Wi-Fi")
    assert started.status == ""  # optional fields default empty


# ----------------------------------------------------------------------
# CLI end to end
# ----------------------------------------------------------------------


def _wire_download(monkeypatch, runner, searcher, session):
    from charon.platform_inventory import collect_inventory
    from charon.wua import search_driver_updates

    fake_runner = runner  # aliased: the lambda's own arg would shadow it
    monkeypatch.setattr(
        cli,
        "collect_inventory",
        lambda runner=None: collect_inventory(runner=fake_runner),
    )

    def fake_search(searcher_arg=None, keep_objects=False):
        return search_driver_updates(searcher, keep_objects=keep_objects)

    monkeypatch.setattr(cli, "search_driver_updates", fake_search)
    monkeypatch.setattr(
        cli,
        "download_updates",
        lambda objects, downloader=None, emit=None: download_updates(
            objects, downloader=ComUpdateDownloader(session, collection_factory=session.make_collection), emit=emit
        ),
    )


def test_cli_download_end_to_end(
    monkeypatch, tmp_path, capsys, cim_text, pnputil_text
):
    from conftest import RecordingRunner

    # Fresh search offers com-object-carrying copies of the fixture set.
    from conftest import load_json_fixture

    raw_updates = load_json_fixture("wua_updates.json")
    com_updates = [
        FakeComUpdate(
            title=u["title"],
            driver_model=u["driver_model"],
            driver_class=u["driver_class"],
            provider=u["provider"],
            version=u["version"],
            driver_date=u["driver_date"],
            hardware_ids=u["hardware_ids"],
        )
        for u in raw_updates
    ]
    searcher = FakeSearcher(
        updates=[c.raw() | {"com_update": c} for c in com_updates]
    )
    runner = RecordingRunner(cim_text=cim_text, pnputil_text=pnputil_text)
    session = FakeComSession()
    _wire_download(monkeypatch, runner, searcher, session)

    ledger = tmp_path / "ledger.json"
    code = cli.main(["download", "--ledger", str(ledger)])
    assert code == 0
    out = capsys.readouterr().out
    assert "nothing is installed" in out
    assert "Download plan: 4 driver(s) selected." in out
    assert "All 4 drivers downloaded and staged." in out
    assert "Nothing was installed" in out
    assert "ledger" in out.lower()

    # Revalidation ran: one scan search + one fresh search, both with
    # the pinned criteria; the second asked to keep the seam objects.
    assert searcher.criteria_seen == [WUA_SEARCH_CRITERIA, WUA_SEARCH_CRITERIA]
    assert searcher.keep_objects_seen == [False, True]
    assert session.downloader_constructed == 1
    assert len(session.download_calls[0]) == 4

    saved = load_ledger(ledger)
    assert len(saved["runs"]) == 1
    assert saved["runs"][0]["counts"]["downloaded"] == 4
    assert saved["runs"][0]["source"] == "scan"


def test_cli_download_from_report(
    monkeypatch, tmp_path, capsys, cim_text, pnputil_text, wua_updates
):
    from charon.cli import build_report
    from charon.platform_inventory import parse_cim_json, parse_pnputil_output
    from charon.wua import search_driver_updates as real_search

    report = build_report(
        parse_cim_json(cim_text),
        parse_pnputil_output(pnputil_text),
        wua_updates,
        computer="TEST-PC",
    )
    report_file = tmp_path / "scan.json"
    report_file.write_text(json.dumps(report.to_dict()), encoding="utf-8")

    wifi = next(u for u in wua_updates if "Wi-Fi" in u["driver_model"])
    com = FakeComUpdate(
        title=wifi["title"],
        driver_model=wifi["driver_model"],
        version=wifi["version"],
        hardware_ids=wifi["hardware_ids"],
    )
    searcher = FakeSearcher(updates=[com.raw()])
    session = FakeComSession()
    monkeypatch.setattr(
        cli,
        "search_driver_updates",
        lambda searcher_arg=None, keep_objects=False: real_search(
            searcher, keep_objects=keep_objects
        ),
    )
    monkeypatch.setattr(
        cli,
        "download_updates",
        lambda objects, downloader=None, emit=None: download_updates(
            objects, downloader=ComUpdateDownloader(session, collection_factory=session.make_collection), emit=emit
        ),
    )

    ledger = tmp_path / "ledger.json"
    code = cli.main(
        [
            "download",
            "--from-report",
            str(report_file),
            "--only",
            "Intel(R) Wi-Fi 6 AX201 160MHz",
            "--ledger",
            str(ledger),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "Scan report from" in out
    assert "Download plan: 1 driver(s) selected." in out
    assert "All 1 driver downloaded and staged." in out
    saved = load_ledger(ledger)
    assert saved["runs"][0]["source"] == "report"
    assert saved["runs"][0]["entries"][0]["device_name"] == (
        "Intel(R) Wi-Fi 6 AX201 160MHz"
    )


def test_cli_download_wua_down_at_revalidation_is_designed(
    monkeypatch, tmp_path, capsys, cim_text, pnputil_text, wua_updates
):
    from conftest import RecordingRunner

    runner = RecordingRunner(cim_text=cim_text, pnputil_text=pnputil_text)
    ok_searcher = FakeSearcher(updates=wua_updates)
    calls = {"n": 0}

    from charon.wua import search_driver_updates as real_search

    def two_faced(searcher_arg=None, keep_objects=False):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_search(ok_searcher)
        raise cli.WuaUnavailable(
            "Windows Update didn't answer.", detail="fake outage"
        )

    from charon.platform_inventory import collect_inventory

    fake_runner = runner  # aliased: the lambda's own arg would shadow it
    monkeypatch.setattr(
        cli,
        "collect_inventory",
        lambda runner=None: collect_inventory(runner=fake_runner),
    )
    monkeypatch.setattr(cli, "search_driver_updates", two_faced)
    code = cli.main(["download", "--ledger", str(tmp_path / "l.json")])
    assert code == cli.EXIT_DESIGNED_FAILURE == 2
    out = capsys.readouterr().out
    assert "nothing was downloaded" in out
    assert "Traceback" not in out
    assert not (tmp_path / "l.json").exists()  # no run, no ledger entry


def test_cli_download_nothing_available_is_clean_exit(
    monkeypatch, capsys, cim_text, pnputil_text
):
    from conftest import RecordingRunner

    runner = RecordingRunner(cim_text=cim_text, pnputil_text=pnputil_text)
    searcher = FakeSearcher(updates=[])  # WU offers nothing
    _wire_download(monkeypatch, runner, searcher, FakeComSession())
    code = cli.main(["download"])
    assert code == 0
    out = capsys.readouterr().out
    assert "Nothing to download" in out


# ----------------------------------------------------------------------
# Bug hunt #2 regressions: the seam fails in designed shapes only
# ----------------------------------------------------------------------


def test_downloader_setup_failure_is_designed_not_raw():
    """Building the collection/downloader used to sit outside every
    guard, so a COM failure there escaped as a raw exception (a
    traceback in the CLI) instead of WuaDownloadUnavailable."""

    def exploding_factory():
        raise RuntimeError("COM collection exploded")

    with pytest.raises(WuaDownloadUnavailable):
        ComUpdateDownloader(
            FakeComSession(), collection_factory=exploding_factory
        ).download([_wifi_com()])


def test_unreadable_verdict_fails_only_that_update():
    """One update whose GetUpdateResult raises used to sink the
    whole batch with a raw exception. Verdicts are read from the
    object Download() returns — that is the member patched here,
    because it is the only place the real API keeps them."""
    session = FakeComSession()
    real_create = session.CreateUpdateDownloader

    def create():
        downloader = real_create()
        real_download = downloader.Download

        def download():
            result = real_download()
            real_get = result.GetUpdateResult

            def wrapped(index):
                updates = list(downloader.Updates.items)
                if updates[index].Title == TOUCH.title:
                    raise RuntimeError("verdict exploded")
                return real_get(index)

            result.GetUpdateResult = wrapped
            return result

        downloader.Download = download
        return downloader

    session.CreateUpdateDownloader = create
    report = _report(
        _device("Wi-Fi", "dev-wifi", WIFI),
        _device("TouchPad", "dev-touch", TOUCH),
    )
    plan = build_download_plan(report)
    resolved = revalidate_plan(plan, [_wifi_com().raw(), _touch_com().raw()])
    results = _stage(resolved, session, [])
    by_name = {r.entry.device_name: r for r in results}
    assert by_name["Wi-Fi"].status == "succeeded"
    assert by_name["TouchPad"].status == "failed"
    assert "no verdict" in by_name["TouchPad"].detail


def test_eula_failed_update_is_not_sent_to_the_downloader():
    """An update whose terms couldn't be accepted used to ride the
    batch anyway. Windows Update is never asked to fetch it."""
    report = _report(
        _device("Wi-Fi", "dev-wifi", WIFI),
        _device("TouchPad", "dev-touch", TOUCH),
    )
    plan = build_download_plan(report)
    resolved = revalidate_plan(
        plan,
        [
            _wifi_com(requires_eula=True, accept_eula_raises=True).raw(),
            _touch_com().raw(),
        ],
    )
    session = FakeComSession()
    results = _stage(resolved, session, [])
    by_name = {r.entry.device_name: r for r in results}
    assert by_name["Wi-Fi"].status == "failed"
    assert by_name["TouchPad"].status == "succeeded"
    sent = [u.Title for call in session.download_calls for u in call]
    assert sent == [TOUCH.title]


def test_empty_batch_builds_no_downloader():
    session = FakeComSession()
    assert ComUpdateDownloader(session).download([]) == []
    assert session.downloader_constructed == 0
    assert session.download_calls == []


def test_absurd_download_sizes_never_crash_and_never_stick():
    for bad_size in (float("nan"), float("inf"), -5):
        com = _wifi_com(max_download_size=bad_size)
        session = FakeComSession()
        results = ComUpdateDownloader(
            session, collection_factory=session.make_collection
        ).download([com])
        assert results[0].status == "succeeded"
        assert results[0].size_bytes is None


def test_none_update_fails_alone():
    session = FakeComSession()
    results = ComUpdateDownloader(
        session, collection_factory=session.make_collection
    ).download([None, _touch_com()])
    assert results[0].status == "failed"
    assert results[1].status == "succeeded"


def test_stage_unknown_seam_status_counts_as_failed(tmp_path):
    """A seam status outside the ledger vocabulary used to pass
    through verbatim: counts stopped summing to the plan, and the
    summary falsely called the driver 'no longer offered'."""
    from types import SimpleNamespace

    report = _report(_device("Wi-Fi", "dev-wifi", WIFI))
    plan = build_download_plan(report)
    resolved = revalidate_plan(plan, [_wifi_com().raw()])

    def weird_fetch(objects, emit=None):
        return [
            SimpleNamespace(
                status="melted",
                detail="",
                eula_accepted=False,
                size_bytes=None,
                result_code=None,
                hresult=None,
            )
        ]

    results = stage_plan(resolved, weird_fetch)
    assert results[0].status == "failed"
    assert "unrecognized result" in results[0].detail
    summary = summarize_results(results)
    assert "the Wi-Fi driver failed" in summary
    run = append_run_to_ledger(tmp_path / "ledger.json", results)
    assert run["counts"]["planned"] == 1
    assert run["counts"]["failed"] == 1


# ----------------------------------------------------------------------
# Kit hunt 2026-10-20: the REAL WUA downloader shape
#
# Microsoft's documented IUpdateDownloader (wuapi.h): Download()
# takes NO argument and RETURNS an IDownloadResult; the per-update
# verdicts are read from THAT object via GetUpdateResult(index) —
# the downloader itself has no GetUpdateResult method, and the
# synchronous Download() has no progress-callback parameter at all
# (progress callbacks belong to the separate BeginDownload job API).
# The shared FakeComSession was built to the seam's mistaken shape
# (GetUpdateResult on the downloader, Download(sink)), so the whole
# suite stayed green while every real download's verdicts would be
# read from an object that cannot answer. These fakes model the
# documented API exactly, so the seam is now tested against the API
# Windows actually exposes.
# ----------------------------------------------------------------------


class _RealEntry:
    def __init__(self, code, hresult=0):
        self.ResultCode = code
        self.HResult = hresult


class _RealDownloadResult:
    """IDownloadResult: the object Download() returns."""

    def __init__(self, codes):
        self._codes = list(codes)

    def GetUpdateResult(self, index):
        return _RealEntry(self._codes[index])


class _RealDownloader:
    """IUpdateDownloader as documented: no GetUpdateResult here."""

    def __init__(self, codes):
        self.Updates = None
        self._codes = list(codes)
        self.download_calls = 0

    def Download(self):  # no parameters — the real signature
        self.download_calls += 1
        return _RealDownloadResult(self._codes)

    def __getattr__(self, name):
        # A real COM downloader answers an unknown member with an
        # error, never with a verdict.
        raise AttributeError(name)


def _real_collection():
    from conftest import _FakeCollection

    return _FakeCollection()


class _RealSession:
    """IUpdateSession as documented: searcher/downloader/installer
    factories only. There is deliberately NO collection member —
    a collection is its own coclass, handed to the seam through
    its collection_factory. Any other member request detonates."""

    def __init__(self, codes):
        self._codes = list(codes)
        self.downloader = None

    def CreateUpdateDownloader(self):
        self.downloader = _RealDownloader(self._codes)
        return self.downloader

    def __getattr__(self, name):
        raise AssertionError(f"unexpected session member requested: {name}")


def test_download_verdicts_come_from_the_download_result_object():
    """A successful real-shaped download must be reported staged."""
    session = _RealSession([2, 2])
    results = ComUpdateDownloader(
        session, collection_factory=_real_collection
    ).download([_wifi_com(), _touch_com()], emit=lambda event: None)
    assert [r.status for r in results] == ["succeeded", "succeeded"]
    assert [r.result_code for r in results] == [2, 2]
    assert session.downloader.download_calls == 1


def test_download_verdicts_real_shape_per_update_failure():
    session = _RealSession([2, 4])
    results = ComUpdateDownloader(
        session, collection_factory=_real_collection
    ).download([_wifi_com(), _touch_com()])
    by_status = [r.status for r in results]
    assert by_status == ["succeeded", "failed"]


def test_ledger_lock_contention_exhaustion_is_not_silent(
    tmp_path, monkeypatch, caplog
):
    """Kit hunt: on Windows, when the ledger lock cannot be taken in
    the retry budget, the writer used to fall through and write
    UNLOCKED with nothing in the log — the exact silent-race shape
    v1.0.1 fixed for the seed byte. Writing unlocked may stay (a lost
    race costs history, never safety), but it must be said out loud.
    The Windows branch is simulated: a fake msvcrt whose locking
    always refuses, on a platform patched to look like Windows."""
    import logging
    import sys
    import types

    from charon import download as download_mod

    fake_msvcrt = types.SimpleNamespace(
        LK_NBLCK=1,
        LK_UNLCK=2,
        locking=lambda fd, mode, nbytes: (_ for _ in ()).throw(
            OSError("locked by another writer")
        ),
    )
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(download_mod.time, "sleep", lambda seconds: None)
    path = tmp_path / "ledger.json"
    with caplog.at_level(logging.WARNING, logger="charon.download"):
        with download_mod._ledger_lock(path):
            pass
    assert any(
        "unlocked" in record.getMessage() for record in caplog.records
    ), "lock contention exhaustion proceeded unlocked in silence"
