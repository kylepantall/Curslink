"""Colours, sizes and the Qt style sheet, per platform and light or dark.

The values come from docs/design/desktop-ui-spec.md; change both together.
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

_COMMON_STATUS = {
    "win": {
        "light": dict(good="#0F7B0F", good_bg="#DFF6DD", warn="#9D5D00", warn_bg="#FFF4CE", bad="#C42B1C", bad_bg="#FDE7E9"),
        "dark": dict(good="#6CCB5F", good_bg="#253A22", warn="#FCE100", warn_bg="#433519", bad="#FF99A4", bad_bg="#442726"),
    },
    "mac": {
        "light": dict(good="#248A3D", good_bg="rgba(52,199,89,.16)", warn="#A05A00", warn_bg="rgba(255,159,10,.18)",
                      bad="#D70015", bad_bg="rgba(255,59,48,.14)"),
        "dark": dict(good="#30D158", good_bg="rgba(48,209,88,.16)", warn="#FFB340", warn_bg="rgba(255,159,10,.18)",
                     bad="#FF6961", bad_bg="rgba(255,69,58,.18)"),
    },
    "linux": {
        "light": dict(good="#1B7F3B", good_bg="rgba(46,194,126,.16)", warn="#8F5A00", warn_bg="rgba(229,165,10,.20)",
                      bad="#C01C28", bad_bg="rgba(224,27,36,.12)"),
        "dark": dict(good="#8FF0A4", good_bg="rgba(38,162,105,.25)", warn="#F8E45C", warn_bg="rgba(205,147,9,.25)",
                     bad="#FF7B63", bad_bg="rgba(192,28,40,.30)"),
    },
}

TOKENS: dict[str, dict[str, dict[str, str]]] = {
    "win": {
        "light": dict(win="#F3F3F3", glass="rgba(243,243,243,0)", side="transparent", content="#F9F9F9",
                      content_glass="rgba(255,255,255,.5)", card="#FFFFFF", card2="#F9F9F9", stroke="#E5E5E5",
                      ink="#1B1B1B", ink2="#5F5F5F", ink3="#8A8A8A", accent="#005FB8", accent_ink="#FFFFFF",
                      accent_soft="#E3EEF9", ctl="#FFFFFF", ctl_stroke="#D1D1D1", ctl_bottom="#8A8A8A",
                      navsel="rgba(0,0,0,.055)", plot="#F5F7FA"),
        "dark": dict(win="#202020", glass="rgba(32,32,32,0)", side="transparent", content="#272727",
                     content_glass="rgba(255,255,255,.03)", card="#2B2B2B", card2="#323232", stroke="#1D1D1D",
                     ink="#FFFFFF", ink2="#C5C5C5", ink3="#9A9A9A", accent="#60CDFF", accent_ink="#000000",
                     accent_soft="#1F3A4A", ctl="#2D2D2D", ctl_stroke="#3A3A3A", ctl_bottom="#9A9A9A",
                     navsel="rgba(255,255,255,.06)", plot="#1C1C1C"),
    },
    "mac": {
        "light": dict(win="#F6F6F8", glass="transparent", side="#ECECEF", side_glass="rgba(255,255,255,.12)",
                      content="#F6F6F8", content_glass="rgba(246,246,248,.92)", card_glass="rgba(255,255,255,.78)", card="#FFFFFF", card2="#F0F0F2",
                      stroke="rgba(0,0,0,.10)", ink="#1D1D1F", ink2="#6E6E73", ink3="#8E8E93", accent="#007AFF",
                      accent_ink="#FFFFFF", accent_soft="rgba(0,122,255,.12)", ctl="#FFFFFF",
                      ctl_stroke="rgba(0,0,0,.16)", ctl_bottom="rgba(0,0,0,.16)", navsel="rgba(0,0,0,.08)",
                      plot="#EFEFF2"),
        "dark": dict(win="#1E1E20", glass="transparent", side="#28282B", side_glass="rgba(255,255,255,.03)",
                     content="#1E1E20", content_glass="rgba(30,30,32,.90)", card_glass="rgba(255,255,255,.06)", card="#2A2A2D", card2="#252528",
                     stroke="rgba(255,255,255,.10)", ink="#F5F5F7", ink2="#A1A1A6", ink3="#8E8E93", accent="#0A84FF",
                     accent_ink="#FFFFFF", accent_soft="rgba(10,132,255,.22)", ctl="#3A3A3D",
                     ctl_stroke="rgba(255,255,255,.14)", ctl_bottom="rgba(255,255,255,.14)",
                     navsel="rgba(255,255,255,.10)", plot="#151517"),
    },
    "linux": {
        "light": dict(win="#FAFAFB", side="#EBEBED", content="#FAFAFB", card="#FFFFFF", card2="#F6F6F7",
                      stroke="rgba(0,0,6,.10)", ink="#1E1E22", ink2="#5C5C62", ink3="#7C7C83", accent="#3584E4",
                      accent_ink="#FFFFFF", accent_soft="rgba(53,132,228,.14)", ctl="#EDEDEF", ctl_stroke="#EDEDEF",
                      ctl_bottom="#EDEDEF", navsel="rgba(0,0,6,.10)", plot="#F2F2F4"),
        "dark": dict(win="#222226", side="#2E2E32", content="#222226", card="#36363A", card2="#2E2E32",
                     stroke="rgba(0,0,0,.36)", ink="#FFFFFF", ink2="#B3B3B5", ink3="#8F8F93", accent="#78AEED",
                     accent_ink="#0B1A2B", accent_soft="rgba(120,174,237,.18)", ctl="#3D3D41", ctl_stroke="#3D3D41",
                     ctl_bottom="#3D3D41", navsel="rgba(255,255,255,.10)", plot="#1C1C1F"),
    },
}

DOT_GOOD, DOT_WARN = "#2EC27E", "#E5A50A"
LINK_BLUE = "#3FA9FF"
OTHER_DEVICE = "#8E6BD8"

SIZES = {
    "win": dict(r_card=8, r_ctl=4, r_nav=4, btn_h=32, field_h=32, nav_h=36, h1=28, side_w=232, font_px=14,
                small_px=12.5, h2_px=14, mac_top=0),
    "mac": dict(r_card=12, r_ctl=7, r_nav=8, btn_h=28, field_h=26, nav_h=30, h1=26, side_w=240, font_px=13,
                small_px=12, h2_px=13, mac_top=44),
    "linux": dict(r_card=12, r_ctl=8, r_nav=8, btn_h=34, field_h=34, nav_h=38, h1=24, side_w=232, font_px=14,
                  small_px=12.5, h2_px=14, mac_top=0),
}

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
    native: bool = False  # Qt's own Windows 11 / macOS style draws the standard controls

    @property
    def mode(self) -> str:
        return "dark" if self.dark else "light"

    @property
    def tokens(self) -> dict[str, str]:
        t = dict(TOKENS[self.platform][self.mode])
        t.update(_COMMON_STATUS[self.platform][self.mode])
        return t

    @property
    def size(self) -> dict:
        return SIZES[self.platform]

    def css(self, name: str) -> str:
        return self.tokens[name]

    def color(self, name: str) -> QColor:
        return qcolor(self.tokens[name])

    def solid(self, name: str, under: str = "card") -> QColor:
        """A token as an opaque colour over ``under`` (for painting)."""
        return over(self.tokens[name], self.tokens[under] if under in self.tokens else under)

    # -- the window's layers, which depend on whether there's glass behind
    @property
    def window_bg(self) -> str:
        return self.tokens.get("glass", self.tokens["win"]) if self.glass else self.tokens["win"]

    @property
    def side_bg(self) -> str:
        t = self.tokens
        if self.platform == "win":
            return "transparent" if self.glass else t["win"]
        if self.platform == "mac":
            return t["side_glass"] if self.glass else t["side"]
        return t["side"]

    @property
    def card_bg(self) -> str:
        t = self.tokens
        return t.get("card_glass", t["card"]) if self.glass else t["card"]

    @property
    def content_bg(self) -> str:
        t = self.tokens
        return t.get("content_glass", t["content"]) if self.glass else t["content"]


_current = Theme()


def current() -> Theme:
    return _current


# ------------------------------------------------------------------ style

def stylesheet(th: Theme) -> str:
    t, s = th.tokens, th.size
    plat = th.platform
    bold = 700 if plat == "linux" else 600
    btn_border = "0" if plat == "linux" else f"1px solid {t['ctl_stroke']}"
    field_border = "0" if plat == "linux" else f"1px solid {t['ctl_stroke']}"
    field_bottom = f"border-bottom: 1px solid {t['ctl_bottom']};" if plat == "win" else ""
    field_focus = (f"border-bottom: 2px solid {t['accent']};" if plat == "win"
                   else f"border: {'2px' if plat == 'linux' else '1px'} solid {t['accent']};")
    content_edge = (f"border-top-left-radius: 8px; border-top: 1px solid {t['stroke']}; border-left: 1px solid {t['stroke']};"
                    if plat == "win" else "")
    side_edge = f"border-right: 1px solid {t['stroke']};" if plat == "linux" else ""
    if plat == "mac":
        # The sidebar is the window's vibrancy, flush to the edges, with a
        # hairline where the body starts.
        side_round = f"border-right: 1px solid {t['stroke']};" if th.glass else ""
    else:
        side_round = ""
    # Segmented controls: a recessed track with a raised thumb (macOS look,
    # used everywhere; Windows gets a hairline track, as Fluent does).
    if plat == "mac":
        seg_track = "rgba(120,120,128,.24)" if th.dark else "rgba(120,120,128,.14)"
        seg_track_border = "0"
        seg_thumb = "rgba(255,255,255,.22)" if th.dark else "#FFFFFF"
        seg_thumb_ink = ""
    else:
        seg_track = t["card2"]
        seg_track_border = f"1px solid {t['stroke']}" if plat == "win" else "0"
        seg_thumb = t["ctl"]
        seg_thumb_ink = f"color: {t['accent']};" if plat == "win" else ""
    # Qt's native styles (windows11, macos) draw buttons, fields, menus,
    # scroll bars and sliders themselves. Any style-sheet rule with a box
    # property on those widgets makes Qt fall back to drawing them from the
    # sheet, so in native mode they get no rules at all.
    if th.native:
        generic_buttons = fields = chrome = ""
    else:
        generic_buttons = f"""QPushButton {{
    min-height: {s['btn_h'] - 2}px; padding: 0 14px; border-radius: {s['r_ctl']}px; border: {btn_border};
    background: {t['ctl']}; color: {t['ink']}; font-weight: {700 if plat == 'linux' else 400};
}}
QPushButton:hover {{ border-color: {t['ink3']}; }}
QPushButton:pressed {{ background: {t['card2']}; }}
QPushButton:disabled {{ color: {t['ink3']}; }}
QPushButton:focus {{ outline: none; }}
"""
        fields = f"""QLineEdit, QComboBox, QSpinBox {{
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
"""
        chrome = f"""QToolTip {{ background: {t['card']}; color: {t['ink']}; border: 1px solid {t['stroke']}; padding: 4px 8px; }}
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

{generic_buttons}QPushButton#primary {{ min-height: {s['btn_h'] - 2}px; padding: 0 14px; border-radius: {s['r_ctl']}px;
    background: {t['accent']}; color: {t['accent_ink']}; border: 0; font-weight: 600; }}
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

{fields}QPlainTextEdit {{ background: {t['card2']}; color: {t['ink2']}; border: 1px solid {t['stroke']};
    border-radius: {s['r_ctl']}px; font-family: {MONO}; font-size: 12px; padding: 6px; }}
{chrome}"""


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


# ------------------------------------------------------------- Qt style

NATIVE_STYLES = {"win": "windows11", "mac": "macos"}


def install_style(app) -> str:
    """Use Qt's own Windows 11 or macOS style where there is one, else Fusion.

    ``CURSLINK_STYLE`` picks a style by name (``fusion`` goes back to the
    fully custom look). Linux has no native Qt style for GNOME, so it keeps
    Fusion and the style sheet. Returns the style's name, lower case.
    """
    from PySide6.QtWidgets import QStyleFactory

    keys = {k.lower(): k for k in QStyleFactory.keys()}
    wanted = (os.environ.get("CURSLINK_STYLE") or "").lower()
    if wanted not in keys:
        wanted = NATIVE_STYLES.get(platform_key(), "fusion")
        if wanted not in keys:  # e.g. Windows 10, or a build without the plugin
            wanted = "fusion"
    global _style
    if hasattr(app, "setStyle"):
        app.setStyle(keys.get(wanted, "Fusion"))
        _style = wanted
    log.info("Qt style: %s", wanted)
    return wanted


_style = ""  # what install_style() set; app.style() is a style-sheet wrapper once a sheet is set


def native_style_active() -> bool:
    return _style in NATIVE_STYLES.values()


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
        native = native_style_active()
        _current = Theme(platform_key(), dark, self.glass, native)
        app = QGuiApplication.instance()
        if app is not None:
            # A native style takes its colours (and the user's accent) from
            # the system, following the colour scheme set above.
            app.setPalette(QPalette() if native else palette(_current))
            if hasattr(app, "setStyleSheet"):
                app.setStyleSheet(stylesheet(_current))
            if _current.platform == "win" and sys.platform == "win32":
                f = app.font()
                f.setFamilies(["Segoe UI Variable Text", "Segoe UI"])
                app.setFont(f)
        log.info("theme: %s %s%s%s (setting: %s)", _current.platform, _current.mode,
                 " with glass" if self.glass else "", ", native controls" if native else "", self.mode)
        self.changed.emit()


_manager: ThemeManager | None = None


def manager(mode: str | None = None) -> ThemeManager:
    global _manager
    if _manager is None:
        _manager = ThemeManager(mode or "system")
    elif mode is not None:
        _manager.mode = mode
    return _manager
