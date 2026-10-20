"""CHARON.EXE — the Sentinel palette and stylesheet (Phase 5).

Green and black, full stop — the Sentinel brand, shared with the
rest of the medic kit. No light backgrounds anywhere; every text
color is chosen against the surfaces it actually sits on, and the
test suite pins each pairing at WCAG 4.5:1 or better. Red exists
in exactly one place: the small "needs attention" chip, the brand's
rare accent, used only when a driver genuinely needs the owner.

This module is data and stylesheet only — no behavior, no
capability. The widgets live in ``hud.py``; the window in
``gui.py``.
"""

from __future__ import annotations

# ----------------------------------------------------------------------
# Palette (every value verified ≥ 4.5:1 against its surfaces)
# ----------------------------------------------------------------------

BG = "#060a07"          # the river at night — window background
PANEL = "#0c130d"       # dock planks — card/panel background
PANEL_RAISED = "#111a12"  # raised card / hover surface
HAIRLINE = "#1e3323"    # borders, tracks, the far shore
TEXT = "#e8f2ea"        # primary text (on BG/PANEL: >12:1)
TEXT_FAINT = "#7d927f"  # secondary text (on BG: ~5.7:1)
GREEN = "#2ee06f"       # the lantern — primary accent, graphics
GREEN_DEEP = "#14532d"  # button fill (TEXT on it: ~7.8:1)
GREEN_SOFT = "#8fd6a4"  # bright detail text on dark (on BG: ~9:1)
GOLD = "#d8a723"        # "with warnings" chip text (on BG: ~8.6:1)
DANGER = "#e5534b"      # the rare red accent (on BG: ~5.2:1)
STAMP = "#2ee06f"       # the toll stamp

#: Text/surface pairings the suite holds at ≥ 4.5:1, named so the
#: test reads like the design review it is.
TEXT_PAIRINGS = {
    "body text on window": (TEXT, BG),
    "body text on panel": (TEXT, PANEL),
    "faint text on window": (TEXT_FAINT, BG),
    "faint text on panel": (TEXT_FAINT, PANEL),
    "button text on button green": (TEXT, GREEN_DEEP),
    "soft green detail on window": (GREEN_SOFT, BG),
    "accent text on window": (GREEN, BG),
    "warning chip text on window": (GOLD, BG),
    "danger chip text on window": (DANGER, BG),
    "danger chip text on panel": (DANGER, PANEL),
}


def stylesheet() -> str:
    """The application-wide QSS. One stylesheet, every screen —
    consistency is part of the premium feel."""
    return f"""
QWidget {{
    background: {BG};
    color: {TEXT};
    font-family: "Segoe UI", "Inter", sans-serif;
    font-size: 14px;
}}
QLabel#wordmark {{
    font-size: 34px;
    font-weight: 700;
    letter-spacing: 6px;
    color: {TEXT};
}}
QLabel#tagline {{
    color: {TEXT_FAINT};
    font-size: 14px;
}}
QLabel#screenTitle {{
    font-size: 20px;
    font-weight: 600;
    color: {TEXT};
}}
QLabel#screenHint {{
    color: {TEXT_FAINT};
    font-size: 13px;
}}
QLabel#statusLine {{
    color: {TEXT_FAINT};
    font-size: 12px;
}}
QFrame#panel {{
    background: {PANEL};
    border: 1px solid {HAIRLINE};
    border-radius: 10px;
}}
QPushButton {{
    background: {PANEL_RAISED};
    color: {TEXT};
    border: 1px solid {HAIRLINE};
    border-radius: 8px;
    padding: 9px 18px;
    font-size: 14px;
}}
QPushButton:hover {{
    border-color: {GREEN};
}}
QPushButton:disabled {{
    color: {TEXT_FAINT};
    border-color: {HAIRLINE};
}}
QPushButton#primary {{
    background: {GREEN_DEEP};
    border: 1px solid {GREEN_DEEP};
    font-weight: 600;
}}
QPushButton#primary:hover {{
    border-color: {GREEN};
}}
QPushButton#primary:disabled {{
    background: {PANEL};
    color: {TEXT_FAINT};
    border-color: {HAIRLINE};
}}
QCheckBox {{
    color: {TEXT};
    spacing: 8px;
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {HAIRLINE};
    border-radius: 4px;
    background: {PANEL};
}}
QCheckBox::indicator:checked {{
    background: {GREEN_DEEP};
    border-color: {GREEN};
}}
QProgressBar {{
    background: {PANEL};
    border: 1px solid {HAIRLINE};
    border-radius: 5px;
    height: 10px;
    text-align: center;
    color: {TEXT};
    font-size: 10px;
}}
QProgressBar::chunk {{
    background: {GREEN};
    border-radius: 4px;
}}
QScrollArea {{
    border: none;
    background: {BG};
}}
QScrollBar:vertical {{
    background: {PANEL};
    width: 10px;
    border-radius: 5px;
}}
QScrollBar::handle:vertical {{
    background: {HAIRLINE};
    border-radius: 5px;
    min-height: 30px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
"""
