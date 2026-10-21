"""THE CROSSING — the apply engine behind the fresh toll gate.

Every test drives ``charon.apply`` through fakes (a scripted toll
backend, the conftest searcher, a scripted installer), on Linux,
with no Windows in sight. The load-bearing cases:

- no toll / stale toll / forged toll → the gate refuses, and
  NOTHING is installed;
- the gate is re-run fresh inside the apply — a point deleted
  after payment closes it again;
- a staged set swapped before apply is caught by revalidation —
  the substitute is never installed;
- paperwork (license terms), per-update verdicts, stop-the-line,
  and restart reporting are all recorded, per driver, in the
  ledger.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from charon import apply as apply_mod
from charon import cli, toll
from charon.apply import (
    EVENT_APPLY_COMPLETED,
    EVENT_REBOOT_REQUIRED,
    apply_staged,
    load_staged_set,
    summarize_apply_results,
)
from charon.download import (
    DownloadPlanEntry,
    StagedResult,
    append_run_to_ledger,
    load_ledger,
)
from charon.model import UpdateCandidate
from charon.toll import (
    RestorePointInfo,
    TollProbe,
    TollRecord,
    pay_toll,
    record_toll,
)
from charon.wua_install import InstallOutcome, WuaInstallUnavailable
from conftest import FakeComUpdate, FakeSearcher

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _clean_session():
    toll.clear_session_toll()
    yield
    toll.clear_session_toll()


# ----------------------------------------------------------------------
# Fakes
# ----------------------------------------------------------------------


class FakeGateBackend:
    """The toll seam as the apply path sees it: probe + enumerate.

    Creation raises — the apply path must never create a point;
    it presents one at the gate or it doesn't cross.
    """

    def __init__(self, points=(), is_admin=True):
        self._points = list(points)
        self._probe = TollProbe(
            is_admin=is_admin, protection_enabled=True, system_drive="C:"
        )
        self.list_calls = 0
        self.probe_calls = 0

    def probe(self):
        self.probe_calls += 1
        return self._probe

    def create_restore_point(self, description):
        raise AssertionError("the apply path must never create a point")

    def list_restore_points(self):
        self.list_calls += 1
        return list(self._points)

    def drop_all(self):
        self._points = []


class FullFakeBackend(FakeGateBackend):
    """A gate backend that can also pay (for freshness tests)."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._seq = 41

    def create_restore_point(self, description):
        self._points.append(
            RestorePointInfo(
                sequence_number=self._seq,
                description=description,
                created_at=datetime.now(timezone.utc),
                restore_point_type=12,
            )
        )
        self._seq += 1


class FakeInstaller:
    """Scripted install seam: outcomes by title, records every call."""

    def __init__(self, outcomes=None, raises=None):
        self.outcomes = dict(outcomes or {})
        self.raises = dict(raises or {})
        self.calls: list = []

    def install_one(self, update, emit=None):
        self.calls.append(update)
        if update.Title in self.raises:
            raise self.raises[update.Title]
        return self.outcomes.get(
            update.Title,
            InstallOutcome(status="succeeded", result_code=2, hresult=0),
        )


def ok_verify(entries, candidate):
    return True, ""


# ----------------------------------------------------------------------
# Ledger staging helpers
# ----------------------------------------------------------------------


def stage(ledger, *specs):
    """Write one download run's staged rows into the ledger.

    Each spec: (device_name, device_id, title, version[, eula]).
    """
    results = []
    for spec in specs:
        device_name, device_id, title, version = spec[:4]
        eula = spec[4] if len(spec) > 4 else False
        results.append(
            StagedResult(
                entry=DownloadPlanEntry(
                    update=UpdateCandidate(title=title, version=version),
                    device_name=device_name,
                    device_id=device_id,
                ),
                status="succeeded",
                eula_accepted=eula,
            )
        )
    append_run_to_ledger(ledger, results)
    return ledger


def record_and_point(ledger, created=NOW, seq=41, marker=None):
    """A toll on record AND its matching point on the 'PC'."""
    marker = marker if marker is not None else "crossing #cafe"
    record = TollRecord(
        sequence_number=seq,
        description=f"CHARON.EXE crossing checkpoint {marker}",
        marker=marker,
        created_at=created,
        verified_at=created,
        computer="test-pc",
    )
    record_toll(ledger, record)
    point = RestorePointInfo(
        sequence_number=seq,
        description=record.description,
        created_at=created,
        restore_point_type=12,
    )
    return record, point


def two_drivers(tmp_path):
    ledger = tmp_path / "ledger.json"
    stage(
        ledger,
        ("Net Device", "NET-1", "Net Driver", "2.0"),
        ("Audio Device", "AUD-1", "Audio Driver", "3.1"),
    )
    return ledger


def fresh_searcher(*updates):
    return FakeSearcher(updates=[u.raw() for u in updates])


def com(title, version, **kwargs):
    return FakeComUpdate(title=title, version=version, **kwargs)


# ----------------------------------------------------------------------
# The happy crossing
# ----------------------------------------------------------------------


def test_apply_happy_path_crosses_in_order_and_ledgers(tmp_path):
    ledger = two_drivers(tmp_path)
    record, point = record_and_point(ledger)
    backend = FakeGateBackend(points=[point])
    installer = FakeInstaller()
    events = []
    results, toll_record = apply_staged(
        ledger,
        toll_backend=backend,
        searcher=fresh_searcher(com("Net Driver", "2.0"), com("Audio Driver", "3.1")),
        installer=installer,
        verify=ok_verify,
        emit=events.append,
    )
    assert toll_record == record
    assert [r.status for r in results] == ["succeeded", "succeeded"]
    assert [u.Title for u in installer.calls] == ["Net Driver", "Audio Driver"]
    assert backend.list_calls >= 1  # the gate enumerated, fresh, in-run
    kinds = [e.kind for e in events]
    assert kinds[-1] == EVENT_APPLY_COMPLETED

    data = json.loads(ledger.read_text(encoding="utf-8"))
    assert len(data["apply_runs"]) == 1
    run = data["apply_runs"][0]
    assert run["toll_sequence"] == 41
    assert run["counts"]["planned"] == 2
    assert run["counts"]["applied"] == 2
    rows = {row["device_name"]: row for row in run["entries"]}
    assert rows["Net Device"]["result"] == "succeeded"
    assert rows["Net Device"]["applied_at"] is not None
    assert rows["Audio Device"]["result_code"] == 2


def test_nothing_staged_returns_empty(tmp_path):
    results, toll_record = apply_staged(
        tmp_path / "ledger.json",
        toll_backend=FakeGateBackend(),
        searcher=fresh_searcher(),
        installer=FakeInstaller(),
        verify=ok_verify,
    )
    assert results == []
    assert toll_record is None


def test_already_applied_is_never_re_crossed(tmp_path):
    ledger = two_drivers(tmp_path)
    _record, point = record_and_point(ledger)
    backend = FakeGateBackend(points=[point])
    searcher = fresh_searcher(com("Net Driver", "2.0"), com("Audio Driver", "3.1"))
    first_installer = FakeInstaller()
    apply_staged(
        ledger,
        toll_backend=backend,
        searcher=searcher,
        installer=first_installer,
        verify=ok_verify,
    )
    assert len(first_installer.calls) == 2
    # A second crossing over the same ledger finds the ferry empty.
    assert load_staged_set(load_ledger(ledger)) == []
    second_installer = FakeInstaller()
    results, _ = apply_staged(
        ledger,
        toll_backend=backend,
        searcher=searcher,
        installer=second_installer,
        verify=ok_verify,
    )
    assert results == []
    assert second_installer.calls == []


def test_two_devices_ride_one_package_one_crossing(tmp_path):
    ledger = tmp_path / "ledger.json"
    stage(
        ledger,
        ("Laptop Net", "NET-1", "Shared Driver", "2.0"),
        ("Desktop Net", "NET-2", "Shared Driver", "2.0"),
    )
    _record, point = record_and_point(ledger)
    installer = FakeInstaller()
    results, _ = apply_staged(
        ledger,
        toll_backend=FakeGateBackend(points=[point]),
        searcher=fresh_searcher(com("Shared Driver", "2.0")),
        installer=installer,
        verify=ok_verify,
    )
    assert len(installer.calls) == 1  # one package, one crossing
    assert [r.status for r in results] == ["succeeded", "succeeded"]


# ----------------------------------------------------------------------
# The gate — refused crossings install NOTHING
# ----------------------------------------------------------------------


def test_apply_refused_without_any_toll(tmp_path):
    ledger = two_drivers(tmp_path)
    installer = FakeInstaller()
    with pytest.raises(toll.TollUnpaid):
        apply_staged(
            ledger,
            toll_backend=FakeGateBackend(points=[]),
            searcher=fresh_searcher(com("Net Driver", "2.0")),
            installer=installer,
            verify=ok_verify,
        )
    assert installer.calls == []
    assert "apply_runs" not in load_ledger(ledger)


def test_apply_refused_when_the_point_was_deleted(tmp_path):
    """Stale proof: the record is on paper, the point is gone."""
    ledger = two_drivers(tmp_path)
    record_and_point(ledger)
    installer = FakeInstaller()
    with pytest.raises(toll.TollUnpaid) as excinfo:
        apply_staged(
            ledger,
            toll_backend=FakeGateBackend(points=[]),  # deleted
            searcher=fresh_searcher(com("Net Driver", "2.0")),
            installer=installer,
            verify=ok_verify,
        )
    assert "can't be found" in excinfo.value.user_message
    assert installer.calls == []


def test_apply_refused_on_forged_record_with_empty_marker(tmp_path):
    ledger = two_drivers(tmp_path)
    record, _point = record_and_point(ledger, marker="")
    # A point that 'matches' everything — because the marker is empty.
    forged_point = RestorePointInfo(
        sequence_number=41,
        description="some other restore point entirely",
        created_at=NOW,
        restore_point_type=12,
    )
    installer = FakeInstaller()
    with pytest.raises(toll.TollUnpaid):
        apply_staged(
            ledger,
            toll_backend=FakeGateBackend(points=[forged_point]),
            searcher=fresh_searcher(com("Net Driver", "2.0")),
            installer=installer,
            verify=ok_verify,
        )
    assert installer.calls == []


def test_apply_refused_on_point_swapped_by_time(tmp_path):
    """Same sequence, same marker — but stamped an hour before the
    record claims. The timestamp leg of the gate catches the swap."""
    ledger = two_drivers(tmp_path)
    record, _point = record_and_point(ledger)
    swapped = RestorePointInfo(
        sequence_number=41,
        description=record.description,
        created_at=NOW - timedelta(hours=1),
        restore_point_type=12,
    )
    installer = FakeInstaller()
    with pytest.raises(toll.TollUnpaid):
        apply_staged(
            ledger,
            toll_backend=FakeGateBackend(points=[swapped]),
            searcher=fresh_searcher(com("Net Driver", "2.0")),
            installer=installer,
            verify=ok_verify,
        )
    assert installer.calls == []


def test_gate_is_fresh_not_a_session_cache(tmp_path):
    """Pay the toll for real, then delete the point: apply must
    still refuse — the session's 'paid' memory is never trusted."""
    ledger = two_drivers(tmp_path)
    backend = FullFakeBackend()
    pay_toll(backend, ledger_path=ledger, sleep=lambda _s: None)
    assert toll.session_toll() is not None
    backend.drop_all()  # the point vanishes after payment
    installer = FakeInstaller()
    with pytest.raises(toll.TollUnpaid):
        apply_staged(
            ledger,
            toll_backend=backend,
            searcher=fresh_searcher(com("Net Driver", "2.0")),
            installer=installer,
            verify=ok_verify,
        )
    assert backend.list_calls >= 2  # payment's checks + the gate's own
    assert installer.calls == []


class DropAfterFirstInstall(FakeInstaller):
    """An installer whose first crossing makes the point vanish —
    as Windows itself can, pruning the oldest restore points
    when disk space runs short mid-run."""

    def __init__(self, backend, **kwargs):
        super().__init__(**kwargs)
        self._backend = backend

    def install_one(self, update, emit=None):
        outcome = super().install_one(update, emit=emit)
        self._backend.drop_all()
        return outcome


def test_point_vanishing_mid_crossing_stops_the_line(tmp_path):
    """Sweep #3: the gate is re-proved before EVERY install. The
    point dies after the first driver crosses; the second driver
    must never be installed, and both verdicts are recorded."""
    ledger = two_drivers(tmp_path)
    _record, point = record_and_point(ledger)
    backend = FakeGateBackend(points=[point])
    installer = DropAfterFirstInstall(backend)
    results, _ = apply_staged(
        ledger,
        toll_backend=backend,
        searcher=fresh_searcher(com("Net Driver", "2.0"), com("Audio Driver", "3.1")),
        installer=installer,
        verify=ok_verify,
    )
    assert [u.Title for u in installer.calls] == ["Net Driver"]
    assert [r.status for r in results] == ["succeeded", "skipped"]
    assert "restore point could no longer be confirmed" in results[1].detail
    data = json.loads(ledger.read_text(encoding="utf-8"))
    run = data["apply_runs"][0]
    assert run["counts"]["applied"] == 1
    assert run["counts"]["skipped"] == 1


class GateOnlyBackend(FakeGateBackend):
    """Answers the gate's first enumeration, then the point is
    gone — deleted in the breath between the gate and the first
    install."""

    def list_restore_points(self):
        points = super().list_restore_points()
        self._points = []
        return points


def test_point_vanishing_between_gate_and_first_install(tmp_path):
    """Sweep #3 probe: delete the point between the gate and the
    first install — zero installs, every driver held, no raise:
    the run's record says the crossing stopped at the gate."""
    ledger = two_drivers(tmp_path)
    _record, point = record_and_point(ledger)
    backend = GateOnlyBackend(points=[point])
    installer = FakeInstaller()
    results, _ = apply_staged(
        ledger,
        toll_backend=backend,
        searcher=fresh_searcher(com("Net Driver", "2.0"), com("Audio Driver", "3.1")),
        installer=installer,
        verify=ok_verify,
    )
    assert installer.calls == []
    assert [r.status for r in results] == ["skipped", "skipped"]
    assert "restore point could no longer be confirmed" in results[0].detail


def test_verify_judges_first_name_match_when_no_id(monkeypatch):
    """Sweep #3: the post-crossing check judges the device that
    crossed. With no device ID to go on and two devices sharing
    a name, the FIRST namesake is judged — letting the scan run
    on to the last one would judge this crossing by a different
    device's version and could pass a driver that never landed."""
    from charon.apply import StagedEntry, _default_verify
    from charon.platform_inventory import InventoryResult

    staged = [
        StagedEntry(
            device_id="",
            device_name="Twin Device",
            update_title="Twin Driver",
            update_version="2.0",
            eula_accepted=True,
        )
    ]
    candidate = UpdateCandidate(title="Twin Driver", version="2.0")

    def fake_collect(runner=None):
        return InventoryResult(
            cim_entries=[
                {
                    "DeviceName": "Twin Device",
                    "DeviceID": "",
                    "DriverVersion": "1.0",
                },
                {
                    "DeviceName": "Twin Device",
                    "DeviceID": "",
                    "DriverVersion": "2.0",
                },
            ],
            pnputil_entries=[],
        )

    monkeypatch.setattr(apply_mod, "collect_inventory", fake_collect)
    verify = _default_verify(fresh_searcher(), None)
    healthy, note = verify(staged, candidate)
    assert healthy is False
    assert "still reports the older driver version 1.0" in note


def test_not_admin_is_refused_before_any_search(tmp_path):
    ledger = two_drivers(tmp_path)
    record_and_point(ledger)
    searcher = fresh_searcher(com("Net Driver", "2.0"))
    installer = FakeInstaller()
    with pytest.raises(toll.TollAdminRequired):
        apply_staged(
            ledger,
            toll_backend=FakeGateBackend(points=[], is_admin=False),
            searcher=searcher,
            installer=installer,
            verify=ok_verify,
        )
    assert installer.calls == []
    assert searcher.criteria_seen == []


# ----------------------------------------------------------------------
# Revalidation at apply time (the TOCTOU answers)
# ----------------------------------------------------------------------


def test_swapped_staged_set_is_caught_and_never_crossed(tmp_path):
    """Staged v2.0; Windows Update now offers v3.0 under the same
    title. The substitute is a different ticket: not crossed."""
    ledger = tmp_path / "ledger.json"
    stage(ledger, ("Net Device", "NET-1", "Net Driver", "2.0"))
    record_and_point(ledger)
    substitute = com("Net Driver", "3.0")
    installer = FakeInstaller()
    results, _ = apply_staged(
        ledger,
        toll_backend=FakeGateBackend(points=[_point_for(ledger)]),
        searcher=fresh_searcher(substitute),
        installer=installer,
        verify=ok_verify,
    )
    assert [r.status for r in results] == ["no_longer_available"]
    assert installer.calls == []
    assert substitute.IsInstalled is False
    data = json.loads(ledger.read_text(encoding="utf-8"))
    assert data["apply_runs"][0]["entries"][0]["result"] == "no_longer_available"


def test_withdrawn_update_skips_and_the_rest_still_cross(tmp_path):
    ledger = two_drivers(tmp_path)
    _record, point = record_and_point(ledger)
    installer = FakeInstaller()
    results, _ = apply_staged(
        ledger,
        toll_backend=FakeGateBackend(points=[point]),
        searcher=fresh_searcher(com("Audio Driver", "3.1")),  # Net gone
        installer=installer,
        verify=ok_verify,
    )
    assert [r.status for r in results] == ["no_longer_available", "succeeded"]
    assert [u.Title for u in installer.calls] == ["Audio Driver"]


def test_eula_not_accepted_blocks_only_that_driver(tmp_path):
    ledger = tmp_path / "ledger.json"
    stage(
        ledger,
        ("Net Device", "NET-1", "Net Driver", "2.0", False),
        ("Audio Device", "AUD-1", "Audio Driver", "3.1", False),
    )
    record_and_point(ledger)
    gated = com("Net Driver", "2.0", requires_eula=True)
    plain = com("Audio Driver", "3.1")
    installer = FakeInstaller()
    results, _ = apply_staged(
        ledger,
        toll_backend=FakeGateBackend(
            points=[_point_for(ledger)]
        ),
        searcher=fresh_searcher(gated, plain),
        installer=installer,
        verify=ok_verify,
    )
    assert [r.status for r in results] == ["eula_not_accepted", "succeeded"]
    assert [u.Title for u in installer.calls] == ["Audio Driver"]
    assert gated.IsInstalled is False


def _point_for(ledger):
    """Rebuild the enumeration point matching the ledger's toll."""
    record = toll.latest_toll(ledger)
    return RestorePointInfo(
        sequence_number=record.sequence_number,
        description=record.description,
        created_at=record.created_at,
        restore_point_type=12,
    )


def test_staged_copy_gone_from_cache_fails_that_driver(tmp_path):
    ledger = tmp_path / "ledger.json"
    stage(ledger, ("Net Device", "NET-1", "Net Driver", "2.0"))
    record_and_point(ledger)
    installer = FakeInstaller()
    results, _ = apply_staged(
        ledger,
        toll_backend=FakeGateBackend(points=[_point_for(ledger)]),
        searcher=fresh_searcher(com("Net Driver", "2.0", is_downloaded=False)),
        installer=installer,
        verify=ok_verify,
    )
    assert [r.status for r in results] == ["failed"]
    assert "no longer in the Windows Update cache" in results[0].detail
    assert installer.calls == []


# ----------------------------------------------------------------------
# Verdicts, the line, and the restart
# ----------------------------------------------------------------------


def test_failure_stops_the_line(tmp_path):
    ledger = two_drivers(tmp_path)
    record_and_point(ledger)
    installer = FakeInstaller(
        outcomes={
            "Net Driver": InstallOutcome(
                status="failed", result_code=4, hresult=-1, detail="boom"
            )
        }
    )
    results, _ = apply_staged(
        ledger,
        toll_backend=FakeGateBackend(points=[_point_for(ledger)]),
        searcher=fresh_searcher(com("Net Driver", "2.0"), com("Audio Driver", "3.1")),
        installer=installer,
        verify=ok_verify,
    )
    assert [r.status for r in results] == ["failed", "skipped"]
    assert "Held back" in results[1].detail
    assert len(installer.calls) == 1


def test_unhealthy_device_stops_the_line_and_names_the_way_back(tmp_path):
    ledger = two_drivers(tmp_path)
    record, _point = record_and_point(ledger)

    def sick_verify(entries, candidate):
        return False, "the Net Device still reports the older version."

    installer = FakeInstaller()
    results, toll_record = apply_staged(
        ledger,
        toll_backend=FakeGateBackend(points=[_point_for(ledger)]),
        searcher=fresh_searcher(com("Net Driver", "2.0"), com("Audio Driver", "3.1")),
        installer=installer,
        verify=sick_verify,
    )
    assert [r.status for r in results] == ["failed", "skipped"]
    assert "device check failed" in results[0].detail
    summary = summarize_apply_results(results, toll_record)
    assert "restore point #41" in summary
    assert len(installer.calls) == 1


def test_succeeded_with_errors_crosses_and_the_line_continues(tmp_path):
    ledger = two_drivers(tmp_path)
    record_and_point(ledger)
    installer = FakeInstaller(
        outcomes={
            "Net Driver": InstallOutcome(
                status="succeeded_with_errors",
                result_code=3,
                hresult=0,
                detail="installed, with errors reported",
            )
        }
    )
    results, _ = apply_staged(
        ledger,
        toll_backend=FakeGateBackend(points=[_point_for(ledger)]),
        searcher=fresh_searcher(com("Net Driver", "2.0"), com("Audio Driver", "3.1")),
        installer=installer,
        verify=ok_verify,
    )
    assert [r.status for r in results] == ["succeeded_with_errors", "succeeded"]


def test_reboot_required_is_reported_never_forced(tmp_path):
    ledger = tmp_path / "ledger.json"
    stage(ledger, ("Net Device", "NET-1", "Net Driver", "2.0"))
    record_and_point(ledger)
    events = []
    installer = FakeInstaller(
        outcomes={
            "Net Driver": InstallOutcome(
                status="succeeded", result_code=2, hresult=0,
                reboot_required=True,
            )
        }
    )
    results, record = apply_staged(
        ledger,
        toll_backend=FakeGateBackend(points=[_point_for(ledger)]),
        searcher=fresh_searcher(com("Net Driver", "2.0")),
        installer=installer,
        verify=ok_verify,
        emit=events.append,
    )
    assert results[0].reboot_required is True
    assert EVENT_REBOOT_REQUIRED in [e.kind for e in events]
    summary = summarize_apply_results(results, record)
    assert "restart is needed" in summary.lower()
    assert "never restarts" in summary
    data = json.loads(ledger.read_text(encoding="utf-8"))
    assert data["apply_runs"][0]["entries"][0]["reboot_required"] is True
    assert data["apply_runs"][0]["counts"]["reboot_required"] == 1


def test_install_that_cannot_begin_still_records_partial_truth(tmp_path):
    ledger = two_drivers(tmp_path)
    record_and_point(ledger)
    installer = FakeInstaller(
        raises={
            "Audio Driver": WuaInstallUnavailable(
                "Windows Update couldn't start the install. "
                "Nothing was installed.",
                detail="fake",
            )
        }
    )
    with pytest.raises(WuaInstallUnavailable):
        apply_staged(
            ledger,
            toll_backend=FakeGateBackend(points=[_point_for(ledger)]),
            searcher=fresh_searcher(
                com("Net Driver", "2.0"), com("Audio Driver", "3.1")
            ),
            installer=installer,
            verify=ok_verify,
        )
    data = json.loads(ledger.read_text(encoding="utf-8"))
    rows = {r["device_name"]: r for r in data["apply_runs"][0]["entries"]}
    assert rows["Net Device"]["result"] == "succeeded"
    assert rows["Audio Device"]["result"] == "failed"


def test_default_verify_catches_an_install_that_did_not_land(tmp_path):
    """No verify injected: the standard check asks Windows Update
    again — the update is STILL offered as not-installed, so the
    crossing counts as failed even though the installer smiled."""
    ledger = tmp_path / "ledger.json"
    stage(ledger, ("Net Device", "NET-1", "Net Driver", "2.0"))
    record_and_point(ledger)
    installer = FakeInstaller()
    results, _ = apply_staged(
        ledger,
        toll_backend=FakeGateBackend(points=[_point_for(ledger)]),
        searcher=fresh_searcher(com("Net Driver", "2.0")),
        installer=installer,
        # verify intentionally NOT injected
    )
    assert [r.status for r in results] == ["failed"]
    assert "device check failed" in results[0].detail


# ----------------------------------------------------------------------
# Crafted-ledger robustness (the audit's own regressions)
# ----------------------------------------------------------------------


def test_crafted_ledger_rows_neither_crash_nor_open_doors(tmp_path):
    """A hand-edited ledger: a row whose result is a list must be
    ignored quietly (not crash the load), and a row whose
    eula_accepted is the STRING "false" must not count as accepted
    paperwork — the crossing treats it as unaccepted."""
    ledger = tmp_path / "ledger.json"
    ledger.write_text(
        json.dumps(
            {
                "format": 1,
                "runs": [
                    {
                        "entries": [
                            {"result": ["succeeded"], "device_name": "Ghost"},
                            {
                                "result": "succeeded",
                                "device_id": "NET-1",
                                "device_name": "Net Device",
                                "update_title": "Net Driver",
                                "update_version": "2.0",
                                "eula_accepted": "false",
                            },
                        ]
                    }
                ],
                "tolls": [],
            }
        ),
        encoding="utf-8",
    )
    staged = load_staged_set(load_ledger(ledger))
    assert [e.device_name for e in staged] == ["Net Device"]
    assert staged[0].eula_accepted is False

    record_and_point(ledger)
    gated = com("Net Driver", "2.0", requires_eula=True)
    installer = FakeInstaller()
    results, _ = apply_staged(
        ledger,
        toll_backend=FakeGateBackend(points=[_point_for(ledger)]),
        searcher=fresh_searcher(gated),
        installer=installer,
        verify=ok_verify,
    )
    assert [r.status for r in results] == ["eula_not_accepted"]
    assert installer.calls == []


# ----------------------------------------------------------------------
# The CLI surface
# ----------------------------------------------------------------------


def test_cli_apply_end_to_end(monkeypatch, tmp_path, capsys):
    ledger = two_drivers(tmp_path)
    record_and_point(ledger)
    backend = FakeGateBackend(points=[_point_for(ledger)])
    searcher = fresh_searcher(com("Net Driver", "2.0"), com("Audio Driver", "3.1"))
    installer = FakeInstaller()
    real = apply_mod.apply_staged
    monkeypatch.setattr(
        cli,
        "apply_staged",
        lambda path=None, **kw: real(
            path,
            toll_backend=backend,
            searcher=searcher,
            installer=installer,
            verify=ok_verify,
        ),
    )
    code = cli.main(["apply", "--ledger", str(ledger)])
    out = capsys.readouterr().out
    assert code == 0
    assert "crossed" in out


def test_cli_apply_refused_at_the_gate(monkeypatch, tmp_path, capsys):
    ledger = two_drivers(tmp_path)  # no toll anywhere
    monkeypatch.setattr(
        cli,
        "apply_staged",
        lambda path=None, **kw: apply_mod.apply_staged(
            path,
            toll_backend=FakeGateBackend(points=[]),
            searcher=fresh_searcher(com("Net Driver", "2.0")),
            installer=FakeInstaller(),
            verify=ok_verify,
        ),
    )
    code = cli.main(["apply", "--ledger", str(ledger)])
    out = capsys.readouterr().out
    assert code == 2
    assert "No toll has been paid" in out
