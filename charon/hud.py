"""CHARON.EXE — the HUD widgets (Phase 5).

The animated instruments of the crossing:

- :class:`SweepWidget` — the scan sweep: a lantern's arc sweeping
  the dark while Charon looks at every driver on the PC. It spins
  only while a scan is genuinely running, and its blips are the
  devices actually found so far — decoration that tells the truth.
- :class:`JourneyWidget` — the river: Scan → Download → Toll →
  Apply as four stations on a flowing line, the ferry gliding
  toward the station the run is really on. The toll station shows
  its lock until the toll is paid.
- :class:`DriverRow` — one passenger's card: name, versions, a
  status chip, and a progress bar that moves only on real events.
- :class:`TollTicketWidget` — the ticket: the toll, printed with
  the restore point's number, and stamped PAID — the stamp lands
  only when verification has actually passed.

Painting is done with the theme palette only. Animation is plain
timer-driven state, so every widget renders deterministically —
on a real display and in the offscreen audit screenshots alike.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from . import theme

# Driver-row states → (chip text color, chip border, chip fill).
# Every state is text on the raised face except "bad": danger is
# the fleet treatment — white text on the danger red itself, a
# fill that cannot be missed or misread as decoration.
CHIP_STYLES = {
    "waiting": (theme.TEXT_FAINT, theme.PANEL_BORDER, theme.INPUT_BG),
    "working": (theme.PRIMARY, theme.PANEL_BORDER, theme.INPUT_BG),
    "done": (theme.PRIMARY, theme.PANEL_BORDER, theme.INPUT_BG),
    "warn": (theme.ACCENT, theme.PANEL_BORDER, theme.INPUT_BG),
    "bad": (theme.RED_TEXT, theme.RED, theme.RED),
    "held": (theme.ACCENT, theme.PANEL_BORDER, theme.INPUT_BG),
    "info": (theme.TEXT, theme.PANEL_BORDER, theme.INPUT_BG),
}


def _color(hex_value: str, alpha: int = 255) -> QColor:
    color = QColor(hex_value)
    color.setAlpha(alpha)
    return color


# ----------------------------------------------------------------------
# The scan sweep
# ----------------------------------------------------------------------


class SweepWidget(QWidget):
    """A lantern arc sweeping the dark, with blips for finds."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(220, 220)
        self._angle = 0.0
        self._blips = 0
        self._running = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        self._running = True
        self._timer.start(40)

    def stop(self) -> None:
        self._running = False
        self._timer.stop()
        self.update()

    def set_blips(self, count: int) -> None:
        self._blips = max(0, count)
        self.update()

    def _tick(self) -> None:
        self._angle = (self._angle + 4.0) % 360.0
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        side = min(self.width(), self.height())
        cx, cy = self.width() / 2, self.height() / 2
        radius = side / 2 - 8

        # Concentric soundings.
        for frac in (1.0, 0.66, 0.33):
            painter.setPen(QPen(_color(theme.PANEL_BORDER), 1))
            painter.setBrush(Qt.NoBrush)
            r = radius * frac
            painter.drawEllipse(QPointF(cx, cy), r, r)
        # Cross lines.
        painter.setPen(QPen(_color(theme.PANEL_BORDER, 160), 1))
        painter.drawLine(
            QPointF(cx - radius, cy), QPointF(cx + radius, cy)
        )
        painter.drawLine(
            QPointF(cx, cy - radius), QPointF(cx, cy + radius)
        )

        # The sweep wedge: a soft cone trailing the lantern line.
        if self._running:
            path = QPainterPath()
            path.moveTo(cx, cy)
            path.arcTo(
                QRectF(cx - radius, cy - radius, radius * 2, radius * 2),
                -self._angle,
                55,
            )
            path.closeSubpath()
            painter.fillPath(path, _color(theme.PRIMARY, 34))
            rad = math.radians(self._angle)
            painter.setPen(QPen(_color(theme.PRIMARY), 2))
            painter.drawLine(
                QPointF(cx, cy),
                QPointF(cx + radius * math.cos(rad), cy - radius * math.sin(rad)),
            )

        # Blips: one per device found, placed on the rings.
        painter.setBrush(_color(theme.PRIMARY))
        painter.setPen(Qt.NoPen)
        for i in range(self._blips):
            golden = i * 2.39996  # golden-angle spread, no clumping
            ring = radius * (0.33 + 0.22 * (i % 3))
            x = cx + ring * math.cos(golden)
            y = cy + ring * math.sin(golden)
            painter.drawEllipse(QPointF(x, y), 3.2, 3.2)

        # The lantern itself.
        painter.setBrush(_color(theme.PRIMARY))
        painter.drawEllipse(QPointF(cx, cy), 4.5, 4.5)
        painter.end()


# ----------------------------------------------------------------------
# The river journey
# ----------------------------------------------------------------------

STATIONS = ("Scan", "Download", "The toll", "Apply")


class JourneyWidget(QWidget):
    """The crossing as a river with four stations and one ferry."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(86)
        self._current = 0          # station the run is on
        self._completed = -1       # highest completed station
        self._toll_paid = False
        self._ferry = 0.0          # animated position, in stations
        self._phase = 0.0          # river flow / pulse phase
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(50)

    def set_journey(self, current: int, toll_paid: bool = False) -> None:
        """Point the ferry at a station; completed = all before it."""
        self._current = max(0, min(len(STATIONS) - 1, current))
        self._completed = self._current - 1
        self._toll_paid = toll_paid
        self.update()

    def set_complete(self) -> None:
        self._completed = len(STATIONS) - 1
        self._current = len(STATIONS) - 1
        self.update()

    def _tick(self) -> None:
        target = float(self._current)
        if abs(self._ferry - target) > 0.005:
            self._ferry += (target - self._ferry) * 0.12
        self._phase = (self._phase + 0.06) % (2 * math.pi)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        width, height = self.width(), self.height()
        margin = 70
        y = height / 2 - 8
        span = max(1, width - margin * 2)
        step = span / (len(STATIONS) - 1)

        def station_x(i: int) -> float:
            return margin + step * i

        # The river: a gently flowing sine line.
        river = QPainterPath()
        for px in range(0, int(span) + 1, 6):
            wave = math.sin(px / 46.0 + self._phase) * 3.0
            if px == 0:
                river.moveTo(margin + px, y + wave)
            else:
                river.lineTo(margin + px, y + wave)
        painter.setPen(QPen(_color(theme.PANEL_BORDER), 2))
        painter.drawPath(river)
        crossed = QPainterPath()
        ferry_x = station_x(0) + step * self._ferry
        for px in range(0, max(0, int(ferry_x - margin)) + 1, 6):
            wave = math.sin(px / 46.0 + self._phase) * 3.0
            if px == 0:
                crossed.moveTo(margin + px, y + wave)
            else:
                crossed.lineTo(margin + px, y + wave)
        painter.setPen(QPen(_color(theme.PRIMARY), 2))
        painter.drawPath(crossed)

        # Stations.
        for i, name in enumerate(STATIONS):
            x = station_x(i)
            done = i <= self._completed or (i == 2 and self._toll_paid and self._completed >= 2)
            active = i == self._current and not done
            if done:
                painter.setBrush(_color(theme.PRIMARY))
                painter.setPen(QPen(_color(theme.PRIMARY), 2))
            elif active:
                pulse = 0.55 + 0.45 * math.sin(self._phase * 2)
                painter.setBrush(_color(theme.INPUT_BG))
                painter.setPen(
                    QPen(_color(theme.PRIMARY, int(120 + 120 * pulse)), 2)
                )
            else:
                painter.setBrush(_color(theme.PANEL))
                painter.setPen(QPen(_color(theme.PANEL_BORDER), 2))
            painter.drawEllipse(QPointF(x, y), 13, 13)
            painter.setPen(QPen(_color(theme.TEXT), 1))
            painter.setFont(self.font())
            if done:
                # The check sits on the aqua fill — it is drawn in
                # the dark text that fill is paired with.
                painter.setPen(QPen(_color(theme.PRIMARY_DARK_TEXT), 1))
                painter.drawText(
                    QRectF(x - 13, y - 9, 26, 18),
                    Qt.AlignCenter,
                    "✓",
                )
            elif i == 2 and not self._toll_paid:
                # A small lock, drawn not glyphed — every font has it.
                painter.setPen(QPen(_color(theme.TEXT_FAINT), 1.6))
                painter.setBrush(Qt.NoBrush)
                painter.drawArc(QRectF(x - 4.5, y - 8, 9, 9), 0, 180 * 16)
                painter.setBrush(_color(theme.TEXT_FAINT))
                painter.drawRoundedRect(QRectF(x - 6, y - 3, 12, 9), 2, 2)
            painter.setPen(QPen(_color(theme.TEXT_FAINT), 1))
            painter.drawText(
                QRectF(x - 45, y + 16, 90, 16), Qt.AlignCenter, name
            )

        # The ferry: a small lantern-lit diamond riding the river.
        fy = y + math.sin((ferry_x - margin) / 46.0 + self._phase) * 3.0
        boat = QPainterPath()
        boat.moveTo(ferry_x, fy - 16)
        boat.lineTo(ferry_x + 7, fy - 7)
        boat.lineTo(ferry_x, fy - 2)
        boat.lineTo(ferry_x - 7, fy - 7)
        boat.closeSubpath()
        painter.fillPath(boat, _color(theme.PRIMARY))
        painter.setBrush(_color(theme.PRIMARY))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(QPointF(ferry_x, fy - 9), 2.2, 2.2)
        painter.end()


# ----------------------------------------------------------------------
# One passenger's card
# ----------------------------------------------------------------------


class DriverRow(QFrame):
    """A driver, its versions, its state — one row of the manifest."""

    def __init__(self, key: str, name: str, detail: str, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        self.key = key
        self.name = name
        self.selectable = True
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(10)

        self.checkbox = QCheckBox()
        self.checkbox.setChecked(True)
        layout.addWidget(self.checkbox)

        text_box = QVBoxLayout()
        text_box.setSpacing(1)
        self.name_label = QLabel(name)
        self.name_label.setStyleSheet("font-weight: 600;")
        self.detail_label = QLabel(detail)
        self.detail_label.setStyleSheet(
            f"color: {theme.TEXT_FAINT}; font-size: 12px;"
        )
        text_box.addWidget(self.name_label)
        text_box.addWidget(self.detail_label)
        layout.addLayout(text_box, 1)

        self.progress = QProgressBar()
        self.progress.setFixedWidth(130)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.chip = QLabel("WAITING")
        self.chip.setAlignment(Qt.AlignCenter)
        self.chip.setFixedWidth(128)
        self.set_state("waiting", "WAITING")
        layout.addWidget(self.chip)

    # -- selection -------------------------------------------------
    def set_selectable(self, selectable: bool) -> None:
        self.selectable = selectable
        self.checkbox.setVisible(selectable)

    def is_checked(self) -> bool:
        return self.checkbox.isChecked()

    # -- state -----------------------------------------------------
    def set_state(self, state: str, text: str, detail: str = "") -> None:
        fg, border, fill = CHIP_STYLES.get(state, CHIP_STYLES["info"])
        self.chip.setText(text)
        self.chip.setStyleSheet(
            f"color: {fg}; border: 1px solid {border}; "
            f"border-radius: 8px; padding: 3px 6px; font-size: 11px; "
            f"background: {fill};"
        )
        if detail:
            self.detail_label.setText(detail)

    def set_progress(self, percent) -> None:
        """``None`` = working, amount unknown (busy bar)."""
        self.progress.setVisible(True)
        if percent is None:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(max(0, min(100, int(percent))))

    def hide_progress(self) -> None:
        self.progress.setVisible(False)


# ----------------------------------------------------------------------
# The toll ticket
# ----------------------------------------------------------------------


class _StampOverlay(QWidget):
    """The PAID stamp, painted as an overlay ABOVE the ticket.

    It must be an overlay: the ticket's labels paint their own
    backgrounds (the app stylesheet gives every widget one), so a
    stamp drawn in the ticket's own paint pass would be painted
    over by its children and show only in slices. As the topmost
    child, the stamp lands on the finished ticket — like ink.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.progress = 0.0  # 0 = unstamped, 1 = stamp landed
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        if self.progress < 1.0:
            self._timer.start(30)

    def reset(self) -> None:
        self._timer.stop()
        self.progress = 0.0
        self.update()

    def set_progress(self, value: float) -> None:
        self.progress = max(0.0, min(1.0, value))
        self.update()

    def _tick(self) -> None:
        self.set_progress(self.progress + 0.09)
        if self.progress >= 1.0:
            self._timer.stop()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        # Deliberately no super() call: the stylesheet's widget
        # background must not paint here — the overlay is glass.
        if self.progress <= 0.0:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        progress = self.progress
        cx = self.width() * 0.74
        cy = self.height() * 0.52
        scale = 1.25 - 0.25 * progress
        painter.translate(cx, cy)
        painter.rotate(-12)
        painter.scale(scale, scale)
        painter.setOpacity(progress)
        w, h = 168, 46
        painter.setPen(QPen(_color(theme.STAMP), 3))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(QRectF(-w / 2, -h / 2, w, h), 8, 8)
        painter.setPen(QPen(_color(theme.STAMP), 1))
        font = painter.font()
        font.setPointSize(17)
        font.setBold(True)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 4)
        painter.setFont(font)
        painter.drawText(
            QRectF(-w / 2, -h / 2, w, h), Qt.AlignCenter, "TOLL PAID"
        )
        painter.end()


class TollTicketWidget(QFrame):
    """The toll, printed: restore point number, time — and the stamp.

    The stamp starts invisible and lands (fade + settle) only when
    :meth:`show_paid` is called — which the window calls only when
    the toll machinery has verified the point. A ticket can never
    show PAID for a toll that wasn't.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        title = QLabel("CROSSING TICKET")
        title.setStyleSheet(
            f"color: {theme.TEXT_FAINT}; letter-spacing: 3px; "
            f"font-size: 12px;"
        )
        layout.addWidget(title)
        self.headline = QLabel("The toll has not been paid yet.")
        self.headline.setStyleSheet("font-size: 16px; font-weight: 600;")
        layout.addWidget(self.headline)
        self.detail = QLabel(
            "Before anything is installed, Charon creates a restore "
            "point — a snapshot of this PC — and checks that it "
            "really exists. If a new driver misbehaves, that snapshot "
            "is the way back."
        )
        self.detail.setWordWrap(True)
        self.detail.setStyleSheet(f"color: {theme.TEXT_FAINT};")
        layout.addWidget(self.detail)

        self._stamp = _StampOverlay(self)
        self._stamp.setGeometry(self.rect())
        self._stamp.raise_()

    # The stamp's progress stays readable/settable on the ticket —
    # the window (and the audit) drives it through here.
    @property
    def stamp_progress(self) -> float:
        return self._stamp.progress

    @stamp_progress.setter
    def stamp_progress(self, value: float) -> None:
        self._stamp.set_progress(value)

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        super().resizeEvent(event)
        self._stamp.setGeometry(self.rect())
        self._stamp.raise_()

    def show_paid(self, point_number: int, when_text: str) -> None:
        self.headline.setText(f"Restore point #{point_number} — verified.")
        self.detail.setText(
            f"Created {when_text}. Windows confirmed this restore "
            f"point exists, so the way back is real. The crossing "
            f"may begin."
        )
        self._stamp.start()

    def reset(self) -> None:
        self._stamp.reset()
        self.headline.setText("The toll has not been paid yet.")
        self.detail.setText(
            "Before anything is installed, Charon creates a restore "
            "point — a snapshot of this PC — and checks that it "
            "really exists. If a new driver misbehaves, that snapshot "
            "is the way back."
        )
        self.update()
