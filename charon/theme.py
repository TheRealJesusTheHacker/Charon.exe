"""CHARON.EXE — Charon's own palette and stylesheet (Phase 5,
restyled v1.0.3).

Every tool in the medic kit wears the colors of what it was named
for. Charon is the ferryman of the Styx, so Charon wears the river:
a charcoal-slate night, the river's aqua-teal for everything that
moves or can be pressed, and the muted gold of the obol — the coin
the dead paid for passage — for the moments that carry a warning.
No light backgrounds anywhere; every text color is chosen against
the surfaces it actually sits on, and the test suite pins each
pairing at WCAG 4.5:1 or better.

Red exists in exactly one treatment, shared with the whole kit:
white text on the fleet's danger red, used only when something
genuinely needs the owner — a toll refused, a driver held back.
It is a fill, never a whisper: danger is never written in a color
that has to compete with the dark.

This module is data and stylesheet only — no behavior, no
capability. The widgets live in ``hud.py``; the window in
``gui.py``.
"""

from __future__ import annotations

# ----------------------------------------------------------------------
# Palette (every pairing verified ≥ 4.5:1 against its surfaces)
# ----------------------------------------------------------------------

BG = "#07090b"               # the river at night — window background
PANEL = "#0d1216"            # dock planks — card/panel background
PANEL_BORDER = "#22303a"     # borders, tracks, the far shore
INPUT_BG = "#0a0e11"         # the raised face — buttons, chips
TEXT = "#e8eef0"             # primary text (on BG: ~17:1)
TEXT_FAINT = "#93a7b1"       # secondary text (on BG: ~8:1)
PRIMARY = "#2fbfa8"          # Styx aqua — the lantern, graphics, action
PRIMARY_DARK_TEXT = "#04120f"  # text on a primary fill (on PRIMARY: ~8.3:1)
ACCENT = "#bfa05a"           # obol gold — "with warnings" text (on BG: ~8:1)
DISABLED_TEXT = "#7e939e"    # retired controls (on PANEL: ~5.9:1)
RED = "#b03a2e"              # the fleet danger red — a fill, with white text
RED_TEXT = "#ffffff"         # text on the danger red (on RED: ~6:1)
STAMP = "#2fbfa8"            # the toll stamp — the primary, struck like a seal

#: Text/surface pairings the suite holds at ≥ 4.5:1, named so the
#: test reads like the design review it is.
TEXT_PAIRINGS = {
    "body text on window": (TEXT, BG),
    "body text on panel": (TEXT, PANEL),
    "body text on raised surface": (TEXT, INPUT_BG),
    "faint text on window": (TEXT_FAINT, BG),
    "faint text on panel": (TEXT_FAINT, PANEL),
    "faint text on raised surface": (TEXT_FAINT, INPUT_BG),
    "primary text on window": (PRIMARY, BG),
    "primary text on panel": (PRIMARY, PANEL),
    "button text on primary aqua": (PRIMARY_DARK_TEXT, PRIMARY),
    "accent text on window": (ACCENT, BG),
    "accent text on panel": (ACCENT, PANEL),
    "danger text on danger red": (RED_TEXT, RED),
    "disabled text on window": (DISABLED_TEXT, BG),
    "disabled text on panel": (DISABLED_TEXT, PANEL),
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
    border: 1px solid {PANEL_BORDER};
    border-radius: 10px;
}}
QPushButton {{
    background: {INPUT_BG};
    color: {TEXT};
    border: 1px solid {PANEL_BORDER};
    border-radius: 8px;
    padding: 9px 18px;
    font-size: 14px;
}}
QPushButton:hover {{
    border-color: {PRIMARY};
}}
QPushButton:disabled {{
    color: {DISABLED_TEXT};
    border-color: {PANEL_BORDER};
}}
QPushButton#primary {{
    background: {PRIMARY};
    color: {PRIMARY_DARK_TEXT};
    border: 1px solid {PRIMARY};
    font-weight: 600;
}}
QPushButton#primary:hover {{
    border-color: {ACCENT};
}}
QPushButton#primary:disabled {{
    background: {PANEL};
    color: {DISABLED_TEXT};
    border-color: {PANEL_BORDER};
}}
QCheckBox {{
    color: {TEXT};
    spacing: 8px;
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {PANEL_BORDER};
    border-radius: 4px;
    background: {PANEL};
}}
QCheckBox::indicator:checked {{
    background: {PRIMARY};
    border-color: {PRIMARY};
    color: {PRIMARY_DARK_TEXT};
}}
QProgressBar {{
    background: {PANEL};
    border: 1px solid {PANEL_BORDER};
    border-radius: 5px;
    height: 10px;
    text-align: center;
    color: {TEXT};
    font-size: 10px;
}}
QProgressBar::chunk {{
    background: {PRIMARY};
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
    background: {PANEL_BORDER};
    border-radius: 5px;
    min-height: 30px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
"""
