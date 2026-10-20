"""CHARON.EXE — the window (Phase 5): the Crossing, as one journey.

One window, one flow, in the Crossing's fixed order — Scan →
Download → the Toll → Apply — because the engine underneath runs
in that order and the interface refuses to pretend otherwise:

- The crossing (Apply) cannot be reached until the toll step has
  been run *in this window* and the ticket shows PAID. And the
  button is only the visible half of the lock: the apply engine
  re-verifies the toll against the PC again when the crossing
  begins, so a gate that has closed since the toll was paid closes
  the crossing too, and the owner is brought back to the toll
  screen to see why.
- Every engine call runs on a worker thread; the screen animates
  from the same progress events the CLI prints, so what the HUD
  shows is the work actually happening — never a decoration
  running ahead of it.
- Every outcome has a designed screen: drivers up to date, a
  download that didn't stage, a toll refused (with the way
  forward), a crossing held mid-river, a restart that's needed
  (reported, never forced). Raw error text never reaches this
  window — the designed failures carry plain-language messages,
  and anything unexpected lands on a plain "something went wrong"
  screen with the technical detail kept in the log.

The engine entry points are module-level functions so tests can
stand in fakes for the whole pipeline without touching the seams.
"""

from __future__ import annotations

import logging
import sys

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from . import cli, theme, toll
from .apply import (
    EVENT_APPLY_HELD,
    EVENT_CROSSING_COMPLETED,
    EVENT_CROSSING_STARTED,
    EVENT_REBOOT_REQUIRED,
)
from .download import (
    EVENT_UPDATE_COMPLETED,
    EVENT_UPDATE_PROGRESS,
    EVENT_UPDATE_STARTED,
    summarize_results,
)
from .hud import DriverRow, JourneyWidget, SweepWidget, TollTicketWidget
from .model import STATUS_UPDATE_AVAILABLE
from .toll import EVENT_TOLL_PAID, TollUnpaid

log = logging.getLogger("charon.gui")


# ----------------------------------------------------------------------
# Engine adapters (the real composed pipeline; tests substitute)
# ----------------------------------------------------------------------


def engine_scan():
    """Phase 1+2's question: what drivers are here, what's outdated."""
    return cli.run_scan()


def engine_download(report, selection, emit):
    """Phase 2: stage the selected drivers (installs nothing)."""
    return cli.run_download(report, selection, emit=emit)


def engine_toll(emit):
    """Phase 3: create the restore point and verify it exists."""
    return toll.pay_toll(emit=emit)


def engine_apply(emit):
    """Phase 4: the crossing, behind the gate, one at a time."""
    return cli.run_apply(emit=emit)


def _user_message(exc: Exception, fallback: str) -> str:
    """A designed failure's own words, or a plain fallback. Raw
    exception text is for the log, never the screen."""
    message = getattr(exc, "user_message", "")
    return message if message else fallback


# ----------------------------------------------------------------------
# Worker plumbing: engine on a thread, events on the UI thread
# ----------------------------------------------------------------------


class EngineWorker(QThread):
    """Runs one engine callable; forwards its progress events and
    exactly one terminal signal (succeeded with the result, or
    failed with the exception object — designed failures travel
    as data, to be rendered as designed screens)."""

    event_emitted = Signal(object)
    succeeded = Signal(object)
    failed = Signal(object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:  # noqa: D102 — QThread's entry point
        try:
            result = self._fn(self.event_emitted.emit)
        except Exception as exc:  # designed failures included, by design
            log.warning("engine step failed: %s", exc, exc_info=True)
            self.failed.emit(exc)
            return
        self.succeeded.emit(result)


# ----------------------------------------------------------------------
# The window
# ----------------------------------------------------------------------

_PAGE_WELCOME = 0
_PAGE_SCANNING = 1
_PAGE_MANIFEST = 2
_PAGE_TOLL = 3
_PAGE_MESSAGE = 4


class MainWindow(QMainWindow):
    """Charon's one window: the journey bar, the screens, the line."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("CHARON.EXE — Sentinel Safeguard Driver Updater")
        self.resize(880, 640)
        self._report = None
        self._download_results: list = []
        self._toll_record = None
        self._worker: EngineWorker | None = None
        self._active_step = ""
        self._rows: dict[str, DriverRow] = {}
        self._row_by_name: dict[str, DriverRow] = {}
        self._row_by_title: dict[str, DriverRow] = {}
        self._message_primary_action = None

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 14, 18, 12)
        root.setSpacing(10)

        self.journey = JourneyWidget()
        root.addWidget(self.journey)

        self.pages = QStackedWidget()
        root.addWidget(self.pages, 1)

        self.status_line = QLabel("")
        self.status_line.setObjectName("statusLine")
        root.addWidget(self.status_line)

        self._build_welcome_page()
        self._build_scanning_page()
        self._build_manifest_page()
        self._build_toll_page()
        self._build_message_page()
        self.pages.setCurrentIndex(_PAGE_WELCOME)

    # ------------------------------------------------------------------
    # Page construction
    # ------------------------------------------------------------------

    def _page(self) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(10)
        self.pages.addWidget(page)
        return page, layout

    def _title_block(self, layout, title: str, hint: str) -> None:
        title_label = QLabel(title)
        title_label.setObjectName("screenTitle")
        layout.addWidget(title_label)
        if hint:
            hint_label = QLabel(hint)
            hint_label.setObjectName("screenHint")
            hint_label.setWordWrap(True)
            layout.addWidget(hint_label)

    def _build_welcome_page(self) -> None:
        page, layout = self._page()
        layout.addStretch(1)
        wordmark = QLabel("CHARON.EXE")
        wordmark.setObjectName("wordmark")
        wordmark.setAlignment(Qt.AlignCenter)
        layout.addWidget(wordmark)
        tagline = QLabel("Every driver ferried safely to the other side.")
        tagline.setObjectName("tagline")
        tagline.setAlignment(Qt.AlignCenter)
        layout.addWidget(tagline)
        intro = QLabel(
            "Charon checks every driver on this PC against what "
            "Windows Update itself offers, downloads the updates, "
            "creates a restore point — the toll — and only then "
            "installs, one driver at a time. Nothing crosses "
            "without the toll."
        )
        intro.setWordWrap(True)
        intro.setAlignment(Qt.AlignCenter)
        layout.addWidget(intro)
        self.btn_scan = QPushButton("Scan for driver updates")
        self.btn_scan.setObjectName("primary")
        self.btn_scan.clicked.connect(self.start_scan)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.btn_scan)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(1)

    def _build_scanning_page(self) -> None:
        page, layout = self._page()
        self._title_block(
            layout,
            "Scanning this PC",
            "Looking at every installed driver and asking Windows "
            "Update what it offers. Nothing changes during a scan.",
        )
        self.sweep = SweepWidget()
        layout.addWidget(self.sweep, 1)
        self.scan_status = QLabel("Looking…")
        self.scan_status.setObjectName("screenHint")
        self.scan_status.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.scan_status)

    def _build_manifest_page(self) -> None:
        page, layout = self._page()
        self.manifest_title = QLabel("Your drivers")
        self.manifest_title.setObjectName("screenTitle")
        layout.addWidget(self.manifest_title)
        self.manifest_hint = QLabel("")
        self.manifest_hint.setObjectName("screenHint")
        self.manifest_hint.setWordWrap(True)
        layout.addWidget(self.manifest_hint)

        self.rows_scroll = QScrollArea()
        self.rows_scroll.setWidgetResizable(True)
        self.rows_host = QWidget()
        self.rows_layout = QVBoxLayout(self.rows_host)
        self.rows_layout.setContentsMargins(2, 2, 2, 2)
        self.rows_layout.setSpacing(8)
        self.rows_layout.addStretch(1)
        self.rows_scroll.setWidget(self.rows_host)
        layout.addWidget(self.rows_scroll, 1)

        self.empty_panel = QLabel(
            "Every driver on this PC is up to date. There's "
            "nothing for the ferry to carry today."
        )
        self.empty_panel.setObjectName("screenHint")
        self.empty_panel.setWordWrap(True)
        self.empty_panel.setVisible(False)
        layout.addWidget(self.empty_panel)

        self.reboot_banner = QLabel("")
        self.reboot_banner.setWordWrap(True)
        self.reboot_banner.setStyleSheet(
            f"color: {theme.ACCENT}; border: 1px solid {theme.PANEL_BORDER}; "
            f"border-radius: 8px; padding: 8px; "
            f"background: {theme.PANEL};"
        )
        self.reboot_banner.setVisible(False)
        layout.addWidget(self.reboot_banner)

        footer = QHBoxLayout()
        self.btn_rescan = QPushButton("Scan again")
        self.btn_rescan.clicked.connect(self.start_scan)
        footer.addWidget(self.btn_rescan)
        footer.addStretch(1)
        self.btn_download = QPushButton("Download selected drivers")
        self.btn_download.setObjectName("primary")
        self.btn_download.clicked.connect(self.start_download)
        footer.addWidget(self.btn_download)
        layout.addLayout(footer)

    def _build_toll_page(self) -> None:
        page, layout = self._page()
        self._title_block(
            layout,
            "The toll",
            "One restore point, created and verified, stands "
            "between this PC and every install. No toll, no "
            "crossing — that rule has no exceptions.",
        )
        self.ticket = TollTicketWidget()
        layout.addWidget(self.ticket)

        self.toll_refusal = QLabel("")
        self.toll_refusal.setWordWrap(True)
        self.toll_refusal.setStyleSheet(
            f"color: {theme.RED_TEXT}; border: 1px solid {theme.RED}; "
            f"border-radius: 8px; padding: 8px; "
            f"background: {theme.RED};"
        )
        self.toll_refusal.setVisible(False)
        layout.addWidget(self.toll_refusal)
        layout.addStretch(1)

        footer = QHBoxLayout()
        self.btn_back_drivers = QPushButton("Back to the drivers")
        self.btn_back_drivers.clicked.connect(
            lambda: self.pages.setCurrentIndex(_PAGE_MANIFEST)
        )
        footer.addWidget(self.btn_back_drivers)
        footer.addStretch(1)
        self.btn_pay = QPushButton("Pay the toll")
        self.btn_pay.setObjectName("primary")
        self.btn_pay.clicked.connect(self.start_toll)
        footer.addWidget(self.btn_pay)
        self.btn_cross = QPushButton("Begin the crossing")
        self.btn_cross.setObjectName("primary")
        self.btn_cross.setEnabled(False)  # the lock: real, see start_apply
        self.btn_cross.clicked.connect(self.start_apply)
        footer.addWidget(self.btn_cross)
        layout.addLayout(footer)

    def _build_message_page(self) -> None:
        page, layout = self._page()
        self.message_title = QLabel("")
        self.message_title.setObjectName("screenTitle")
        layout.addWidget(self.message_title)
        self.message_body = QLabel("")
        self.message_body.setWordWrap(True)
        layout.addWidget(self.message_body)
        layout.addStretch(1)
        footer = QHBoxLayout()
        footer.addStretch(1)
        self.btn_message_primary = QPushButton("Scan again")
        self.btn_message_primary.setObjectName("primary")
        self.btn_message_primary.clicked.connect(self._on_message_primary)
        footer.addWidget(self.btn_message_primary)
        layout.addLayout(footer)

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _set_status(self, text: str) -> None:
        self.status_line.setText(text)

    def _busy(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        """Refuse to close mid-step: destroying a window whose worker
        thread is still running makes Qt abort the whole process —
        on a real PC that reads as a crash, mid-scan or mid-crossing.
        The close is refused in plain language instead; once the step
        finishes, the window closes as asked."""
        if self._busy():
            self._set_status(
                "Charon is in the middle of a step — let it finish "
                "before closing the window."
            )
            event.ignore()
            return
        super().closeEvent(event)

    def _run_worker(self, fn, on_success, on_failure) -> None:
        if self._busy():
            return  # one engine step at a time — the flow is linear
        self._worker = EngineWorker(fn, parent=self)
        self._worker.event_emitted.connect(self._on_engine_event)
        self._worker.succeeded.connect(on_success)
        self._worker.failed.connect(on_failure)
        self._worker.start()

    def _show_message(self, title: str, body: str, primary_text: str, action) -> None:
        self.message_title.setText(title)
        self.message_body.setText(body)
        self.btn_message_primary.setText(primary_text)
        self._message_primary_action = action
        self.pages.setCurrentIndex(_PAGE_MESSAGE)
        self._set_status("")

    def _on_message_primary(self) -> None:
        action = self._message_primary_action or self.start_scan
        action()

    def _row_for_event(self, event) -> DriverRow | None:
        if event.device_name and event.device_name in self._row_by_name:
            return self._row_by_name[event.device_name]
        if event.title and event.title in self._row_by_title:
            return self._row_by_title[event.title]
        return None

    # ------------------------------------------------------------------
    # Step 1 — Scan
    # ------------------------------------------------------------------

    def start_scan(self) -> None:
        if self._busy():
            return  # never re-enter mid-step; the flow is linear
        self._active_step = "scan"
        self.pages.setCurrentIndex(_PAGE_SCANNING)
        self.journey.set_journey(0)
        self.scan_status.setText("Looking at every driver on this PC…")
        self._set_status("Scanning…")
        self.sweep.set_blips(0)
        self.sweep.start()
        self._run_worker(
            lambda emit: engine_scan(),
            self._on_scan_done,
            self._on_scan_failed,
        )

    def _on_scan_done(self, report) -> None:
        self.sweep.stop()
        self.sweep.set_blips(len(report.devices))
        self._report = report
        self._build_rows(report)
        self.journey.set_journey(1)
        self.pages.setCurrentIndex(_PAGE_MANIFEST)
        self._set_status(report.summary_line())

    def _on_scan_failed(self, exc: Exception) -> None:
        self.sweep.stop()
        self._show_message(
            "The scan couldn't run",
            _user_message(
                exc,
                "Charon couldn't read this PC's drivers this time. "
                "Nothing was changed.",
            )
            + " Try again in a moment; if it keeps failing, "
            "restarting the PC usually clears it.",
            "Try the scan again",
            self.start_scan,
        )

    def _build_rows(self, report) -> None:
        self.reboot_banner.setVisible(False)
        # Clear any previous manifest.
        while self.rows_layout.count() > 1:
            item = self.rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._rows.clear()
        self._row_by_name.clear()
        self._row_by_title.clear()

        updatable = 0
        for device in report.devices:
            driver = device.driver
            key = driver.device_id or driver.device_name
            detail = f"Installed: version {driver.version or 'unknown'}"
            row = DriverRow(key, driver.device_name, detail)
            if device.status == STATUS_UPDATE_AVAILABLE and device.update:
                updatable += 1
                detail += (
                    f"  →  available: version "
                    f"{device.update.version or 'unknown'}"
                )
                row.set_state("info", "UPDATE READY", detail)
                row.set_selectable(True)
                row.checkbox.toggled.connect(self._refresh_download_button)
                self._row_by_title[device.update.title] = row
            elif device.status == "up_to_date":
                row.set_state("waiting", "UP TO DATE", detail)
                row.set_selectable(False)
                row.checkbox.setChecked(False)
            else:
                row.set_state("waiting", "NO WORD", detail)
                row.set_selectable(False)
                row.checkbox.setChecked(False)
            self.rows_layout.insertWidget(self.rows_layout.count() - 1, row)
            self._rows[key] = row
            self._row_by_name[driver.device_name] = row

        has_updates = updatable > 0
        self.empty_panel.setVisible(not has_updates)
        self.btn_download.setVisible(has_updates)
        self.manifest_title.setText("Your drivers")
        self.manifest_hint.setText(
            "Ticked drivers will be downloaded first. Nothing is "
            "installed until the toll is paid and you begin the "
            "crossing."
            if has_updates
            else "Windows Update has nothing newer for this PC right now."
        )
        self._refresh_download_button()

    def _refresh_download_button(self) -> None:
        chosen = sum(
            1
            for row in self._rows.values()
            if row.selectable and row.is_checked()
        )
        self.btn_download.setText(
            f"Download {chosen} selected driver{'s' if chosen != 1 else ''}"
            if chosen
            else "Download selected drivers"
        )
        self.btn_download.setEnabled(chosen > 0)

    # ------------------------------------------------------------------
    # Step 2 — Download (staged only)
    # ------------------------------------------------------------------

    def start_download(self) -> None:
        if self._busy():
            return
        selection = [
            row.key
            for row in self._rows.values()
            if row.selectable and row.is_checked()
        ]
        if not selection or self._report is None:
            return
        self.manifest_title.setText("Downloading your drivers")
        self.manifest_hint.setText(
            "Fetching the updates into the Windows Update cache. "
            "Nothing is installed yet — the toll comes first."
        )
        self._active_step = "download"
        self.btn_download.setEnabled(False)
        self.btn_rescan.setEnabled(False)
        for row in self._rows.values():
            row.set_selectable(False)
        self.journey.set_journey(1)
        self._set_status("Downloading…")
        report = self._report
        self._run_worker(
            lambda emit: engine_download(report, selection, emit),
            self._on_download_done,
            self._on_download_failed,
        )

    def _on_download_event(self, event) -> None:
        row = self._row_for_event(event)
        if event.kind == EVENT_UPDATE_STARTED and row is not None:
            row.set_state("working", "DOWNLOADING")
            row.set_progress(None)
        elif event.kind == EVENT_UPDATE_PROGRESS and row is not None:
            if event.percent is not None:
                row.set_progress(event.percent)
        elif event.kind == EVENT_UPDATE_COMPLETED and row is not None:
            row.hide_progress()
            if event.status == "succeeded":
                row.set_state("done", "DOWNLOADED")
            elif event.status == "succeeded_with_errors":
                row.set_state("warn", "DOWNLOADED — WARNINGS")
            elif event.status == "no_longer_available":
                row.set_state("warn", "NO LONGER OFFERED")
            else:
                row.set_state("bad", "NOT DOWNLOADED")

    def _restore_select_mode(self) -> None:
        """After a download attempt, hand the manifest back in its
        picking state so the owner can adjust and try again."""
        if self._report is None:
            return
        updatable_keys = {
            (d.driver.device_id or d.driver.device_name)
            for d in self._report.devices
            if d.status == STATUS_UPDATE_AVAILABLE and d.update
        }
        for key, row in self._rows.items():
            row.set_selectable(key in updatable_keys)
        self.btn_download.setVisible(bool(updatable_keys))
        self._refresh_download_button()

    def _on_download_done(self, payload) -> None:
        results, _ledger = payload
        self._download_results = list(results)
        self.btn_rescan.setEnabled(True)
        self._restore_select_mode()
        staged = [r for r in results if r.staged]
        if not staged:
            self._show_message(
                "Nothing was downloaded",
                summarize_results(results)
                + " When nothing is aboard, there's nothing to "
                "cross — and the toll is only paid for a real "
                "crossing.",
                "Back to your drivers",
                lambda: self.pages.setCurrentIndex(_PAGE_MANIFEST),
            )
            return
        self._set_status(summarize_results(results))
        self._enter_toll_page()

    def _on_download_failed(self, exc: Exception) -> None:
        self.btn_rescan.setEnabled(True)
        self._restore_select_mode()
        self._show_message(
            "The download couldn't begin",
            _user_message(
                exc,
                "Windows Update didn't answer, so nothing was "
                "downloaded — and nothing was installed.",
            ),
            "Back to your drivers",
            lambda: self.pages.setCurrentIndex(_PAGE_MANIFEST),
        )

    # ------------------------------------------------------------------
    # Step 3 — The toll
    # ------------------------------------------------------------------

    def _enter_toll_page(self) -> None:
        self._toll_record = None
        self.ticket.reset()
        self.toll_refusal.setVisible(False)
        self.btn_cross.setEnabled(False)
        self.btn_pay.setEnabled(True)
        self.btn_pay.setText("Pay the toll")
        self.journey.set_journey(2)
        self.pages.setCurrentIndex(_PAGE_TOLL)
        self._set_status(
            "The drivers are aboard. The toll is next — nothing "
            "installs until it's paid and verified."
        )

    def start_toll(self) -> None:
        if self._busy():
            return
        self._active_step = "toll"
        self.btn_pay.setEnabled(False)
        self.toll_refusal.setVisible(False)
        self._set_status("Paying the toll…")
        self._run_worker(
            lambda emit: engine_toll(emit),
            self._on_toll_paid,
            self._on_toll_refused,
        )

    def _on_toll_event(self, event) -> None:
        if event.kind == EVENT_TOLL_PAID:
            return  # the success handler stamps the ticket
        if event.message:
            self._set_status(event.message)

    def _on_toll_paid(self, record) -> None:
        self._toll_record = record
        when = record.created_at.astimezone().strftime("%B %d, %H:%M")
        self.ticket.show_paid(record.sequence_number, when)
        self.journey.set_journey(2, toll_paid=True)
        self.btn_cross.setEnabled(True)
        self.btn_pay.setText("Toll paid ✓")
        self._set_status(
            f"Toll paid — restore point #{record.sequence_number}, "
            f"verified. The crossing is unlocked."
        )

    def _on_toll_refused(self, exc: Exception) -> None:
        self.btn_pay.setEnabled(True)
        self.btn_pay.setText("Try paying the toll again")
        message = _user_message(
            exc, "The toll could not be paid, so the ferry doesn't move."
        )
        next_step = getattr(exc, "next_step", "")
        self.toll_refusal.setText(
            message + (f"\n\n{next_step}" if next_step else "")
        )
        self.toll_refusal.setVisible(True)
        self._set_status("The toll is unpaid — the crossing stays locked.")

    # ------------------------------------------------------------------
    # Step 4 — Apply (the crossing)
    # ------------------------------------------------------------------

    def start_apply(self) -> None:
        """Begin the crossing. The button unlocks only after the
        toll — and even then, the engine re-verifies the toll
        against the PC as the crossing begins. If the gate has
        closed, the owner lands back here with the reason."""
        if self._toll_record is None:
            self.pages.setCurrentIndex(_PAGE_TOLL)
            self._set_status(
                "The toll hasn't been paid in this visit — the "
                "crossing stays locked until it is."
            )
            return
        if self._busy():
            return
        self._active_step = "apply"
        # The manifest's picking controls retire for the crossing:
        # nothing may be re-scanned or re-picked mid-river.
        self.btn_download.setVisible(False)
        self.btn_rescan.setEnabled(False)
        self._prepare_apply_rows()
        self.pages.setCurrentIndex(_PAGE_MANIFEST)
        self.manifest_title.setText("The crossing")
        self.manifest_hint.setText(
            "Installing one driver at a time, and checking each "
            "device as it crosses. If a driver fails, the line "
            "stops — the rest wait, and the restore point is the "
            "way back."
        )
        self.journey.set_journey(3, toll_paid=True)
        self._set_status("Crossing…")
        self._run_worker(
            lambda emit: engine_apply(emit),
            self._on_apply_done,
            self._on_apply_failed,
        )

    def _prepare_apply_rows(self) -> None:
        staged_keys = set()
        for result in self._download_results:
            staged_keys.add(result.entry.key)
        for key, row in self._rows.items():
            row.set_selectable(False)
            row.hide_progress()
            if key in staged_keys:
                row.set_state("waiting", "ABOARD — READY")
            else:
                row.set_state("waiting", "NOT ABOARD")

    def _on_apply_event(self, event) -> None:
        row = self._row_for_event(event)
        if event.kind == EVENT_CROSSING_STARTED and row is not None:
            row.set_state("working", "INSTALLING")
            row.set_progress(None)
        elif event.kind == EVENT_UPDATE_PROGRESS and row is not None:
            if event.percent is not None:
                row.set_progress(event.percent)
        elif event.kind == EVENT_CROSSING_COMPLETED and row is not None:
            row.hide_progress()
            status = event.status
            if status == "succeeded":
                row.set_state("done", "ACROSS ✓")
            elif status == "succeeded_with_errors":
                row.set_state("warn", "ACROSS — WARNINGS")
            elif status == "no_longer_available":
                row.set_state("warn", "NO LONGER OFFERED")
            elif status == "eula_not_accepted":
                row.set_state("warn", "TERMS NOT ACCEPTED")
            elif status == "skipped":
                row.set_state("held", "HELD BACK")
            else:
                row.set_state("bad", "DIDN'T CROSS")
        elif event.kind == EVENT_APPLY_HELD:
            if event.message:
                self._set_status(event.message)
        elif event.kind == EVENT_REBOOT_REQUIRED:
            if event.message:
                self.reboot_banner.setText(event.message)
                self.reboot_banner.setVisible(True)
                self.pages.setCurrentIndex(_PAGE_MANIFEST)

    def _on_apply_done(self, payload) -> None:
        results, toll_record, _path = payload
        self.btn_rescan.setEnabled(True)
        self._restore_select_mode()
        self.journey.set_complete()
        failed = [r for r in results if not r.applied]
        title = (
            "The crossing is complete"
            if not failed
            else "The ferry has docked"
        )
        self._show_message(
            title,
            _apply_summary(results, toll_record),
            "Scan again",
            self.start_scan,
        )

    def _on_apply_failed(self, exc: Exception) -> None:
        self.btn_rescan.setEnabled(True)
        self._restore_select_mode()
        if isinstance(exc, TollUnpaid):
            # The gate closed between the toll and the crossing —
            # back to the toll screen, with the gate's own reason.
            self._enter_toll_page()
            self._on_toll_refused(exc)
            return
        self._show_message(
            "The crossing couldn't begin",
            _user_message(
                exc,
                "Windows Update didn't answer, so nothing was "
                "installed. What already crossed stays crossed — "
                "the record is in Charon's ledger.",
            ),
            "Back to the toll",
            lambda: self.pages.setCurrentIndex(_PAGE_TOLL),
        )

    # ------------------------------------------------------------------
    # Event routing
    # ------------------------------------------------------------------

    def _on_engine_event(self, event) -> None:
        kind = getattr(event, "kind", "")
        if kind.startswith("toll_"):
            self._on_toll_event(event)
        elif kind.startswith(("crossing_", "apply_", "reboot_")):
            self._on_apply_event(event)
        elif self._active_step == "apply":
            self._on_apply_event(event)
        else:
            self._on_download_event(event)


def _apply_summary(results, toll_record) -> str:
    """The done-screen summary: the engine's own honest sentence-set."""
    from .apply import summarize_apply_results

    return summarize_apply_results(list(results), toll_record)


def create_window() -> MainWindow:
    """Build the window (used by main() and by the GUI tests)."""
    return MainWindow()


def main(argv: list[str] | None = None) -> int:
    """Launch the HUD. Returns the application's exit code."""
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setStyleSheet(theme.stylesheet())
    window = create_window()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
