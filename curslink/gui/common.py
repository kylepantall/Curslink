"""Shared bits of the desktop app: the live status, background work, icons, style."""

from __future__ import annotations

import collections
import threading
import time
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QByteArray, QObject, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

# The mockup's palette (see the "Curslink desktop app" design canvas).
NAVY = "#0F1B2D"
NAVY_2 = "#16263D"
NAVY_3 = "#24395A"
ACCENT = "#1467B8"       # blue for text and buttons (passes contrast on white)
ACCENT_BRIGHT = "#3FA9FF"  # the switch animation's blue
INK = "#18212E"
MUTED = "#556173"
LINE = "#DDE3EA"
SURFACE = "#F3F5F8"
GOOD = "#1A9E66"
WARN = "#D98A0B"


# --------------------------------------------------------------------- status

@dataclass
class Switch:
    at: float
    source: str
    target: str
    screen: str
    reason: str


@dataclass
class Agent:
    device_id: str
    name: str = ""
    platform: str = ""
    connected: bool = False
    screens: list = field(default_factory=list)


class Status(QObject):
    """What the running host or agent last told us."""

    changed = Signal()
    switched = Signal(object)  # Switch

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.reset()

    def reset(self) -> None:
        self.running = False
        self.role = ""
        self.device_id = ""
        self.name = ""
        self.active = ""
        self.screen = ""
        self.gaze = False
        self.gaze_paused = False
        self.agents: dict[str, Agent] = {}
        self.host_state = ""   # agent side: connected / disconnected / waiting
        self.host_detail = ""
        self.switches: collections.deque[Switch] = collections.deque(maxlen=50)
        self.log: collections.deque[tuple[float, str, str]] = collections.deque(maxlen=300)
        self.log_seq = 0

    def stopped(self) -> None:
        keep = self.log, self.switches, self.log_seq
        self.reset()
        self.log, self.switches, self.log_seq = keep
        self.changed.emit()

    def add_log(self, level: str, message: str) -> None:
        self.log.append((time.time(), level, message))
        self.log_seq += 1

    def apply(self, event: dict[str, Any]) -> None:
        kind = event.get("event")
        if kind == "running":
            self.running = True
            self.role = event.get("role", "")
            self.device_id = event.get("device_id", "")
            self.name = event.get("name", "")
            self.active = event.get("active", self.device_id)
            self.screen = event.get("screen", "")
            self.gaze = bool(event.get("gaze", False))
            self.gaze_paused = bool(event.get("gaze_paused", False))
        elif kind == "agent":
            agent = self.agents.setdefault(event["device_id"], Agent(event["device_id"]))
            agent.connected = bool(event.get("connected"))
            for key in ("name", "platform", "screens"):
                if key in event:
                    setattr(agent, key, event[key])
        elif kind == "switch":
            self.active, self.screen = event.get("to", ""), event.get("screen", "")
            switch = Switch(time.time(), event.get("from", ""), event.get("to", ""),
                            event.get("screen", ""), event.get("reason", ""))
            self.switches.appendleft(switch)
            self.switched.emit(switch)
        elif kind == "gaze":
            self.gaze_paused = bool(event.get("paused"))
        elif kind == "host":
            self.host_state = event.get("state", "")
            self.host_detail = event.get("detail", "")
        elif kind == "log":
            self.add_log(event.get("level", "info"), event.get("message", ""))
            return  # the log page polls; no need to redraw everything
        else:
            return
        self.changed.emit()


# --------------------------------------------------------- background work

class _Relay(QObject):
    done = Signal(object)
    failed = Signal(object)


def in_background(fn: Callable[[], Any], on_done: Callable[[Any], None] | None = None,
                  on_error: Callable[[BaseException], None] | None = None) -> threading.Thread:
    """Run ``fn`` on a thread; ``on_done``/``on_error`` run back on the Qt thread."""
    relay = _Relay()
    if on_done:
        relay.done.connect(on_done)
    if on_error:
        relay.failed.connect(on_error)

    def run() -> None:
        try:
            result = fn()
        except BaseException as exc:  # handed to on_error
            relay.failed.emit(exc)
        else:
            relay.done.emit(result)

    thread = threading.Thread(target=run, daemon=True)
    thread._relay = relay  # keep it alive as long as the thread
    thread.start()
    return thread


# ------------------------------------------------------------------- icons

def asset(name: str) -> bytes:
    return resources.files("curslink.assets").joinpath(name).read_bytes()


# The masters follow Apple's icon grid: the tile fills the middle 824 of
# 1024 px. Elsewhere (the window, Windows, Linux) icons fill their square, so
# those draw the tile at TILE_FILL of the size.
TILE_SPAN = 824 / 1024
TILE_FILL = 0.94
SMALL_MAX = 24  # at or below this, curslink-small.png reads better


def svg_image(name: str, size: int, fill: float | None = TILE_FILL) -> QImage:
    """``name`` (an SVG asset) at ``size`` px; ``fill`` scales the tile to
    that share of the square (None = the canvas as drawn)."""
    renderer = QSvgRenderer(QByteArray(asset(name)))
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    if fill is None:
        target = QRectF(0, 0, size, size)
    else:
        span = size * fill / TILE_SPAN
        target = QRectF((size - span) / 2, (size - span) / 2, span, span)
    renderer.render(painter, target)
    painter.end()
    return image


def png_image(name: str, size: int) -> QImage:
    image = QImage.fromData(asset(name))
    return image.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation)


def app_image(size: int) -> QImage:
    return png_image("curslink-small.png", size) if size <= SMALL_MAX else svg_image("curslink.svg", size)


def app_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256, 512):
        icon.addPixmap(QPixmap.fromImage(app_image(size)))
    return icon


def tray_glyph(size: int, color: str) -> QImage:
    """The black-on-transparent tray glyph, recoloured."""
    image = png_image("tray.png", size).convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    painter = QPainter(image)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    painter.fillRect(image.rect(), QColor(color))
    painter.end()
    return image


def tray_icon(state: str, dark_bar: bool, template: bool = False) -> QIcon:
    """The tray glyph with a status dot: ``running`` (green), ``paused``
    (amber) or anything else (no dot, dimmed). macOS gets a plain template
    image, which the menu bar tints itself."""
    icon = QIcon()
    for size in (16, 22, 32, 44, 64):
        image = tray_glyph(size, "#000000" if template else ("#FFFFFF" if dark_bar else "#111111"))
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if state not in ("running", "paused"):
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
            painter.fillRect(image.rect(), QColor(0, 0, 0, 120))
        elif not template:
            r = size * 0.2
            centre = QPointF(size - r - 0.5, size - r - 0.5)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            painter.drawEllipse(centre, r + size * 0.08, r + size * 0.08)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            painter.setBrush(QColor(GOOD if state == "running" else WARN))
            painter.drawEllipse(centre, r, r)
        painter.end()
        icon.addPixmap(QPixmap.fromImage(image))
    if template:
        icon.setIsMask(True)
    return icon


def placeholder_size() -> QSize:
    return QSize(1100, 740)


# ------------------------------------------------------------------- style

STYLE = f"""
* {{ font-size: 14px; }}
QMainWindow, QWidget#page {{ background: {SURFACE}; color: {INK}; }}
QWidget#sidebar {{ background: {NAVY}; }}
QLabel#brand {{ color: white; font-size: 18px; font-weight: 700; }}
QLabel#brandSub {{ color: #A9B8CB; font-size: 12px; }}
QPushButton#nav {{
    text-align: left; padding: 10px 14px; border: 0; border-radius: 10px;
    color: #C3D0DF; background: transparent; font-size: 15px;
}}
QPushButton#nav:hover {{ background: {NAVY_2}; color: white; }}
QPushButton#nav:checked {{ background: {NAVY_3}; color: white; font-weight: 600; }}
QFrame#sideNote {{ background: {NAVY_2}; border-radius: 10px; }}
QLabel#sideNoteTitle {{ color: white; font-weight: 600; }}
QLabel#sideNoteText {{ color: #C3D0DF; font-size: 13px; }}

QLabel#h1 {{ font-size: 26px; font-weight: 700; }}
QLabel#h2 {{ font-size: 16px; font-weight: 600; }}
QLabel#big {{ font-size: 28px; font-weight: 700; }}
QLabel#eyebrow {{ color: {MUTED}; font-size: 12px; font-weight: 600; letter-spacing: 1px; }}
QLabel#muted {{ color: {MUTED}; }}
QLabel#small {{ color: {MUTED}; font-size: 13px; }}
QLabel#mono {{ font-family: "IBM Plex Mono", "Cascadia Mono", Menlo, Consolas, monospace; color: {MUTED}; }}
QLabel#code {{ font-family: "IBM Plex Mono", "Cascadia Mono", Menlo, Consolas, monospace; font-size: 40px; color: white; letter-spacing: 6px; }}
QLabel#warn {{ background: #FFF1DB; color: #8A5200; border-radius: 10px; padding: 10px 12px; }}
QLabel#error {{ background: #FCE8E4; color: #8E2A17; border-radius: 10px; padding: 10px 12px; }}

QFrame#card {{ background: white; border: 1px solid {LINE}; border-radius: 14px; }}
QFrame#darkCard {{ background: {NAVY}; border-radius: 14px; }}
QFrame#darkCard QLabel {{ color: #C3D0DF; }}
QFrame#row {{ border-top: 1px solid #EEF1F5; }}

QPushButton {{
    padding: 9px 16px; min-height: 22px; border-radius: 10px; border: 1px solid #C9D2DD;
    background: white; color: {INK}; font-weight: 600;
}}
QPushButton:hover {{ border-color: #9FB0C4; }}
QPushButton:disabled {{ color: #9AA5B3; }}
QPushButton#primary {{ background: {ACCENT}; color: white; border: 0; }}
QPushButton#primary:hover {{ background: #0F5AA3; }}
QPushButton#danger {{ color: #A23B26; border-color: #E0B9B1; }}
QPushButton#choice {{ text-align: left; padding: 12px 14px; }}
QPushButton#choice:checked {{ border: 2px solid {ACCENT}; background: #EAF3FC; color: #0F3B66; }}
QPushButton#seg:checked {{ border: 2px solid {ACCENT}; background: #EAF3FC; color: #0F3B66; }}

QLineEdit, QComboBox {{
    padding: 8px 10px; border: 1px solid #C9D2DD; border-radius: 10px; background: white;
}}
QSpinBox {{ min-height: 30px; }}
QPlainTextEdit {{ border: 1px solid {LINE}; border-radius: 10px; background: white;
    font-family: "IBM Plex Mono", "Cascadia Mono", Menlo, Consolas, monospace; font-size: 12px; }}
QScrollArea {{ border: 0; background: transparent; }}
QSlider::groove:horizontal {{ height: 4px; background: #C9D2DD; border-radius: 2px; }}
QSlider::handle:horizontal {{ width: 18px; margin: -8px 0; border-radius: 9px; background: {ACCENT}; }}
"""


def pill_style(kind: str) -> str:
    colours = {
        "on": ("#E3F4EC", "#1A6B48"),
        "off": ("#EEF1F5", "#4A5566"),
        "here": ("#EAF3FC", "#12599E"),
        "wait": ("#FFF1DB", "#8A5200"),
        "bad": ("#FCE8E4", "#8E2A17"),
    }
    bg, fg = colours.get(kind, colours["off"])
    return f"background: {bg}; color: {fg}; border-radius: 11px; padding: 3px 12px; font-size: 13px; font-weight: 600;"


def friendly_reason(reason: str) -> str:
    return {"gaze": "gaze", "edge": "edge", "hotkey": "hotkey", "disconnect": "disconnected", "app": "app"}.get(reason, reason)


def config_dir(path: Path) -> Path:
    return path.expanduser().resolve().parent
