"""Colours, sizes and the Qt style sheet: one Curslink look, light or dark.

Curslink looks the same on Windows, macOS and Linux: its own colours (the
link blue of its icon on a deep navy, or on a cool white), its own shapes
and its own controls. Nothing here follows the platform's style; the only
thing a platform changes is what it has to (:attr:`Theme.platform`: room
for the Mac's window buttons, which font is to hand).
``current()`` is the theme in use; ``manager().changed`` fires when it
changes (the user picks Light or Dark, or the OS switches while on System).
"""

from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPalette

log = logging.getLogger(__name__)

MODES = ("system", "light", "dark")


def platform_key() -> str:
    forced = os.environ.get("CURSLINK_LOOK")  # win / mac / linux, for screenshots and tests
    if forced in ("win", "mac", "linux"):
        return forced
    if sys.platform == "win32":
        return "win"
    if sys.platform == "darwin":
        return "mac"
    return "linux"


# ----------------------------------------------------------------- tokens

LOOK = "own"

_COMMON_STATUS = {
    LOOK: {
        "light": dict(good="#1B7F3B", good_bg="rgba(46,194,126,.16)", warn="#8A5600", warn_bg="rgba(229,165,10,.20)",
                      bad="#C01C28", bad_bg="rgba(224,27,36,.11)"),
        "dark": dict(good="#6FE3A0", good_bg="rgba(46,194,126,.20)", warn="#F5CF5B", warn_bg="rgba(229,165,10,.22)",
                     bad="#FF8A80", bad_bg="rgba(255,90,80,.22)"),
    },
}

# Neutrals lean to the icon's navy; the accent is its link blue (a deeper
# one on white, so white text on it reads).
TOKENS: dict[str, dict[str, dict[str, str]]] = {
    LOOK: {
        "light": dict(win="#F4F6FA", side="#E8EDF5", content="#F4F6FA", card="#FFFFFF", card2="#EEF2F8",
                      stroke="rgba(18,35,60,.12)", ink="#12233C", ink2="#4F5F7A", ink3="#7A879C", accent="#0F6FD1",
                      accent_ink="#FFFFFF", accent_soft="rgba(63,169,255,.17)", ctl="#FFFFFF",
                      ctl_stroke="rgba(18,35,60,.20)", ctl_bottom="rgba(18,35,60,.20)", navsel="rgba(18,35,60,.08)",
                      plot="#E9EFF7"),
        "dark": dict(win="#0F1B2D", side="#0B1524", content="#0F1B2D", card="#162843", card2="#12233C",
                     stroke="rgba(182,223,255,.13)", ink="#EAF2FB", ink2="#9FB3CC", ink3="#70839D", accent="#3FA9FF",
                     accent_ink="#06182B", accent_soft="rgba(63,169,255,.20)", ctl="#1D3352",
                     ctl_stroke="rgba(182,223,255,.18)", ctl_bottom="rgba(182,223,255,.18)",
                     navsel="rgba(182,223,255,.10)", plot="#0A1422"),
    },
}

DOT_GOOD, DOT_WARN = "#2EC27E", "#E5A50A"
LINK_BLUE = "#3FA9FF"
OTHER_DEVICE = "#8E6BD8"

SIZES = {
    LOOK: dict(r_card=12, r_ctl=8, r_nav=8, btn_h=34, field_h=34, nav_h=38, h1=26, side_w=236, font_px=14,
               small_px=12.5, h2_px=14, mac_top=0),
}
MAC_TOP = 44  # macOS: the window's buttons sit over the sidebar's top

# Only fonts each platform has, so Qt doesn't spend time looking up missing ones.
MONO = {"win32": '"Cascadia Mono", Consolas, monospace', "darwin": 'Menlo, monospace'}.get(
    sys.platform, '"Source Code Pro", "DejaVu Sans Mono", monospace')


def qcolor(value: str) -> QColor:
    """``#RRGGBB`` or ``rgba(r,g,b,a)`` (a from 0 to 1) as a QColor."""
    m = re.fullmatch(r"rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([\d.]+)\s*\)", value.strip())
    if m:
        r, g, b, a = m.groups()
        return QColor(int(r), int(g), int(b), round(float(a) * 255))
    if value == "transparent":
        return QColor(0, 0, 0, 0)
    return QColor(value)


def over(top: str, under: str) -> QColor:
    """``top`` composited over the opaque ``under``."""
    t, u = qcolor(top), qcolor(under)
    a = t.alphaF()
    return QColor.fromRgbF(t.redF() * a + u.redF() * (1 - a), t.greenF() * a + u.greenF() * (1 - a),
                           t.blueF() * a + u.blueF() * (1 - a), 1.0)


@dataclass
class Theme:
    platform: str = field(default_factory=platform_key)
    dark: bool = False
    glass: bool = False  # the window has a native backdrop (Mica / vibrancy) behind it

    @property
    def mode(self) -> str:
        return "dark" if self.dark else "light"

    @property
    def look(self) -> str:
        """The one look there is (what drawing code asks, where it once asked the platform)."""
        return LOOK

    @property
    def tokens(self) -> dict[str, str]:
        t = dict(TOKENS[LOOK][self.mode])
        t.update(_COMMON_STATUS[LOOK][self.mode])
        return t

    @property
    def size(self) -> dict:
        return {**SIZES[LOOK], "mac_top": MAC_TOP if self.platform == "mac" else 0}

    def css(self, name: str) -> str:
        return self.tokens[name]

    def color(self, name: str) -> QColor:
        return qcolor(self.tokens[name])

    def solid(self, name: str, under: str = "card") -> QColor:
        """A token as an opaque colour over ``under`` (for painting)."""
        return over(self.tokens[name], self.tokens[under] if under in self.tokens else under)

    # -- the window's layers (solid: Curslink draws its own, no Mica or vibrancy behind)
    @property
    def window_bg(self) -> str:
        return self.tokens["win"]

    @property
    def side_bg(self) -> str:
        return self.tokens["side"]

    @property
    def card_bg(self) -> str:
        return self.tokens["card"]

    @property
    def content_bg(self) -> str:
        return self.tokens["content"]


_current = Theme()


def current() -> Theme:
    return _current


# ------------------------------------------------------------------ style

def stylesheet(th: Theme) -> str:
    t, s = th.tokens, th.size
    plat = th.look
    bold = 600
    btn_border = f"1px solid {t['ctl_stroke']}"
    field_border = f"1px solid {t['ctl_stroke']}"
    field_bottom = ""
    field_focus = f"border: 2px solid {t['accent']};"
    content_edge = ""
    side_edge = f"border-right: 1px solid {t['stroke']};"
    side_round = ""
    # Segmented controls: a recessed track with a raised thumb.
    seg_track = t["card2"]
    seg_track_border = f"1px solid {t['stroke']}"
    seg_thumb = t["ctl"]
    seg_thumb_ink = ""
    return f"""
* {{ font-size: {s['font_px']}px; }}
QMainWindow, QWidget#root {{ background: {th.window_bg}; color: {t['ink']}; }}
QWidget#sidebar {{ background: {th.side_bg}; {side_edge} {side_round} }}
QWidget#contentPane {{ background: {th.content_bg}; {content_edge} }}
QWidget#page, QScrollArea, QScrollArea > QWidget > QWidget#page {{ background: transparent; }}
QScrollArea > QWidget {{ background: transparent; }}
QLabel {{ color: {t['ink']}; background: transparent; }}
QLabel#brand {{ font-size: 15px; font-weight: 700; }}
QLabel#brandSub, QLabel#sideNoteText {{ color: {t['ink2']}; font-size: 12px; }}
QLabel#sideNoteTitle {{ font-weight: 600; font-size: 13px; }}
QLabel#welcomeName {{ font-size: 20px; font-weight: 700; }}
QLabel#welcomeVersion {{ color: {t['ink2']}; font-size: 13px; }}
QLabel#welcomeNote {{ color: {t['ink3']}; font-size: 12px; }}

QLabel#h1 {{ font-size: {s['h1']}px; font-weight: {700 if plat != 'win' else 600}; }}
QLabel#h2 {{ font-size: {s['h2_px']}px; font-weight: {bold}; {'color: ' + t['ink2'] + ';' if plat == 'mac' else ''} }}
QLabel#group {{ font-size: {s['h2_px']}px; font-weight: {bold}; {'color: ' + t['ink2'] + ';' if plat == 'mac' else ''} padding: 4px 2px 0 2px; }}
QLabel#big {{ font-size: 26px; font-weight: 600; }}
QLabel#prompt {{ font-size: 20px; font-weight: 600; }}
QLabel#emptyTitle {{ font-size: 17px; font-weight: 600; }}
QLabel#eyebrow {{ color: {t['ink2']}; font-size: 11px; font-weight: 600; letter-spacing: 1px; }}
QLabel#muted {{ color: {t['ink2']}; }}
QLabel#small {{ color: {t['ink2']}; font-size: {s['small_px']}px; }}
QLabel#faint {{ color: {t['ink3']}; font-size: {s['small_px']}px; }}
QLabel#mono {{ font-family: {MONO}; font-size: 12px; color: {t['ink2']}; }}
QLabel#code {{ font-family: {MONO}; font-size: 38px; letter-spacing: 6px; color: {t['ink']}; }}
QLabel#warn {{ background: {t['warn_bg']}; color: {t['ink']}; border-radius: {s['r_card']}px; padding: 10px 12px; }}
QLabel#error {{ background: {t['bad_bg']}; color: {t['ink']}; border-radius: {s['r_card']}px; padding: 10px 12px; }}
QLabel#info {{ background: {t['accent_soft']}; color: {t['ink']}; border-radius: {s['r_card']}px; padding: 10px 12px; }}

QFrame#card {{ background: {th.card_bg}; border: 1px solid {t['stroke']}; border-radius: {s['r_card']}px; }}
QFrame#accentCard {{ background: {t['accent_soft']}; border: 0; border-radius: {s['r_card']}px; }}
QFrame#row {{ background: transparent; border: 0; border-top: 1px solid {t['stroke']}; }}
QFrame#rowFirst {{ background: transparent; border: 0; }}

QPushButton {{
    min-height: {s['btn_h'] - 2}px; padding: 0 14px; border-radius: {s['r_ctl']}px; border: {btn_border};
    background: {t['ctl']}; color: {t['ink']}; font-weight: {700 if plat == 'linux' else 400};
}}
QPushButton:hover {{ border-color: {t['ink3']}; }}
QPushButton:pressed {{ background: {t['card2']}; }}
QPushButton:disabled {{ color: {t['ink3']}; }}
QPushButton:focus {{ outline: none; }}
QPushButton#primary {{ background: {t['accent']}; color: {t['accent_ink']}; border: 0; font-weight: 600; }}
QPushButton#primary:disabled {{ background: {t['ctl']}; color: {t['ink3']}; }}
QPushButton#danger {{ color: {t['bad']}; {'background: ' + t['bad_bg'] + ';' if plat == 'linux' else ''} }}
QPushButton#choice {{ text-align: left; padding: 14px 16px; border: {'2px solid transparent' if plat == 'linux' else '1px solid ' + t['ctl_stroke']}; border-radius: {s['r_card']}px; }}
QPushButton#choice:checked {{ border: 2px solid {t['accent']}; background: {t['accent_soft']}; }}
QFrame#segTrack {{ background: {seg_track}; border: {seg_track_border}; border-radius: {s['r_ctl'] + 1}px; }}
QPushButton#seg {{ min-height: {s['btn_h'] - 8}px; padding: 0 14px; border: 1px solid transparent; background: transparent;
    border-radius: {s['r_ctl'] - 1}px; }}
QPushButton#seg:hover {{ border-color: transparent; color: {t['ink']}; }}
QPushButton#seg:checked {{ background: {seg_thumb}; border: 1px solid {t['ctl_stroke']}; font-weight: 600; {seg_thumb_ink} }}
QPushButton#link {{ border: 0; background: transparent; color: {t['accent']}; padding: 0; min-height: 0; }}

QLineEdit, QComboBox, QSpinBox {{
    min-height: {s['field_h'] - 2}px; padding: 0 10px; border: {field_border}; {field_bottom}
    border-radius: {s['r_ctl']}px; background: {t['ctl']}; color: {t['ink']};
    selection-background-color: {t['accent']}; selection-color: {t['accent_ink']};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ {field_focus} }}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{ color: {t['ink3']}; }}
QComboBox::drop-down {{ border: 0; width: 24px; }}
QComboBox QAbstractItemView {{
    background: {t['card']}; color: {t['ink']}; border: 1px solid {t['stroke']}; padding: 4px; outline: 0;
    selection-background-color: {t['accent_soft']}; selection-color: {t['ink']};
}}
QPlainTextEdit {{ background: {t['card2']}; color: {t['ink2']}; border: 1px solid {t['stroke']};
    border-radius: {s['r_ctl']}px; font-family: {MONO}; font-size: 12px; padding: 6px; }}
QToolTip {{ background: {t['card']}; color: {t['ink']}; border: 1px solid {t['stroke']}; padding: 4px 8px; }}
QMenu {{ background: {t['card']}; color: {t['ink']}; border: 1px solid {t['stroke']}; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; border-radius: {s['r_ctl']}px; }}
QMenu::item:selected {{ background: {t['accent_soft']}; }}
QMenu::item:disabled {{ color: {t['ink3']}; }}
QMenu::separator {{ height: 1px; background: {t['stroke']}; margin: 4px 6px; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {t['ink3']}; border-radius: 3px; min-height: 30px; margin: 0 2px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {t['ink3']}; border-radius: 3px; min-width: 30px; margin: 2px 0; }}

QSlider::groove:horizontal {{ height: 4px; background: {t['ctl_stroke'] if plat != 'linux' else t['ctl']}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {t['accent']}; border-radius: 2px; }}
QSlider::handle:horizontal {{ width: 18px; height: 18px; margin: -7px 0; border-radius: 9px;
    background: {'#FFFFFF' if plat == 'mac' else t['accent']}; border: {('1px solid ' + t['ctl_stroke']) if plat == 'mac' else '0'}; }}
"""


def pill_style(kind: str) -> str:
    t = current().tokens
    colours = {
        "on": (t["good_bg"], t["good"]),
        "off": (t["card2"], t["ink2"]),
        "here": (t["accent_soft"], t["accent"] if not current().dark else t["ink"]),
        "wait": (t["warn_bg"], t["warn"]),
        "bad": (t["bad_bg"], t["bad"]),
    }
    bg, fg = colours.get(kind, colours["off"])
    # A border (even one the colour of the fill) makes Qt clip the fill to the radius.
    border = f"border: 1px solid {t['stroke'] if kind == 'off' else bg};"
    return (f"background: {bg}; color: {fg}; {border} border-radius: 10px; padding: 2px 10px; "
            "font-size: 12px; font-weight: 600;")


def palette(th: Theme) -> QPalette:
    t = th.tokens
    p = QPalette()
    window = over(th.content_bg, t["win"]) if th.content_bg != "transparent" else qcolor(t["win"])
    card = over(t["card"], window.name())
    roles = {
        QPalette.ColorRole.Window: window,
        QPalette.ColorRole.WindowText: qcolor(t["ink"]),
        QPalette.ColorRole.Base: over(t["ctl"], card.name()),
        QPalette.ColorRole.AlternateBase: over(t["card2"], card.name()),
        QPalette.ColorRole.Text: qcolor(t["ink"]),
        QPalette.ColorRole.Button: over(t["ctl"], card.name()),
        QPalette.ColorRole.ButtonText: qcolor(t["ink"]),
        QPalette.ColorRole.Highlight: qcolor(t["accent"]),
        QPalette.ColorRole.HighlightedText: qcolor(t["accent_ink"]),
        QPalette.ColorRole.PlaceholderText: over(t["ink3"], card.name()),
        QPalette.ColorRole.ToolTipBase: card,
        QPalette.ColorRole.ToolTipText: qcolor(t["ink"]),
        QPalette.ColorRole.Link: qcolor(t["accent"]),
        QPalette.ColorRole.BrightText: qcolor(t["accent_ink"]),
        QPalette.ColorRole.Mid: over(t["stroke"], card.name()),
    }
    for role, colour in roles.items():
        p.setColor(role, colour)
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText, QPalette.ColorRole.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, over(t["ink3"], card.name()))
    return p


# ---------------------------------------------------------------- manager

class ThemeManager(QObject):
    changed = Signal()

    def __init__(self, mode: str = "system"):
        super().__init__(QGuiApplication.instance())
        self.mode = mode if mode in MODES else "system"
        self.glass = False
        hints = QGuiApplication.styleHints()
        if hasattr(hints, "colorSchemeChanged"):
            hints.colorSchemeChanged.connect(lambda _s: self._system_changed())

    def system_dark(self) -> bool:
        hints = QGuiApplication.styleHints()
        scheme = hints.colorScheme() if hasattr(hints, "colorScheme") else None
        if scheme == Qt.ColorScheme.Dark:
            return True
        if scheme == Qt.ColorScheme.Light:
            return False
        if os.environ.get("CURSLINK_DARK") == "1":
            return True
        # Unknown (some Linux desktops): judge by the platform's own palette.
        return QGuiApplication.palette().color(QPalette.ColorRole.Window).lightness() < 128

    def _system_changed(self) -> None:
        if self.mode == "system":
            log.info("the system switched to %s", "dark" if self.system_dark() else "light")
            self.apply()

    def set_mode(self, mode: str) -> None:
        self.mode = mode if mode in MODES else "system"
        self.apply()

    def set_glass(self, on: bool) -> None:
        if on != self.glass:
            self.glass = on
            self.apply()

    def apply(self) -> None:
        global _current
        hints = QGuiApplication.styleHints()
        if hasattr(hints, "setColorScheme"):
            # Lets native parts (title bars, dialogs) follow a forced choice.
            if self.mode == "system":
                hints.unsetColorScheme()
            else:
                hints.setColorScheme(Qt.ColorScheme.Dark if self.mode == "dark" else Qt.ColorScheme.Light)
        dark = self.system_dark() if self.mode == "system" else self.mode == "dark"
        _current = Theme(platform_key(), dark, self.glass)
        app = QGuiApplication.instance()
        if app is not None:
            app.setPalette(palette(_current))
            if hasattr(app, "setStyleSheet"):
                app.setStyleSheet(stylesheet(_current))
            if sys.platform == "win32":
                f = app.font()
                f.setFamilies(["Segoe UI Variable Text", "Segoe UI"])
                app.setFont(f)
        log.info("theme: %s (setting: %s)", _current.mode, self.mode)
        self.changed.emit()


_manager: ThemeManager | None = None


def manager(mode: str | None = None) -> ThemeManager:
    global _manager
    if _manager is None:
        _manager = ThemeManager(mode or "system")
    elif mode is not None:
        _manager.mode = mode
    return _manager
