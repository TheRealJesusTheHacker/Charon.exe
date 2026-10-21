"""THE WINDOW — the Crossing driven end to end, offscreen.

Runs under PySide6 with the offscreen platform (see the run
commands in the Phase 5 report): ``QT_QPA_PLATFORM=offscreen``
and the shared venv. Where PySide6 isn't importable the module
skips — the engine suite must stay green on a Qt-less box, by the
same rule that keeps the CLI Qt-free.

The whole pipeline is faked at the window's engine adapters: scan,
download, toll, and apply are the window's four verbs, and each
test scripts them. Load-bearing cases:

- the crossing starts LOCKED and unlocks only behind a paid toll;
- a toll refusal is a designed screen, and the lock holds;
- a gate that closes between toll and crossing (the engine
  re-verifies) brings the owner back to the toll screen;
- unchecking a driver really excludes it from the download;
- screenshots of every main state land in ``phase5-audit/``.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from charon import gui, theme, toll  # noqa: E402
from charon.apply import (  # noqa: E402
    EVENT_CROSSING_STARTED,
    EVENT_REBOOT_REQUIRED,
    ApplyResult,
    StagedEntry,
)
from charon.download import (  # noqa: E402
    EVENT_UPDATE_COMPLETED,
    EVENT_UPDATE_STARTED,
    DownloadPlanEntry,
    ProgressEvent,
    StagedResult,
)
from charon.model import (  # noqa: E402
    STATUS_UP_TO_DATE,
    STATUS_UPDATE_AVAILABLE,
    DeviceReport,
    InstalledDriver,
    ScanReport,
    UpdateCandidate,
)
from charon.platform_inventory import InventoryUnavailable  # noqa: E402
from charon.toll import (  # noqa: E402
    SystemProtectionOff,
    TollRecord,
    TollUnpaid,
)

AUDIT_DIR = Path(__file__).resolve().parent.parent / "phase5-audit"
NOW = datetime.now(timezone.utc)


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(theme.stylesheet())
    return app


@pytest.fixture()
def window(qapp):
    win = gui.create_window()
    win.show()
    yield win
    win.close()
    win.deleteLater()
    qapp.processEvents()


def spin(qapp, seconds=0.3):
    """Let the UI breathe: timers, animations, queued signals."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)


def settle(qapp, window, timeout=6.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        worker = window._worker
        if worker is None or not worker.isRunning():
            qapp.processEvents()
            return True
        time.sleep(0.01)
    return False


# ----------------------------------------------------------------------
# Fake pipeline
# ----------------------------------------------------------------------


def make_report(updates=True):
    devices = [
        DeviceReport(
            driver=InstalledDriver(
                device_name="Net Device", device_id="NET-1", version="1.0"
            ),
            status=STATUS_UPDATE_AVAILABLE,
            update=UpdateCandidate(title="Net Driver", version="2.0"),
            explanation="A newer Net driver is available.",
        ),
        DeviceReport(
            driver=InstalledDriver(
                device_name="Audio Device", device_id="AUD-1", version="3.0"
            ),
            status=STATUS_UPDATE_AVAILABLE,
            update=UpdateCandidate(title="Audio Driver", version="3.1"),
            explanation="A newer Audio driver is available.",
        ),
        DeviceReport(
            driver=InstalledDriver(
                device_name="Chipset", device_id="CHI-1", version="9.0"
            ),
            status=STATUS_UP_TO_DATE,
        ),
    ]
    if not updates:
        devices = devices[2:]
    return ScanReport(
        generated_at=datetime.now().astimezone(),
        computer="test-pc",
        devices=devices,
    )


def staged_result(device_id, name, title, version, status="succeeded"):
    return StagedResult(
        entry=DownloadPlanEntry(
            update=UpdateCandidate(title=title, version=version),
            device_name=name,
            device_id=device_id,
        ),
        status=status,
    )


def fake_download_ok(report, selection, emit):
    titles = {"NET-1": ("Net Device", "Net Driver"), "AUD-1": ("Audio Device", "Audio Driver")}
    results = []
    for key in selection:
        name, title = titles[key]
        emit(ProgressEvent(kind=EVENT_UPDATE_STARTED, device_name=name, title=title))
        emit(
            ProgressEvent(
                kind=EVENT_UPDATE_COMPLETED,
                device_name=name,
                title=title,
                status="succeeded",
            )
        )
        version = "2.0" if key == "NET-1" else "3.1"
        results.append(staged_result(key, name, title, version))
    return results, Path("ledger.json")


def fake_record():
    return TollRecord(
        sequence_number=41,
        description="CHARON.EXE crossing checkpoint test",
        marker="test",
        created_at=NOW,
        verified_at=NOW,
        computer="test-pc",
    )


def fake_toll_ok(emit):
    return fake_record()


def fake_apply_ok(emit):
    emit(
        ProgressEvent(
            kind=EVENT_REBOOT_REQUIRED,
            device_name="Net Device",
            title="Net Driver",
            message=(
                "Net Device is in, but needs a restart to finish. "
                "Charon won't restart your PC — restart when you're "
                "ready, from the Start menu."
            ),
        )
    )
    results = [
        ApplyResult(
            entry=StagedEntry(
                device_id="NET-1",
                device_name="Net Device",
                update_title="Net Driver",
                update_version="2.0",
            ),
            status="succeeded",
            reboot_required=True,
        ),
        ApplyResult(
            entry=StagedEntry(
                device_id="AUD-1",
                device_name="Audio Device",
                update_title="Audio Driver",
                update_version="3.1",
            ),
            status="succeeded",
        ),
    ]
    return results, fake_record(), Path("ledger.json")


def drive_to_toll(qapp, window, monkeypatch):
    monkeypatch.setattr(gui, "engine_scan", lambda: make_report())
    monkeypatch.setattr(gui, "engine_download", fake_download_ok)
    window.start_scan()
    assert settle(qapp, window)
    window.start_download()
    assert settle(qapp, window)
    assert window.pages.currentIndex() == gui._PAGE_TOLL


# ----------------------------------------------------------------------
# The lock, the journey, the screens
# ----------------------------------------------------------------------


def test_window_starts_on_welcome_with_everything_functional(window):
    assert window.pages.currentIndex() == gui._PAGE_WELCOME
    assert window.btn_scan.isEnabled()
    assert window.btn_cross.isEnabled() is False  # locked from birth


def test_scan_populates_the_manifest(window, monkeypatch, qapp):
    monkeypatch.setattr(gui, "engine_scan", lambda: make_report())
    window.start_scan()
    assert settle(qapp, window)
    assert window.pages.currentIndex() == gui._PAGE_MANIFEST
    assert len(window._rows) == 3
    assert window.btn_download.isEnabled()
    assert "2 selected" in window.btn_download.text()
    chipset = window._rows["CHI-1"]
    assert chipset.selectable is False
    assert chipset.chip.text() == "UP TO DATE"


def test_unchecking_a_driver_excludes_it(window, monkeypatch, qapp):
    monkeypatch.setattr(gui, "engine_scan", lambda: make_report())
    seen = {}

    def recording_download(report, selection, emit):
        seen["selection"] = list(selection)
        return fake_download_ok(report, selection, emit)

    monkeypatch.setattr(gui, "engine_download", recording_download)
    monkeypatch.setattr(gui, "engine_toll", fake_toll_ok)
    window.start_scan()
    assert settle(qapp, window)
    window._rows["AUD-1"].checkbox.setChecked(False)
    window.start_download()
    assert settle(qapp, window)
    assert seen["selection"] == ["NET-1"]


def test_toll_paid_unlocks_the_crossing(window, monkeypatch, qapp):
    drive_to_toll(qapp, window, monkeypatch)
    assert window.btn_cross.isEnabled() is False
    monkeypatch.setattr(gui, "engine_toll", fake_toll_ok)
    window.start_toll()
    assert settle(qapp, window)
    assert window._toll_record is not None
    assert window._toll_record.sequence_number == 41
    assert window.btn_cross.isEnabled() is True
    assert "#41" in window.ticket.headline.text()


def test_toll_refusal_is_designed_and_the_lock_holds(window, monkeypatch, qapp):
    drive_to_toll(qapp, window, monkeypatch)

    def refused(emit):
        raise SystemProtectionOff(
            "System Protection is turned off on your system drive.",
            next_step="Open 'Create a restore point' and turn it on.",
        )

    monkeypatch.setattr(gui, "engine_toll", refused)
    window.start_toll()
    assert settle(qapp, window)
    assert window.toll_refusal.isHidden() is False
    assert "turned off" in window.toll_refusal.text()
    assert "Create a restore point" in window.toll_refusal.text()
    assert window.btn_cross.isEnabled() is False
    assert window.btn_pay.isEnabled() is True  # try again is real


def test_gate_closing_at_the_crossing_returns_to_the_toll(window, monkeypatch, qapp):
    drive_to_toll(qapp, window, monkeypatch)
    monkeypatch.setattr(gui, "engine_toll", fake_toll_ok)
    window.start_toll()
    assert settle(qapp, window)
    assert window.btn_cross.isEnabled() is True

    def gate_closed(emit):
        raise TollUnpaid(
            "The restore point that paid this crossing's toll can't "
            "be found on this PC anymore — it may have been deleted."
        )

    monkeypatch.setattr(gui, "engine_apply", gate_closed)
    window.start_apply()
    assert settle(qapp, window)
    assert window.pages.currentIndex() == gui._PAGE_TOLL
    assert window.toll_refusal.isHidden() is False
    assert "can't be found" in window.toll_refusal.text()
    assert window.btn_cross.isEnabled() is False


def test_apply_without_a_toll_in_this_visit_goes_nowhere(window):
    window.start_apply()
    assert window.pages.currentIndex() == gui._PAGE_TOLL
    assert "hasn't been paid" in window.status_line.text()


def test_full_journey_ends_on_a_designed_done_screen(window, monkeypatch, qapp):
    drive_to_toll(qapp, window, monkeypatch)
    monkeypatch.setattr(gui, "engine_toll", fake_toll_ok)
    monkeypatch.setattr(gui, "engine_apply", fake_apply_ok)
    window.start_toll()
    assert settle(qapp, window)
    window.start_apply()
    assert settle(qapp, window)
    assert window.pages.currentIndex() == gui._PAGE_MESSAGE
    assert window.message_title.text() == "The crossing is complete"
    assert "crossed safely" in window.message_body.text()
    assert "restart is needed" in window.message_body.text().lower()
    # The reboot was also reported on the crossing screen itself —
    # as a banner while the crossing ran, never an action taken.
    assert "restart your pc" in window.reboot_banner.text().lower()
    assert "won't restart" in window.reboot_banner.text().lower()


def test_crossing_retires_the_picking_controls(window, monkeypatch, qapp):
    drive_to_toll(qapp, window, monkeypatch)
    monkeypatch.setattr(gui, "engine_toll", fake_toll_ok)
    release = threading.Event()

    def blocking_apply(emit):
        release.wait(10)
        return fake_apply_ok(emit)

    monkeypatch.setattr(gui, "engine_apply", blocking_apply)
    window.start_toll()
    assert settle(qapp, window)
    window.start_apply()
    spin(qapp, 0.3)
    # Mid-river, the picking controls are retired…
    assert window.btn_download.isVisible() is False
    assert window.btn_rescan.isEnabled() is False
    # …and no step can be re-entered while the engine works.
    page_before = window.pages.currentIndex()
    window.start_scan()
    window.start_download()
    window.start_toll()
    assert window.pages.currentIndex() == page_before
    release.set()
    assert settle(qapp, window)
    assert window.pages.currentIndex() == gui._PAGE_MESSAGE
    assert window.btn_rescan.isEnabled() is True


def test_scan_failure_is_a_designed_screen(window, monkeypatch, qapp):
    def broken_scan():
        raise InventoryUnavailable(
            "Charon couldn't read this PC's driver list.",
            detail="fake seam detail",
        )

    monkeypatch.setattr(gui, "engine_scan", broken_scan)
    window.start_scan()
    assert settle(qapp, window)
    assert window.pages.currentIndex() == gui._PAGE_MESSAGE
    assert window.message_title.text() == "The scan couldn't run"
    assert "couldn't read" in window.message_body.text()
    assert "Traceback" not in window.message_body.text()
    assert "fake seam detail" not in window.message_body.text()


def test_no_updates_shows_the_up_to_date_panel(window, monkeypatch, qapp):
    monkeypatch.setattr(gui, "engine_scan", lambda: make_report(updates=False))
    window.start_scan()
    assert settle(qapp, window)
    assert window.pages.currentIndex() == gui._PAGE_MANIFEST
    assert window.empty_panel.isHidden() is False
    assert window.btn_download.isVisible() is False


# ----------------------------------------------------------------------
# Brand pins: contrast and the no-purple law
# ----------------------------------------------------------------------


def _luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))

    def lin(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _ratio(fg: str, bg: str) -> float:
    la, lb = _luminance(fg), _luminance(bg)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("name,pair", sorted(theme.TEXT_PAIRINGS.items()))
def test_text_pairings_meet_wcag_contrast(name, pair):
    fg, bg = pair
    assert _ratio(fg, bg) >= 4.5, f"{name}: {_ratio(fg, bg):.2f}:1"


def test_palette_is_charons_own_styx_palette():
    """The namesake pin: these exact values ARE Charon's identity
    — the Styx palette (charcoal-slate, river aqua, obol gold),
    design-proven for contrast before it landed. A drive-by
    recolor, including back to the shared house green, fails
    here on purpose. The danger red is the fleet constant."""
    expected = {
        "BG": "#07090b",
        "PANEL": "#0d1216",
        "PANEL_BORDER": "#22303a",
        "INPUT_BG": "#0a0e11",
        "TEXT": "#e8eef0",
        "TEXT_FAINT": "#93a7b1",
        "PRIMARY": "#2fbfa8",
        "PRIMARY_DARK_TEXT": "#04120f",
        "ACCENT": "#bfa05a",
        "DISABLED_TEXT": "#7e939e",
        "RED": "#b03a2e",
        "RED_TEXT": "#ffffff",
    }
    for name, value in expected.items():
        assert getattr(theme, name) == value, name
    assert theme.STAMP == theme.PRIMARY


def test_palette_contains_no_purple():
    import colorsys

    palette = {
        name: getattr(theme, name)
        for name in dir(theme)
        if name.isupper() and isinstance(getattr(theme, name), str)
        and getattr(theme, name).startswith("#")
    }
    assert palette, "palette constants not found"
    for name, hex_value in palette.items():
        h = hex_value.lstrip("#")
        r, g, b = (int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))
        hue, _light, sat = colorsys.rgb_to_hls(r, g, b)
        degrees = hue * 360
        if sat > 0.08:
            assert not (240 <= degrees <= 320), (
                f"{name}={hex_value} sits in the purple band "
                f"({degrees:.0f}°)"
            )


# ----------------------------------------------------------------------
# The screenshot audit — every main state, rendered
# ----------------------------------------------------------------------


def _grab(qapp, window, name: str):
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    spin(qapp, 0.25)
    path = AUDIT_DIR / name
    image = window.grab()
    assert image.save(str(path)), f"could not save {path}"
    assert path.stat().st_size > 4000, f"{name} looks blank"
    return path


def test_screenshot_audit(windows_pair, monkeypatch, qapp):
    window, failure_window = windows_pair

    release_scan = threading.Event()
    release_apply = threading.Event()

    def blocking_scan():
        release_scan.wait(10)
        return make_report()

    def blocking_apply(emit):
        emit(
            ProgressEvent(
                kind=EVENT_CROSSING_STARTED,
                device_name="Net Device",
                title="Net Driver",
            )
        )
        release_apply.wait(10)
        return fake_apply_ok(emit)

    monkeypatch.setattr(gui, "engine_scan", blocking_scan)
    monkeypatch.setattr(gui, "engine_download", fake_download_ok)
    monkeypatch.setattr(gui, "engine_toll", fake_toll_ok)
    monkeypatch.setattr(gui, "engine_apply", blocking_apply)

    _grab(qapp, window, "01-idle.png")

    window.start_scan()
    spin(qapp, 0.7)  # the sweep is mid-turn, honestly working
    _grab(qapp, window, "02-scanning.png")
    release_scan.set()
    assert settle(qapp, window)
    _grab(qapp, window, "03-results.png")

    window.start_download()
    assert settle(qapp, window)
    window.start_toll()
    assert settle(qapp, window)
    window.ticket.stamp_progress = 1.0  # the stamp, landed
    window.ticket.update()
    _grab(qapp, window, "04-toll-paid.png")

    window.start_apply()
    spin(qapp, 0.7)  # mid-crossing: first driver installing
    _grab(qapp, window, "05-applying.png")
    release_apply.set()
    assert settle(qapp, window)
    _grab(qapp, window, "06-done.png")

    # The failure state gets its own window and its own portrait.
    monkeypatch.setattr(gui, "engine_scan", lambda: make_report())

    def refused(emit):
        raise SystemProtectionOff(
            toll._PROTECTION_OFF_MESSAGE,
            next_step=toll._PROTECTION_OFF_NEXT_STEP,
        )

    monkeypatch.setattr(gui, "engine_toll", refused)
    failure_window.start_scan()
    assert settle(qapp, failure_window)
    failure_window.start_download()
    assert settle(qapp, failure_window)
    failure_window.start_toll()
    assert settle(qapp, failure_window)
    _grab(qapp, failure_window, "07-failure-toll-refused.png")


@pytest.fixture()
def windows_pair(qapp):
    first = gui.create_window()
    first.show()
    second = gui.create_window()
    second.show()
    yield first, second
    for win in (first, second):
        win.close()
        win.deleteLater()
    qapp.processEvents()


# ----------------------------------------------------------------------
# Kit hunt 2026-10-20: worker-thread COM + close-while-busy
# ----------------------------------------------------------------------


def test_close_while_engine_is_working_is_refused(window, monkeypatch, qapp):
    """Closing the window mid-step must not destroy a running worker
    thread (Qt aborts the process for that). The close is refused in
    plain language until the step finishes."""
    release = threading.Event()

    def blocking_scan():
        release.wait(10)
        return make_report()

    monkeypatch.setattr(gui, "engine_scan", blocking_scan)
    window.start_scan()
    spin(qapp, 0.3)
    assert window._busy()
    accepted = window.close()
    assert accepted is False, "window closed while its worker was running"
    assert window.isVisible()
    assert "finish" in window.status_line.text().lower()
    release.set()
    assert settle(qapp, window)


@pytest.mark.skipif(
    not os.sys.platform.startswith("win"),
    reason="COM apartment behaviour is Windows-only",
)
def test_engine_worker_can_use_com_on_windows(window, qapp):
    """Probe/guard: the engine runs on the worker thread, so COM
    activation from that thread is exercised against the real thing
    on the Windows runner — creating the Windows Update session,
    exactly what a GUI scan does first. The 2026-10-20 kit hunt
    suspected an apartment defect here ('CoInitialize has not been
    called'); the probe PASSED on windows-latest pre-fix, clearing
    that suspicion with evidence, and stays as the guard so a
    future threading change that breaks it fails here, not on a
    user's PC."""
    outcome = {}

    def engine(emit):
        import win32com.client

        session = win32com.client.Dispatch("Microsoft.Update.Session")
        return session is not None

    worker = gui.EngineWorker(lambda emit: engine(emit))
    worker.succeeded.connect(lambda result: outcome.update(ok=result))
    worker.failed.connect(lambda exc: outcome.update(error=exc))
    worker.start()
    assert worker.wait(15000), "worker thread did not finish"
    qapp.processEvents()
    assert outcome.get("error") is None, (
        f"engine failed on the worker thread: {outcome.get('error')!r}"
    )
    assert outcome.get("ok") is True


@pytest.mark.skipif(
    not os.sys.platform.startswith("win"),
    reason="real WUA API shape is Windows-only",
)
def test_real_session_shape_and_update_collection_coclass():
    """Probe/guard, Windows-only: pin the two WUA shapes the kit
    hunt found the seams getting wrong, against the real API.

    1. IUpdateSession has NO CreateUpdateCollection member — both
       batch seams asked it for one, which fails on a real PC
       before any download or install can begin.
    2. A collection is its own coclass: Dispatch of
       Microsoft.Update.UpdateColl yields an empty collection —
       the shape both seams now build through their collection
       factory.

    The probe initializes its own COM apartment explicitly: this
    test is about API shape, not apartment policy (activation
    from the app's real threads is the worker probe's subject,
    above — and a thread that reuses an already-imported COM
    stack without initializing gets 'CoInitialize has not been
    called', as this probe's first version rediscovered).
    """
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        session = win32com.client.Dispatch("Microsoft.Update.Session")
        with pytest.raises(Exception):
            session.CreateUpdateCollection()
        collection = win32com.client.Dispatch(
            "Microsoft.Update.UpdateColl"
        )
        assert int(collection.Count) == 0
    finally:
        pythoncom.CoUninitialize()


def test_engine_worker_drops_its_callable_after_running(qapp):
    """Sweep #3: a finished worker must not keep its callable —
    and whatever the callable closed over — alive. The worker
    stays a child of the window after its step; the closure must
    not stay with it."""
    seen = {}

    def fn(emit):
        return 41

    worker = gui.EngineWorker(fn)
    worker.succeeded.connect(lambda result: seen.update(result=result))
    worker.start()
    assert worker.wait(10000)
    qapp.processEvents()
    assert seen.get("result") == 41
    assert worker._fn is None


# ----------------------------------------------------------------------
# Phase 6 — the passenger manifest button (B-5)
# ----------------------------------------------------------------------


def test_manifest_button_saves_the_current_report(window, monkeypatch, qapp):
    monkeypatch.setattr(gui, "engine_scan", lambda: make_report())
    seen = {}

    def fake_manifest(report, emit=None):
        seen["report"] = report
        return Path("charon-passenger-manifest.json")

    monkeypatch.setattr(gui, "engine_manifest", fake_manifest)
    window.start_scan()
    assert settle(qapp, window)
    assert window.btn_manifest.isEnabled()
    window.start_manifest_export()
    assert settle(qapp, window)
    assert seen["report"] is window._report
    assert "Passenger manifest saved" in window.status_line.text()
    assert "charon-passenger-manifest.json" in window.status_line.text()
    # Saving a record never moves the owner off the manifest page.
    assert window.pages.currentIndex() == gui._PAGE_MANIFEST


def test_manifest_button_failure_is_a_designed_screen(window, monkeypatch, qapp):
    from charon.manifest import ManifestExportError

    monkeypatch.setattr(gui, "engine_scan", lambda: make_report())

    def refused(report, emit=None):
        raise ManifestExportError(
            "Charon couldn't write the passenger manifest file.",
            detail="fake disk detail",
        )

    monkeypatch.setattr(gui, "engine_manifest", refused)
    window.start_scan()
    assert settle(qapp, window)
    window.start_manifest_export()
    assert settle(qapp, window)
    assert window.pages.currentIndex() == gui._PAGE_MESSAGE
    assert window.message_title.text() == "The passenger manifest couldn't be saved"
    assert "couldn't write the passenger manifest" in window.message_body.text()
    assert "fake disk detail" not in window.message_body.text()
