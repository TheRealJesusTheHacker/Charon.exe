"""THE TOLL — creation is a promise; verification is payment.

Every test drives ``charon.toll`` through a fake backend (and, for
the seam itself, a recording runner), on Linux, with no Windows in
sight. The load-bearing cases:

- the happy path pays, records, and opens the gate;
- the silent skip (Windows' 24-hour rule) is UNPAID, always;
- a point bearing our marker but stamped before this run is UNPAID;
- System Protection off refuses BEFORE anything is created;
- the gate refuses without a toll and re-verifies with one.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from charon import cli, toll
from charon.download import append_run_to_ledger, load_ledger
from charon.toll import (
    EVENT_TOLL_PAID,
    EVENT_TOLL_UNPAID,
    PowerShellRestoreBackend,
    RestorePointInfo,
    SystemProtectionOff,
    TollAdminRequired,
    TollBackendError,
    TollProbe,
    TollRecord,
    build_marker,
    latest_toll,
    parse_probe_json,
    parse_restore_point_time,
    parse_restore_points_json,
    pay_toll,
    record_toll,
    require_paid_toll,
)

YESTERDAY = datetime.now(timezone.utc) - timedelta(hours=25)


@pytest.fixture(autouse=True)
def _clean_session():
    toll.clear_session_toll()
    yield
    toll.clear_session_toll()


def _old_charon_point(sequence=3) -> RestorePointInfo:
    """A restore point from YESTERDAY's crossing: Charon's marker,
    but an earlier run's token, stamped over a day ago."""
    return RestorePointInfo(
        sequence_number=sequence,
        description=build_marker(YESTERDAY, token="deadbeef"),
        created_at=YESTERDAY,
        restore_point_type=12,
    )


class FakeRestoreBackend:
    """Scripted stand-in for the PowerShell backend.

    ``create_behavior``: "add" (a point bearing our exact description
    appears, stamped now), "skip" (creation 'succeeds' and nothing
    new exists — the 24-hour trap), "stale" (a point bearing our
    description appears but stamped an hour before the run).
    """

    def __init__(
        self,
        *,
        is_admin=True,
        protection=True,
        points=(),
        create_behavior="add",
        create_error=None,
        list_error=None,
    ):
        self._probe = TollProbe(
            is_admin=is_admin, protection_enabled=protection, system_drive="C:"
        )
        self._points = list(points)
        self._create_behavior = create_behavior
        self._create_error = create_error
        self._list_error = list_error
        self._next_sequence = 41
        self.probe_calls = 0
        self.create_calls: list[str] = []
        self.list_calls = 0

    def probe(self):
        self.probe_calls += 1
        return self._probe

    def create_restore_point(self, description):
        self.create_calls.append(description)
        if self._create_error is not None:
            raise TollBackendError(self._create_error)
        if self._create_behavior == "skip":
            return  # Windows smiles, nods, and does nothing (24h rule)
        if self._create_behavior == "add":
            created = datetime.now(timezone.utc)
        elif self._create_behavior == "stale":
            created = datetime.now(timezone.utc) - timedelta(hours=1)
        else:  # pragma: no cover - a typo'd behavior is a test bug
            raise AssertionError(self._create_behavior)
        self._points.append(
            RestorePointInfo(
                sequence_number=self._next_sequence,
                description=description,
                created_at=created,
                restore_point_type=12,
            )
        )
        self._next_sequence += 1

    def list_restore_points(self):
        self.list_calls += 1
        if self._list_error is not None:
            raise TollBackendError(self._list_error)
        return list(self._points)

    def drop_point(self, sequence_number):
        self._points = [
            p for p in self._points if p.sequence_number != sequence_number
        ]


# ----------------------------------------------------------------------
# Paying the toll
# ----------------------------------------------------------------------


def test_pay_toll_happy_path(tmp_path):
    backend = FakeRestoreBackend()
    ledger = tmp_path / "ledger.json"
    events = []
    record = pay_toll(backend, ledger_path=ledger, emit=events.append)

    assert record.sequence_number == 41
    assert record.marker in record.description
    assert record.description.startswith("CHARON.EXE crossing checkpoint")
    assert record.created_at is not None
    assert backend.create_calls == [record.description]
    assert [e.kind for e in events] == [
        toll.EVENT_TOLL_CHECKING,
        toll.EVENT_TOLL_CREATING,
        toll.EVENT_TOLL_VERIFYING,
        EVENT_TOLL_PAID,
    ]
    # The proof is on disk, in the shared ledger, and in the session.
    assert latest_toll(ledger) == record
    assert toll.session_toll() == record


def test_silent_skip_is_unpaid(tmp_path):
    """THE TRAP: creation 'succeeds', no new point exists (24h rule),
    and an older Charon point from yesterday is sitting right there.
    The toll is unpaid — yesterday's coin doesn't pay today's fare."""
    backend = FakeRestoreBackend(
        points=[_old_charon_point()], create_behavior="skip"
    )
    ledger = tmp_path / "ledger.json"
    events = []
    with pytest.raises(toll.TollUnpaid) as excinfo:
        pay_toll(
            backend,
            ledger_path=ledger,
            emit=events.append,
            verify_attempts=2,
            sleep=lambda _s: None,
        )
    assert "skipped" in excinfo.value.user_message
    assert "ferry doesn't move" in excinfo.value.user_message
    assert events[-1].kind == EVENT_TOLL_UNPAID
    # Nothing was recorded as paid, anywhere.
    assert latest_toll(ledger) is None
    assert toll.session_toll() is None


def test_marker_with_stale_timestamp_is_unpaid(tmp_path):
    """A point bearing THIS run's marker but stamped before the run
    began fails the timestamp leg — marker alone is never enough."""
    backend = FakeRestoreBackend(create_behavior="stale")
    with pytest.raises(toll.TollUnpaid):
        pay_toll(
            backend,
            ledger_path=tmp_path / "ledger.json",
            verify_attempts=1,
        )
    assert toll.session_toll() is None


def test_protection_off_refuses_before_creating(tmp_path):
    backend = FakeRestoreBackend(protection=False)
    with pytest.raises(SystemProtectionOff) as excinfo:
        pay_toll(backend, ledger_path=tmp_path / "ledger.json")
    assert backend.create_calls == []  # refused upstream, nothing tried
    message = excinfo.value.user_message
    assert "System Protection is turned off" in message
    steps = excinfo.value.next_step
    assert "Create a restore point" in steps  # the how-to is in the refusal
    assert "turn on system protection" in steps.lower()


def test_protection_unknown_proceeds_and_verification_decides(tmp_path):
    """Probe can't tell → creation is attempted; the enumeration
    afterwards is the arbiter, and here it pays."""
    backend = FakeRestoreBackend(protection=None)
    record = pay_toll(backend, ledger_path=tmp_path / "ledger.json")
    assert record.sequence_number == 41


def test_create_error_saying_disabled_maps_to_protection_off(tmp_path):
    backend = FakeRestoreBackend(
        create_error="Checkpoint-Computer failed: System Restore is "
        "disabled by group policy on this machine"
    )
    with pytest.raises(SystemProtectionOff):
        pay_toll(backend, ledger_path=tmp_path / "ledger.json")


def test_create_error_saying_frequency_maps_to_skip(tmp_path):
    backend = FakeRestoreBackend(
        create_error="A new system restore point cannot be created "
        "because one has already been created within the past 24 hours"
    )
    with pytest.raises(toll.TollUnpaid) as excinfo:
        pay_toll(backend, ledger_path=tmp_path / "ledger.json")
    assert not isinstance(excinfo.value, SystemProtectionOff)
    assert "skipped" in excinfo.value.user_message


def test_not_admin_refuses_before_creating(tmp_path):
    backend = FakeRestoreBackend(is_admin=False)
    with pytest.raises(TollAdminRequired) as excinfo:
        pay_toll(backend, ledger_path=tmp_path / "ledger.json")
    assert isinstance(excinfo.value, toll.TollUnpaid)  # still just: unpaid
    assert backend.create_calls == []
    assert "administrator" in excinfo.value.user_message


def test_verification_query_failure_fails_closed(tmp_path):
    """If the points can't be read back, the toll counts as unpaid —
    Charon never moves the ferry on an unverifiable promise."""
    backend = FakeRestoreBackend(list_error="WMI query exploded")
    with pytest.raises(toll.TollUnpaid) as excinfo:
        pay_toll(backend, ledger_path=tmp_path / "ledger.json")
    assert "counts as unpaid" in excinfo.value.user_message
    assert "WMI query exploded" in excinfo.value.detail  # log-only detail
    assert toll.session_toll() is None


def test_pay_toll_without_backend_on_non_windows():
    import charon.toll as toll_module

    if toll_module.sys.platform.startswith("win"):
        pytest.skip("test box is Windows")
    with pytest.raises(toll.TollUnpaid) as excinfo:
        pay_toll()
    assert "isn't running Windows" in excinfo.value.user_message


# ----------------------------------------------------------------------
# The gate
# ----------------------------------------------------------------------


def test_gate_refuses_without_any_toll(tmp_path):
    backend = FakeRestoreBackend()
    with pytest.raises(toll.TollUnpaid) as excinfo:
        require_paid_toll(backend, ledger_path=tmp_path / "ledger.json")
    assert "No toll has been paid" in excinfo.value.user_message


def test_gate_accepts_session_toll_and_reverifies(tmp_path):
    backend = FakeRestoreBackend()
    ledger = tmp_path / "ledger.json"
    record = pay_toll(backend, ledger_path=ledger)

    assert require_paid_toll(backend, ledger_path=ledger) == record

    # The point vanishes (owner deletes it, disk cleanup eats it):
    # the paper record alone must NOT open the gate.
    backend.drop_point(record.sequence_number)
    with pytest.raises(toll.TollUnpaid) as excinfo:
        require_paid_toll(backend, ledger_path=ledger)
    assert "can't be found" in excinfo.value.user_message


def test_gate_opens_from_the_ledger_after_a_restart(tmp_path):
    """Phase 4 in a fresh process: no session toll, but the ledger
    holds the proof and the point still exists → the gate opens."""
    backend = FakeRestoreBackend()
    ledger = tmp_path / "ledger.json"
    record = pay_toll(backend, ledger_path=ledger)
    toll.clear_session_toll()

    assert require_paid_toll(backend, ledger_path=ledger) == record


def test_apply_shaped_path_cannot_run_without_a_toll(tmp_path):
    """The Phase 4 contract, pinned now: an apply-shaped callable
    that asks the gate first simply cannot run unpaid."""
    backend = FakeRestoreBackend()
    ledger = tmp_path / "ledger.json"
    applied = []

    def fake_apply():
        record = require_paid_toll(backend, ledger_path=ledger)
        applied.append(record.sequence_number)
        return f"crossed behind toll #{record.sequence_number}"

    with pytest.raises(toll.TollUnpaid):
        fake_apply()
    assert applied == []

    pay_toll(backend, ledger_path=ledger)
    assert fake_apply() == "crossed behind toll #41"
    assert applied == [41]


# ----------------------------------------------------------------------
# The ledger
# ----------------------------------------------------------------------


def test_toll_ledger_round_trip_and_sharing(tmp_path):
    ledger = tmp_path / "ledger.json"
    # A download run lands first — the toll must share the file
    # without disturbing it.
    append_run_to_ledger(ledger, [], source="scan")
    first = pay_toll(FakeRestoreBackend(), ledger_path=ledger)
    second_backend = FakeRestoreBackend()
    second = pay_toll(second_backend, ledger_path=ledger)
    assert second.sequence_number == 41  # the fake's numbering, again

    data = load_ledger(ledger)
    assert len(data["runs"]) == 1  # download history untouched
    assert len(data["tolls"]) == 2
    assert data["tolls"][0]["sequence_number"] == first.sequence_number
    assert latest_toll(ledger) == second

    raw = json.loads(ledger.read_text(encoding="utf-8"))
    assert raw["tolls"][1]["marker"] == second.marker


def test_toll_record_dict_round_trip():
    record = TollRecord(
        sequence_number=7,
        description=build_marker(YESTERDAY, token="cafe1234"),
        marker=build_marker(YESTERDAY, token="cafe1234"),
        created_at=YESTERDAY,
        verified_at=YESTERDAY + timedelta(minutes=1),
        computer="TEST-PC",
    )
    assert TollRecord.from_dict(record.to_dict()) == record


# ----------------------------------------------------------------------
# Markers and time parsing (pure)
# ----------------------------------------------------------------------


def test_build_marker_shape_and_charset():
    marker = build_marker(datetime(2026, 10, 19, 14, 5, 9, tzinfo=timezone.utc),
                          token="9f3ac41b")
    assert marker == (
        "CHARON.EXE crossing checkpoint 2026-10-19T14:05:09Z #9f3ac41b"
    )
    # It is substituted into the pinned creation script verbatim —
    # nothing in it may be able to break out of the quoted string.
    assert "'" not in marker and '"' not in marker and ";" not in marker
    assert toll._MARKER_SAFE.match(marker)


def test_markers_are_unique_per_run():
    assert build_marker() != build_marker()


def test_parse_restore_point_time_wmi_format():
    # 14:05:09 at UTC-5 (-300 minutes) is 19:05:09 UTC.
    parsed = parse_restore_point_time("20261019140509.000000-300")
    assert parsed == datetime(2026, 10, 19, 19, 5, 9, tzinfo=timezone.utc)
    # Positive offset: 14:05:09 at UTC+1 is 13:05:09 UTC.
    parsed = parse_restore_point_time("20261019140509.123456+060")
    assert parsed == datetime(
        2026, 10, 19, 13, 5, 9, 123456, tzinfo=timezone.utc
    )


def test_parse_restore_point_time_other_shapes():
    moment = datetime(2026, 10, 19, 14, 5, 9, tzinfo=timezone.utc)
    ms = int(moment.timestamp() * 1000)
    assert parse_restore_point_time(f"/Date({ms})/") == moment
    assert parse_restore_point_time("2026-10-19T14:05:09Z") == moment
    assert parse_restore_point_time(moment) == moment
    assert parse_restore_point_time("not a timestamp") is None
    assert parse_restore_point_time("") is None
    assert parse_restore_point_time(None) is None


def test_parse_restore_points_json_shapes():
    row = {
        "SequenceNumber": 12,
        "Description": "CHARON.EXE crossing checkpoint 2026-10-19T14:05:09Z #aa",
        "CreationTime": "20261019140509.000000-300",
        "RestorePointType": 12,
    }
    points = parse_restore_points_json(json.dumps([row, dict(row, SequenceNumber=13)]))
    assert [p.sequence_number for p in points] == [12, 13]
    assert points[0].created_at == datetime(
        2026, 10, 19, 19, 5, 9, tzinfo=timezone.utc
    )
    # Bare object (PowerShell's single-result shape) and empty output.
    assert len(parse_restore_points_json(json.dumps(row))) == 1
    assert parse_restore_points_json("") == []
    assert parse_restore_points_json("   ") == []


def test_parse_probe_json():
    probe = parse_probe_json(
        '{"is_admin": true, "system_drive": "C:", "protection_enabled": false}'
    )
    assert probe == TollProbe(is_admin=True, protection_enabled=False,
                              system_drive="C:")
    probe = parse_probe_json('{"is_admin": false, "protection_enabled": null}')
    assert probe.protection_enabled is None  # "couldn't tell" survives


# ----------------------------------------------------------------------
# The seam itself: exactly the pinned commands, in order
# ----------------------------------------------------------------------


class RecordingPowerShellRunner:
    """Answers per pinned script and records every argv. Refuses —
    loudly — anything it doesn't recognize."""

    def __init__(self, marker, sequence=41):
        self.marker = marker
        self.sequence = sequence
        self.calls: list[list[str]] = []

    def run(self, args, timeout=120):
        args = list(args)
        self.calls.append(args)
        script = args[-1]
        if script == toll.TOLL_PROBE_SCRIPT:
            return json.dumps(
                {
                    "is_admin": True,
                    "system_drive": "C:",
                    "protection_enabled": True,
                }
            )
        if script == toll.RP_CREATE_SCRIPT_TEMPLATE.format(
            description=self.marker
        ):
            return ""
        if script == toll.RP_LIST_SCRIPT:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            return json.dumps(
                [
                    {
                        "SequenceNumber": self.sequence,
                        "Description": self.marker,
                        "CreationTime": f"{stamp}.000000+000",
                        "RestorePointType": 12,
                    }
                ]
            )
        raise AssertionError(f"unexpected PowerShell from Charon: {script!r}")


def test_powershell_backend_issues_exactly_the_pinned_commands(tmp_path):
    """Driving a whole toll payment through the real seam issues
    exactly: probe, create, enumerate — in that order, byte-pinned."""
    marker = build_marker(token="c001d00d")
    runner = RecordingPowerShellRunner(marker)
    backend = PowerShellRestoreBackend(runner=runner)

    record = pay_toll(
        backend, ledger_path=tmp_path / "ledger.json", marker=marker
    )

    assert record.sequence_number == 41
    assert runner.calls == [
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         toll.TOLL_PROBE_SCRIPT],
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         toll.RP_CREATE_SCRIPT_TEMPLATE.format(description=marker)],
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         toll.RP_LIST_SCRIPT],
    ]


def test_create_description_charset_guard():
    """A description that could break out of the pinned script's
    quotes is refused before anything runs."""
    runner = RecordingPowerShellRunner(marker="unused")
    backend = PowerShellRestoreBackend(runner=runner)
    with pytest.raises(ValueError):
        backend.create_restore_point("bad'; Format-Volume")
    assert runner.calls == []


# ----------------------------------------------------------------------
# The CLI: `charon toll`
# ----------------------------------------------------------------------


def _fake_record() -> TollRecord:
    marker = build_marker(token="c001d00d")
    now = datetime.now(timezone.utc)
    return TollRecord(
        sequence_number=41,
        description=marker,
        marker=marker,
        created_at=now,
        verified_at=now,
        computer="TEST-PC",
    )


def test_toll_command_prints_the_receipt(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "pay_toll", lambda **kwargs: _fake_record())
    code = cli.main(["toll", "--ledger", str(tmp_path / "ledger.json")])
    assert code == 0
    out = capsys.readouterr().out
    assert "Toll paid — restore point #41" in out
    assert "verified ✓" in out
    assert "Traceback" not in out


def test_toll_command_unpaid_is_designed(monkeypatch, tmp_path, capsys):
    def refuse(**kwargs):
        raise toll.TollUnpaid(
            "Windows skipped creating the restore point — the toll "
            "is not paid.",
            detail="secret technical detail",
            next_step="Wait a day, then try again.",
        )

    monkeypatch.setattr(cli, "pay_toll", refuse)
    code = cli.main(["toll", "--ledger", str(tmp_path / "ledger.json")])
    assert code == cli.EXIT_DESIGNED_FAILURE == 2
    out = capsys.readouterr().out
    assert "skipped creating the restore point" in out
    assert "Wait a day, then try again." in out
    assert "secret technical detail" not in out  # detail goes to the log
    assert "Traceback" not in out


# ----------------------------------------------------------------------
# Bug hunt #2 regressions (Phases 2–3 audit)
# ----------------------------------------------------------------------


def test_parse_restore_point_time_absurd_wmi_offset_is_none():
    """An out-of-range WMI UTC offset used to raise ValueError out of
    the parser (the offset was built outside the guard) — an
    undatable point is None, never a crash."""
    assert parse_restore_point_time("20261019140509.000000+9999") is None
    assert parse_restore_point_time("20261019140509.000000-9999") is None


def test_parse_restore_points_json_infinite_sequence_is_skipped():
    """JSON can carry Infinity; int(inf) raises OverflowError, which
    used to escape the parser and crash verification."""
    text = (
        '[{"SequenceNumber": Infinity, "Description": "x",'
        ' "CreationTime": "2026-10-19T14:05:09Z"},'
        ' {"SequenceNumber": 9, "Description": "y",'
        ' "CreationTime": "2026-10-19T14:05:09Z"}]'
    )
    points = parse_restore_points_json(text)
    assert [p.sequence_number for p in points] == [9]


def test_parse_probe_json_non_boolean_admin_fails_closed():
    """bool("false") is True — a string admin flag used to wave the
    elevation check through. Only a real boolean proves admin."""
    assert parse_probe_json('{"is_admin": "false"}').is_admin is False
    assert parse_probe_json('{"is_admin": true}').is_admin is True
    assert parse_probe_json("{}").is_admin is False


def test_latest_toll_tolerates_infinite_sequence(tmp_path):
    ledger = tmp_path / "ledger.json"
    ledger.write_text(
        '{"format": 1, "runs": [], "tolls": [{"sequence_number": 1e999,'
        ' "description": "d", "marker": "m",'
        ' "created_at": "2026-10-19T14:05:09Z",'
        ' "verified_at": "2026-10-19T14:05:09Z"}]}',
        encoding="utf-8",
    )
    assert latest_toll(ledger) is None


def test_gate_rejects_record_with_empty_marker(tmp_path):
    """An empty marker is a substring of every description — a
    record without one must never open the gate."""
    backend = FakeRestoreBackend(
        points=[
            RestorePointInfo(
                sequence_number=7,
                description="some other point entirely",
                created_at=datetime.now(timezone.utc),
            )
        ]
    )
    now = datetime.now(timezone.utc)
    record = TollRecord(
        sequence_number=7,
        description="some other point entirely",
        marker="",
        created_at=now,
        verified_at=now,
    )
    with pytest.raises(toll.TollUnpaid):
        require_paid_toll(backend, record=record)


def test_gate_rejects_point_stamped_at_a_different_time(tmp_path):
    """Sequence + marker used to be the gate's whole check. A point
    stamped at a different time than the record's is a different
    point — the timestamp leg binds them."""
    marker = build_marker(token="cafe1234")
    point = RestorePointInfo(
        sequence_number=7,
        description=marker,
        created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    backend = FakeRestoreBackend(points=[point])
    now = datetime.now(timezone.utc)
    record = TollRecord(
        sequence_number=7,
        description=marker,
        marker=marker,
        created_at=now,
        verified_at=now,
    )
    with pytest.raises(toll.TollUnpaid):
        require_paid_toll(backend, record=record)


def test_gate_accepts_point_within_timestamp_tolerance():
    """The mirror case: same sequence, same marker, creation time
    within the verification tolerance → the gate opens."""
    marker = build_marker(token="cafe1234")
    now = datetime.now(timezone.utc)
    point = RestorePointInfo(
        sequence_number=7,
        description=marker,
        created_at=now - timedelta(seconds=1),
    )
    backend = FakeRestoreBackend(points=[point])
    record = TollRecord(
        sequence_number=7,
        description=marker,
        marker=marker,
        created_at=now,
        verified_at=now,
    )
    assert require_paid_toll(backend, record=record) == record


def test_pay_toll_backend_value_error_is_unpaid(tmp_path):
    """The seam's charset guard refuses with ValueError; that used
    to escape pay_toll as a traceback instead of TollUnpaid."""

    class GuardBackend(FakeRestoreBackend):
        def create_restore_point(self, description):
            raise ValueError(f"unsafe description: {description!r}")

    with pytest.raises(toll.TollUnpaid):
        pay_toll(
            GuardBackend(),
            ledger_path=tmp_path / "ledger.json",
            marker="bad'; evil",
        )
    assert toll.session_toll() is None


def test_concurrent_ledger_writers_lose_nothing_under_stress(tmp_path):
    """The 2026-10-20 kit hunt's re-probe: the release run for
    v1.0.2 lost 1 of 40 appends on Windows (19 == 20) even with
    the v1.0.1 seed-byte fix in place — the four-thread test
    above only caught it intermittently. The mechanism is a
    Windows locking rule the seed-byte fix never touched: byte
    locks taken through one handle are released when the SAME
    PROCESS closes any other handle to that file, so one thread
    finishing could drop the lock another thread was holding,
    and two writers read-modify-wrote the ledger together. This
    heavier mix (8 threads, 200 appends) makes the loss show up
    reliably on Windows when in-process writers are not
    serialized before they reach the file lock."""
    import threading

    from charon.download import append_run_to_ledger as append_run

    ledger = tmp_path / "ledger.json"
    errors = []

    def runs():
        try:
            for _ in range(25):
                append_run(ledger, [], source="scan")
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    def tolls():
        try:
            for i in range(25):
                now = datetime.now(timezone.utc)
                marker = f"stress probe toll {i}"
                record_toll(
                    ledger,
                    TollRecord(
                        sequence_number=i,
                        description=marker,
                        marker=marker,
                        created_at=now,
                        verified_at=now,
                    ),
                )
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [
        threading.Thread(target=runs) for _ in range(4)
    ] + [threading.Thread(target=tolls) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    data = load_ledger(ledger)
    assert len(data["runs"]) == 100
    assert len(data["tolls"]) == 100


def test_concurrent_ledger_writers_lose_nothing(tmp_path):
    """Download runs and toll records share one ledger. Concurrent
    writers used to collide on one fixed temp name (crashes) and
    erase each other's appends (lost updates)."""
    import threading

    from charon.download import append_run_to_ledger as append_run

    ledger = tmp_path / "ledger.json"
    errors = []

    def runs():
        try:
            for _ in range(10):
                append_run(ledger, [], source="scan")
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    def tolls():
        try:
            for i in range(10):
                now = datetime.now(timezone.utc)
                marker = build_marker(now, token=f"{i:08x}")
                record_toll(
                    ledger,
                    TollRecord(
                        sequence_number=i,
                        description=marker,
                        marker=marker,
                        created_at=now,
                        verified_at=now,
                    ),
                )
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [
        threading.Thread(target=runs),
        threading.Thread(target=runs),
        threading.Thread(target=tolls),
        threading.Thread(target=tolls),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    data = load_ledger(ledger)
    assert len(data["runs"]) == 20
    assert len(data["tolls"]) == 20
