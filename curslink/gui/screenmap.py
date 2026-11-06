"""A drawing of the screens in the layout, used on Status (where the pointer
is) and Calibrate (which screen you're looking at, which are recorded, and
which one to look at next). See the spec, section 5.3."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Property, QPointF, QPropertyAnimation, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from curslink.gui import theme


@dataclass
class MapScreen:
    device_id: str
    device_name: str
    screen_id: str
    x: float
    y: float
    w: float
    h: float
    home: bool

    @property
    def key(self) -> str:
        return f"{self.device_id}/{self.screen_id}"


def screens_from_config(cfg) -> list[MapScreen]:
    try:
        devices = cfg.devices()
        me = cfg.get("host", "device_id")
    except Exception:
        return []
    out = []
    for d in devices:
        for i, s in enumerate(d.get("screens", [])):
            out.append(MapScreen(d.get("id"), d.get("name", d.get("id")), str(s.get("id", f"screen{i}")),
                                 float(s.get("x", 0)), float(s.get("y", 0)), float(s.get("width", 1920)),
                                 float(s.get("height", 1080)), d.get("id") == me))
    return out


def _pointer_path(x: float, y: float, s: float = 1.0) -> QPainterPath:
    pts = [(0, 0), (0, 16), (4.6, 11.7), (7.9, 19.1), (11.1, 17.7), (7.8, 10.5), (14.2, 10.2)]
    path = QPainterPath(QPointF(x, y))
    for px, py in pts[1:]:
        path.lineTo(x + px * s, y + py * s)
    path.closeSubpath()
    return path


class ScreenMap(QWidget):
    """``states`` maps a screen key to any of: active, done, skipped, target, looking."""

    def __init__(self, compact: bool = False):
        super().__init__()
        self.compact = compact
        self.screens: list[MapScreen] = []
        self.states: dict[str, set[str]] = {}
        self._pulse = 0.0
        self._anim = QPropertyAnimation(self, b"pulse", self)
        self._anim.setStartValue(0.0)
        self._anim.setKeyValueAt(0.5, 1.0)
        self._anim.setEndValue(0.0)
        self._anim.setDuration(1800)
        self._anim.setLoopCount(-1)
        if compact:
            self.setFixedSize(150, 96)
        else:
            self.setMinimumHeight(240)
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setAccessibleName("Screen layout")

    def sizeHint(self) -> QSize:
        return QSize(150, 96) if self.compact else QSize(640, 260)

    def _get_pulse(self) -> float:
        return self._pulse

    def _set_pulse(self, v: float) -> None:
        self._pulse = v
        self.update()

    pulse = Property(float, _get_pulse, _set_pulse)

    def set_screens(self, screens: list[MapScreen]) -> None:
        self.screens = screens
        self.update()

    def set_states(self, states: dict[str, set[str]]) -> None:
        self.states = states
        animate = any("target" in s or "active" in s for s in states.values())
        if animate and self._anim.state() != QPropertyAnimation.State.Running:
            self._anim.start()
        elif not animate:
            self._anim.stop()
            self._pulse = 0.0
        self.update()

    # -- drawing --------------------------------------------------------------
    def _geometry(self) -> dict[str, QRectF]:
        if not self.screens:
            return {}
        left = min(s.x for s in self.screens)
        top = min(s.y for s in self.screens)
        right = max(s.x + s.w for s in self.screens)
        bottom = max(s.y + s.h for s in self.screens)
        pad = 8 if self.compact else 36
        area = QRectF(self.rect()).adjusted(pad, pad, -pad, -pad)
        scale = min(area.width() / max(1.0, right - left), area.height() / max(1.0, bottom - top))
        ox = area.left() + (area.width() - (right - left) * scale) / 2
        oy = area.top() + (area.height() - (bottom - top) * scale) / 2
        gap = 2 if self.compact else 3
        return {s.key: QRectF(ox + (s.x - left) * scale + gap / 2, oy + (s.y - top) * scale + gap / 2,
                              s.w * scale - gap, s.h * scale - gap) for s in self.screens}

    def paintEvent(self, _e) -> None:  # noqa: N802
        th = theme.current()
        t = th.tokens
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.compact:
            bg = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.qcolor(t["plot"]))
            p.drawRoundedRect(bg, th.size["r_card"] - 2, th.size["r_card"] - 2)
            dots = QColor(255, 255, 255, 20) if th.dark else theme.qcolor(t["stroke"])
            p.setPen(QPen(dots, 1.4))
            for x in range(16, self.width(), 16):
                for y in range(16, self.height(), 16):
                    p.drawPoint(x, y)
        rects = self._geometry()
        accent = theme.qcolor(t["accent"])
        good = theme.qcolor(t["good"])
        other = QColor(theme.OTHER_DEVICE)
        radius = 5 if self.compact else 7
        for s in self.screens:
            r = rects[s.key]
            st = self.states.get(s.key, set())
            fill = theme.qcolor(t["card"]) if not self.compact else theme.qcolor(t["card2"])
            border = theme.qcolor(t["ink3"])
            width = 2.0
            if not s.home and not self.compact:
                f = QColor(other)
                f.setAlphaF(0.14)
                fill = f
                border = QColor(other)
                border.setAlphaF(0.65)
            if "done" in st:
                border = good
            if "active" in st or "looking" in st or "target" in st:
                glow = QColor(accent)
                spread = 4 + (8 * self._pulse if ("target" in st or "active" in st) else 0)
                glow.setAlphaF(0.22 - 0.12 * self._pulse if ("target" in st or "active" in st) else 0.22)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(glow)
                p.drawRoundedRect(r.adjusted(-spread, -spread, spread, spread), radius + spread, radius + spread)
            if "active" in st or "looking" in st:
                border = accent
                fill = theme.qcolor(t["accent_soft"])
                fill = theme.over(t["accent_soft"], t["card"])
            elif "target" in st:
                border = accent
            p.setPen(QPen(border, width))
            p.setBrush(fill)
            p.drawRoundedRect(r, radius, radius)

            if self.compact:
                if "active" in st:
                    c = r.center()
                    p.setPen(Qt.PenStyle.NoPen)
                    halo = QColor(accent)
                    halo.setAlphaF(0.22)
                    p.setBrush(halo)
                    p.drawEllipse(c, 9 + 3 * self._pulse, 9 + 3 * self._pulse)
                    p.setBrush(accent)
                    p.drawEllipse(c, 5, 5)
                continue

            # Labels: device name and screen id.
            f = QFont(self.font())
            f.setPixelSize(12)
            f.setWeight(QFont.Weight.DemiBold)
            p.setFont(f)
            p.setPen(theme.qcolor(t["ink"]))
            inner = r.adjusted(10, 8, -10, -8)
            p.drawText(inner, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, s.device_name)
            f.setWeight(QFont.Weight.Normal)
            f.setPixelSize(11)
            p.setFont(f)
            p.setPen(theme.qcolor(t["ink2"]))
            p.drawText(inner.adjusted(0, 16, 0, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, s.screen_id)
            if "done" in st:
                c = QPointF(r.right() - 16, r.top() + 16)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(good)
                p.drawEllipse(c, 10, 10)
                p.setPen(QPen(QColor("#FFFFFF"), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                              Qt.PenJoinStyle.RoundJoin))
                path = QPainterPath(QPointF(c.x() - 4.5, c.y()))
                path.lineTo(c.x() - 1.2, c.y() + 3.3)
                path.lineTo(c.x() + 4.8, c.y() - 3.5)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(path)
            if "target" in st:
                f.setPixelSize(11)
                f.setWeight(QFont.Weight.DemiBold)
                p.setFont(f)
                text = "Look here"
                tw = p.fontMetrics().horizontalAdvance(text) + 16
                pill = QRectF(inner.left(), inner.bottom() - 18, tw, 18)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(accent)
                p.drawRoundedRect(pill, 9, 9)
                p.setPen(theme.qcolor(t["accent_ink"]))
                p.drawText(pill, Qt.AlignmentFlag.AlignCenter, text)
            if "looking" in st:
                c = r.center()
                path = _pointer_path(c.x(), c.y() - 6, 1.1)
                p.setPen(QPen(QColor("#1D3A66"), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                              Qt.PenJoinStyle.RoundJoin))
                p.setBrush(QColor("#FFFFFF"))
                p.drawPath(path)
        if not self.screens and not self.compact:
            p.setPen(theme.qcolor(t["ink2"]))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No screens in the layout yet")
        p.end()
