"""Draws the edge indicator (see :mod:`curslink.edgehint`) in the overlay helper.

Two click-through windows that never take focus, like the switch animation's:

* the **bar**: a soft bar of light along the edge, brightest beside the
  pointer and fading to nothing at both ends;
* the **label**: a small glass label beside the pointer with the icon and
  name of the device the edge leads to (only on the screen the pointer is on).

On macOS the label sits on real vibrancy (an ``NSVisualEffectView``); on
Windows and Linux it's a translucent fill in the Fluent / Adwaita colours.
Both follow the system's light or dark mode and accent colour.
"""

from __future__ import annotations

import logging
import math
import sys
import math
import random
import time
from typing import Callable

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QGuiApplication, QImage, QLinearGradient,
                           QPainter, QPainterPath, QPalette, QPen)
from PySide6.QtWidgets import QWidget

log = logging.getLogger(__name__)

THICK = 64          # how far the bar's glow reaches into the screen (logical px)
CORE = 3            # the bright line right on the edge
LABEL_GAP = 14      # label to the edge
LABEL_LIFT = 22     # label's near end to the pointer
SLIDE = 14          # the label slides out from the edge by this much as it fades in
# Fading in or out takes as long as a mirror's fade (curslink.mirror.helper),
# so the glow and the mirror come and go together.
FADE_S = 0.22
FLASH_S = FADE_S    # crossing: the bar flares and is gone as the mirror fades

KIND_TEXT = {"mac": "Mac", "windows": "Windows PC", "linux": "Linux PC", "phone": "Android phone"}


def platform_look() -> str:
    if sys.platform == "darwin":
        return "mac"
    if sys.platform == "win32":
        return "win"
    return "linux"


def is_dark() -> bool:
    try:
        return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except Exception:  # pragma: no cover - Qt older than 6.5
        return QGuiApplication.palette().color(QPalette.ColorRole.Window).lightness() < 128


def accent(dark: bool, look: str | None = None) -> QColor:
    """The system accent where Qt knows it, else the platform's default."""
    try:
        c = QGuiApplication.palette().color(QPalette.ColorRole.Accent)
        if c.isValid() and c.saturation() > 40:
            return c
    except Exception:  # pragma: no cover - Qt older than 6.6
        pass
    look = look or platform_look()
    return QColor({("mac", False): "#007AFF", ("mac", True): "#0A84FF",
                   ("win", False): "#005FB8", ("win", True): "#60CDFF",
                   ("linux", False): "#3584E4", ("linux", True): "#78AEED"}[(look, dark)])


def length_stops(length: float, at: float, base: float = 0.25, focus: float = 200.0) -> list[tuple[float, float]]:
    """Alpha along the bar: 0 at both ends, 1 beside the pointer, ``base``
    in between, so it reads as a bar of light rather than a drawn line."""
    if length <= 0:
        return [(0.0, 0.0), (1.0, 0.0)]
    p = min(max(at / length, 0.04), 0.96)
    f = focus / length
    points = {0.0: 0.0, 1.0: 0.0, p: 1.0}
    for pos, alpha in ((0.14, base), (0.86, base), (p - f, 0.45), (p + f, 0.45)):
        if 0.02 < pos < 0.98 and abs(pos - p) > 0.01:
            points.setdefault(round(pos, 4), alpha)
    # The ends always fade out: nothing near an end may be brighter than the
    # ramp up to it.
    out = []
    for pos in sorted(points):
        alpha = points[pos]
        cap = min(pos, 1 - pos) / 0.14
        out.append((pos, min(alpha, max(cap, 0.35 if pos == p else 0.0))))
    return out


def _flags():
    flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
             | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowTransparentForInput
             | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.NoDropShadowWindowHint)
    if sys.platform.startswith("linux"):
        flags |= Qt.WindowType.X11BypassWindowManagerHint
    return flags


class _Overlay(QWidget):
    def __init__(self) -> None:
        super().__init__(None, _flags())
        for attr in (Qt.WidgetAttribute.WA_TranslucentBackground, Qt.WidgetAttribute.WA_ShowWithoutActivating,
                     Qt.WidgetAttribute.WA_TransparentForMouseEvents, Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow):
            self.setAttribute(attr, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._hidden_from_capture = False

    def showEvent(self, e) -> None:  # noqa: N802
        super().showEvent(e)
        if not self._hidden_from_capture:
            # Mirror mode would otherwise show it again on the screen in front.
            from curslink.capturehide import exclude_from_capture

            exclude_from_capture(self)
            self._hidden_from_capture = True


class Bar(_Overlay):
    def __init__(self) -> None:
        super().__init__()
        self.side = "right"
        self.at = 0.0       # pointer along the bar, logical px from its start
        self.level = 0.0    # 0..1 how strong
        self.boost = 0.0    # 0..1 extra while flashing
        self.dark = False
        self.colour = QColor("#007AFF")

    def paintEvent(self, _e) -> None:  # noqa: N802
        # Flaring on a crossing it goes from full to nothing in a straight
        # line, like the mirror's fade (window opacity, linear).
        strength = min(1.0, self.level) if self.boost > 0 else 0.9 * self.level
        if strength <= 0.003:
            return
        p = QPainter(self)
        p.drawImage(0, 0, self.render(strength, self.devicePixelRatioF()))
        p.end()

    def render(self, strength: float, dpr: float = 1.0) -> QImage:
        """The bar at ``strength``, at the screen's real resolution. Drawn at
        16 bits a channel and dithered down, so its long faint fades don't
        break into bands; the glow across it is one smooth falloff."""
        w, h = self.width(), self.height()
        vertical = self.side in ("left", "right")
        length = h if vertical else w
        img = QImage(max(1, round(w * dpr)), max(1, round(h * dpr)), QImage.Format.Format_RGBA64_Premultiplied)
        img.setDevicePixelRatio(dpr)
        img.fill(0)
        p = QPainter(img)
        # Across the bar: from the edge into the screen.
        start, stop = {"right": ((w, 0), (0, 0)), "left": ((0, 0), (w, 0)),
                       "bottom": ((0, h), (0, 0)), "top": ((0, 0), (0, h))}[self.side]
        thick = w if vertical else h
        g = QLinearGradient(*start, *stop)
        c = QColor(self.colour)
        for pos, a in glow_stops(thick, self.dark, self.boost):
            c.setAlphaF(a)
            g.setColorAt(pos, c)
        p.fillRect(QRectF(0, 0, w, h), QBrush(g))
        # A whiter heart right beside the pointer, on the edge.
        heart = QImage(img.size(), QImage.Format.Format_RGBA64_Premultiplied)
        heart.setDevicePixelRatio(dpr)
        heart.fill(0)
        hp = QPainter(heart)
        hot = QColor(255, 255, 255)
        hg = QLinearGradient(0, 0, 0, h) if vertical else QLinearGradient(0, 0, w, 0)
        for pos, a in ((max(0.0, (self.at - 90) / length), 0.0), (min(max(self.at / length, 0), 1), 0.75),
                       (min(1.0, (self.at + 90) / length), 0.0)):
            hot.setAlphaF(a)
            hg.setColorAt(pos, hot)
        hp.fillRect(QRectF(0, 0, w, h), QBrush(hg))
        hp.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        across = QLinearGradient(*start, *stop)
        for pos, a in ((0.0, 1.0), (min(1.0, 1.5 / max(thick, 1)), 0.8), (min(1.0, 4.0 / max(thick, 1)), 0.0)):
            across.setColorAt(pos, QColor(0, 0, 0, round(255 * a)))
        hp.fillRect(QRectF(0, 0, w, h), QBrush(across))
        hp.end()
        p.drawImage(0, 0, heart)
        # Along the bar: dark at both ends, bright by the pointer.
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        mask = QLinearGradient(0, 0, 0, h) if vertical else QLinearGradient(0, 0, w, 0)
        for pos, a in length_stops(length, self.at):
            m = QColor(0, 0, 0)
            m.setAlphaF(a * strength)
            mask.setColorAt(pos, m)
        p.fillRect(QRectF(0, 0, w, h), QBrush(mask))
        # Dither: up to one 8-bit step of noise before it's cut down to 8 bits.
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        p.resetTransform()
        p.fillRect(QRectF(0, 0, img.width(), img.height()), QBrush(_noise(self.colour)))
        p.end()
        return img.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)


def glow_stops(thick: float, dark: bool, boost: float = 0.0, n: int = 28) -> list[tuple[float, float]]:
    """Alpha across the bar, from the edge (0) into the screen (1): a bright
    line on the edge melting into a soft haze, one smooth curve."""
    haze = (0.46 if dark else 0.4) + 0.3 * boost
    out = []
    for i in range(n + 1):
        pos = (i / n) ** 1.6  # more stops near the edge, where it changes fastest
        d = pos * thick
        a = 0.9 * math.exp(-d / 2.2) + 0.42 * math.exp(-d / 8) + haze * math.exp(-d / 20)
        a *= max(0.0, 1 - pos) ** 1.5  # nothing left at the far side
        out.append((pos, min(1.0, a)))
    return out


_NOISE: dict[str, QImage] = {}


def _noise(colour: QColor) -> QImage:
    """A tile of tiny premultiplied ``colour`` specks, up to one 8-bit step."""
    key = colour.name()
    tile = _NOISE.get(key)
    if tile is None:
        rnd = random.Random(7)
        tile = QImage(64, 64, QImage.Format.Format_RGBA64_Premultiplied)
        r, g, b = colour.redF(), colour.greenF(), colour.blueF()
        for y in range(64):
            for x in range(64):
                a = rnd.random() / 255
                c = QColor()
                c.setRgbF(r, g, b, a)  # (premultiplied as it goes in)
                tile.setPixelColor(x, y, c)
        _NOISE[key] = tile
    return tile


def _icon_path(kind: str, size: float) -> QPainterPath:
    """A line icon for the device, in a ``size`` square."""
    s = size / 24.0
    path = QPainterPath()
    if kind == "phone":
        path.addRoundedRect(QRectF(6.5 * s, 2.5 * s, 11 * s, 19 * s), 2.4 * s, 2.4 * s)
        path.moveTo(10.5 * s, 18.5 * s)
        path.lineTo(13.5 * s, 18.5 * s)
    elif kind == "mac":
        path.addRoundedRect(QRectF(4 * s, 4.5 * s, 16 * s, 11 * s), 1.6 * s, 1.6 * s)
        path.moveTo(2 * s, 19.5 * s)
        path.lineTo(22 * s, 19.5 * s)
        path.lineTo(20.5 * s, 15.5 * s)
        path.moveTo(2 * s, 19.5 * s)
        path.lineTo(3.5 * s, 15.5 * s)
    else:
        path.addRoundedRect(QRectF(2.5 * s, 3.5 * s, 19 * s, 13 * s), 2 * s, 2 * s)
        path.moveTo(8 * s, 20.5 * s)
        path.lineTo(16 * s, 20.5 * s)
        path.moveTo(12 * s, 16.5 * s)
        path.lineTo(12 * s, 20.5 * s)
    return path


class Label(_Overlay):
    """The glass label: icon, name, what it is, and a chevron to the edge."""

    MARGIN = 0  # room for a painted shadow (Windows / Linux)

    def __init__(self, look: str | None = None) -> None:
        super().__init__()
        self.look = look or platform_look()
        self.dark = False
        self.colour = QColor("#007AFF")
        self.name = ""
        self.kind = "windows"
        self.side = "right"
        self.glass = False  # macOS vibrancy behind it
        self.base = (0.0, 0.0)  # where it sits fully out (the edge overlay slides it in)
        self.MARGIN = 0 if self.look == "mac" else 10
        self._name_font = QFont(self.font())
        self._name_font.setPixelSize(13)
        self._name_font.setWeight(QFont.Weight.DemiBold)
        self._sub_font = QFont(self.font())
        self._sub_font.setPixelSize(11)

    def set_content(self, name: str, kind: str, side: str, dark: bool, colour: QColor) -> None:
        changed = (name, kind, side, dark, colour.name()) != (self.name, self.kind, self.side, self.dark,
                                                              self.colour.name())
        self.name, self.kind, self.side, self.dark, self.colour = name, kind, side, dark, colour
        if changed:
            self.resize(self.sizeHint())
            self._mac_glass()
            self.update()

    # -- geometry
    def chip(self) -> float:
        return 32.0

    def body_size(self) -> QSize:
        name_w = QFontMetricsF(self._name_font).horizontalAdvance(self.name or " ")
        sub_w = QFontMetricsF(self._sub_font).horizontalAdvance(KIND_TEXT.get(self.kind, ""))
        pad = 7 if self.look == "mac" else 8
        w = pad + self.chip() + 10 + max(name_w, sub_w) + 10 + 8 + 14
        return QSize(math.ceil(w), math.ceil(self.chip() + 2 * pad))

    def sizeHint(self) -> QSize:  # noqa: N802
        b = self.body_size()
        return QSize(b.width() + 2 * self.MARGIN, b.height() + 2 * self.MARGIN)

    def radius(self, h: float) -> float:
        return h / 2 if self.look == "mac" else (8.0 if self.look == "win" else 12.0)

    # -- macOS vibrancy
    def _mac_glass(self) -> None:
        # Only a real Cocoa window has an NSView behind winId(); under another
        # Qt platform (offscreen, in tests) it's not one, and using it crashes.
        if self.look != "mac" or sys.platform != "darwin" or QGuiApplication.platformName() != "cocoa":
            return
        try:
            import AppKit
            import objc

            view = objc.objc_object(c_void_p=int(self.winId()))
            window = view.window()
            if window is None:
                return
            window.setOpaque_(False)
            window.setBackgroundColor_(AppKit.NSColor.clearColor())
            window.setHasShadow_(True)
            content = window.contentView()
            holder = content.superview() or content
            effect = getattr(self, "_effect", None)
            if effect is None:
                effect = AppKit.NSVisualEffectView.alloc().initWithFrame_(holder.bounds())
                effect.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
                effect.setBlendingMode_(AppKit.NSVisualEffectBlendingModeBehindWindow)
                effect.setState_(AppKit.NSVisualEffectStateActive)
                effect.setMaterial_(AppKit.NSVisualEffectMaterialHUDWindow)
                effect.setWantsLayer_(True)
                holder.addSubview_positioned_relativeTo_(effect, AppKit.NSWindowBelow, content)
                self._effect = effect
            h = self.body_size().height()
            effect.layer().setCornerRadius_(h / 2)
            effect.layer().setMasksToBounds_(True)
            effect.setAppearance_(AppKit.NSAppearance.appearanceNamed_(
                AppKit.NSAppearanceNameDarkAqua if self.dark else AppKit.NSAppearanceNameAqua))
            self.glass = True
        except Exception:
            log.debug("no vibrancy for the edge label", exc_info=True)
            self.glass = False

    def showEvent(self, e) -> None:  # noqa: N802
        super().showEvent(e)
        self._mac_glass()

    # -- painting
    def fill(self) -> QColor:
        if self.look == "mac":
            if self.glass:
                return QColor(255, 255, 255, 64) if not self.dark else QColor(40, 40, 46, 70)
            return QColor(246, 246, 248, 225) if not self.dark else QColor(38, 38, 42, 225)
        if self.look == "win":
            return QColor(243, 243, 243, 232) if not self.dark else QColor(44, 44, 44, 235)
        return QColor(250, 250, 251, 236) if not self.dark else QColor(46, 46, 50, 236)

    def ink(self) -> tuple[QColor, QColor]:
        if self.dark:
            return QColor(245, 245, 247), QColor(245, 245, 247, 160)
        return QColor(29, 29, 31), QColor(29, 29, 31, 150)

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        m = self.MARGIN
        body = QRectF(m, m, self.width() - 2 * m, self.height() - 2 * m)
        r = self.radius(body.height())
        if m:
            for i in range(m, 0, -2):  # a soft shadow
                sh = QColor(0, 0, 0, round((0.10 if not self.dark else 0.22) * 255 * (1 - i / (m + 1)) / 2.5))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(sh)
                p.drawRoundedRect(body.adjusted(-i, -i + 3, i, i + 3), r + i, r + i)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self.fill())
        p.drawRoundedRect(body, r, r)
        # Hairline and a top highlight.
        edge = QColor(255, 255, 255, 40) if self.dark else QColor(0, 0, 0, 22)
        p.setPen(QPen(edge, 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(body.adjusted(0.5, 0.5, -0.5, -0.5), r, r)
        if self.look == "mac":
            p.setPen(QPen(QColor(255, 255, 255, 60 if self.dark else 200), 1))
            p.drawLine(QPointF(body.left() + r, body.top() + 1), QPointF(body.right() - r, body.top() + 1))

        mirrored = self.side == "left"
        pad = 7 if self.look == "mac" else 8
        chip = self.chip()
        name_ink, sub_ink = self.ink()
        cy = body.center().y()
        chip_x = body.right() - pad - chip if mirrored else body.left() + pad
        # Icon chip.
        if self.look == "mac":
            chip_fill = QColor(255, 255, 255, 30) if self.dark else QColor(255, 255, 255, 190)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(chip_fill)
            p.drawEllipse(QRectF(chip_x, cy - chip / 2, chip, chip))
        else:
            soft = QColor(self.colour)
            soft.setAlphaF(0.22 if self.dark else 0.13)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(soft)
            p.drawRoundedRect(QRectF(chip_x, cy - chip / 2, chip, chip), 6, 6)
        icon = _icon_path(self.kind, 18)
        p.save()
        p.translate(chip_x + (chip - 18) / 2, cy - 9)
        p.setPen(QPen(self.colour, 1.7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(icon)
        p.restore()
        # Name and what it is.
        text_x = body.left() + pad + 8 + 14 if mirrored else chip_x + chip + 10
        text_w = body.width() - pad - chip - 10 - 8 - 14 - pad
        p.setFont(self._name_font)
        p.setPen(name_ink)
        p.drawText(QRectF(text_x, cy - 17, text_w, 18), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   self.name)
        p.setFont(self._sub_font)
        p.setPen(sub_ink)
        p.drawText(QRectF(text_x, cy + 1, text_w, 15), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   KIND_TEXT.get(self.kind, ""))
        # Chevron pointing across the edge.
        chev = QColor(name_ink)
        chev.setAlphaF(0.55)
        p.setPen(QPen(chev, 1.7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        x = body.left() + pad + 6 if mirrored else body.right() - pad - 6
        if self.side in ("top", "bottom"):
            x -= 2.25  # centred where the sideways chevron sits
        path = QPainterPath()
        first, tip, last = chevron_points(self.side, x, cy)
        path.moveTo(*first)
        path.lineTo(*tip)
        path.lineTo(*last)
        p.drawPath(path)
        p.end()


def chevron_points(side: str, x: float, y: float, s: float = 4.5) -> tuple[tuple[float, float], ...]:
    """The label's chevron, pointing towards ``side``: (arm, tip, arm).

    Sideways the tip is at (x, y); up or down the chevron is centred on it.
    """
    if side == "left":
        return (x + s, y - s), (x, y), (x + s, y + s)
    if side == "top":
        return (x - s, y + s / 2), (x, y - s / 2), (x + s, y + s / 2)
    if side == "bottom":
        return (x - s, y - s / 2), (x, y + s / 2), (x + s, y - s / 2)
    return (x - s, y - s), (x, y), (x - s, y + s)


class EdgeOverlay:
    """Takes ``edge`` commands (dicts) and keeps the bar and label in step."""

    def __init__(self, to_logical: Callable[[float, float], tuple[float, float]]):
        self.to_logical = to_logical
        self.bar: Bar | None = None
        self.label: Label | None = None
        self.data: dict | None = None
        self.target = 0.0
        self.level = 0.0
        self.flash_at: float | None = None
        self._ticked = 0.0
        self.timer = QTimer()
        self.timer.setInterval(16)
        self.timer.timeout.connect(self._tick)

    def handle(self, data: dict) -> None:
        if data.get("hide"):
            self.target = 0.0
        elif data.get("flash"):
            if self.data is not None and self.level > 0.02:
                self.flash_at = time.monotonic()
            self.target = 0.0
        elif data.get("role") in ("from", "to"):
            self.data = data
            self.flash_at = None
            self.target = max(0.0, min(1.0, float(data.get("near", 0))))
            self._place()
        else:
            return
        if not self.timer.isActive():
            self._ticked = time.monotonic()
            self.timer.start()
        self._tick()

    # -- geometry
    def _screen_box(self, d: dict) -> tuple[float, float, float, float, float, float]:
        """The command's ``rect`` (OS pixels) in Qt's logical pixels, and the
        scale across and down. With ``screen_os`` (the whole screen as the
        host's layout has it) it goes by fractions of that screen, like the
        mirror does: with Windows scaling the layout's pixels needn't be the
        screen's real ones."""
        x, y, w, h = (float(v) for v in d["rect"])
        if isinstance(d.get("screen_os"), list):
            from curslink.mirror.helper import spec_to_logical

            got = spec_to_logical({"os": [x, y, w, h], "screen_os": d["screen_os"],
                                   "screen": str(d.get("screen", ""))})
            if got is not None and w > 0 and h > 0:
                lx, ly, lw, lh, _index = got
                return lx, ly, lw, lh, lw / w, lh / h
        lx, ly = self.to_logical(x, y)
        rx, ry = self.to_logical(x + w - 1, y + h - 1)
        scale = (rx - lx + 1) / w if w > 1 else 1.0
        if scale <= 0:
            scale = 1.0
        return lx, ly, w * scale, h * scale, scale, scale

    def _place(self) -> None:
        d = self.data
        if d is None:
            return
        if self.bar is None:
            self.bar = Bar()
        lx, ly, lw, lh, sx, sy = self._screen_box(d)
        side = d["side"]
        s = sy if side in ("left", "right") else sx  # along the edge
        a, b = sorted((float(d["a"]) * s, float(d["b"]) * s))
        at = float(d["at"]) * s
        span = max(1.0, b - a)
        if side == "right":
            geo = (lx + lw - THICK, ly + a, THICK, span)
        elif side == "left":
            geo = (lx, ly + a, THICK, span)
        elif side == "top":
            geo = (lx + a, ly, span, THICK)
        else:
            geo = (lx + a, ly + lh - THICK, span, THICK)
        dark = is_dark()
        colour = accent(dark)
        bar = self.bar
        bar.side, bar.at, bar.dark, bar.colour = side, at - a, dark, colour
        bar.setGeometry(*(round(v) for v in geo))
        if d["role"] != "from":
            if self.label is not None:
                self.label.hide()
            return
        if self.label is None:
            self.label = Label()
        label = self.label
        label.set_content(str(d.get("name", "")), str(d.get("kind", "windows")), side, dark, colour)
        size = label.sizeHint()
        m = label.MARGIN
        bw, bh = size.width() - 2 * m, size.height() - 2 * m
        if side in ("left", "right"):
            top = ly + at - LABEL_LIFT - bh
            top = min(max(top, ly + 8), ly + lh - bh - 8)
            left = lx + LABEL_GAP if side == "left" else lx + lw - LABEL_GAP - bw
        else:
            left = lx + at + 26
            if left + bw > lx + lw - 8:
                left = lx + at - 26 - bw
            left = min(max(left, lx + 8), lx + lw - bw - 8)
            top = ly + LABEL_GAP if side == "top" else ly + lh - LABEL_GAP - bh
        label.base = (left - m, top - m)
        label.resize(size)

    def _tick(self) -> None:
        now = time.monotonic()
        boost = 0.0
        if self.flash_at is not None:
            t = (now - self.flash_at) / FLASH_S
            if t >= 1:
                self.flash_at = None
                self.level = 0.0
            else:
                boost = 1 - t
                self.level = 1 - t  # linear, as the mirror fades
        else:
            # Linear, at a whole fade per FADE_S.
            step = max(0.0, min(0.1, now - self._ticked)) / FADE_S
            if self.level < self.target:
                self.level = min(self.target, self.level + step)
            else:
                self.level = max(self.target, self.level - step)
        self._ticked = now
        if self.bar is not None:
            self.bar.level, self.bar.boost = self.level, boost
            if self.level > 0.003:
                if not self.bar.isVisible():
                    self.bar.show()
                self.bar.update()
            elif self.bar.isVisible():
                self.bar.hide()
        label = self.label
        if label is not None and self.data is not None and self.data.get("role") == "from":
            shown = min(1.0, self.level * 1.15) if self.level > 0.05 else 0.0
            if shown > 0:
                bx, by = label.base
                off = (1 - min(1.0, self.level)) * SLIDE
                side = self.data["side"]
                dx, dy = {"right": (off, 0), "left": (-off, 0), "top": (0, -off), "bottom": (0, off)}[side]
                label.move(round(bx + dx), round(by + dy))
                label.setWindowOpacity(shown)
                if not label.isVisible():
                    label.show()
            elif label.isVisible():
                label.hide()
        elif label is not None and label.isVisible():
            label.hide()
        if self.level == self.target and self.flash_at is None and self.level == 0.0:
            self.timer.stop()
