"""Where two screens overlap, shown on the real monitor while you edit the layout.

As you drag a screen over another on the Layout page, the part of the
monitor behind that the other screen covers lights up on that monitor itself:
a striped box with a label, so you can line it up with the laptop standing
in front of it on your desk. It's click-through and never takes focus.

A hint is a list of screens, each with the areas to mark::

    {"screens": [{"screen": "screen2", "os": [2560, 0, 2560, 1440],
                  "size": [2560, 1440],
                  "regions": [{"rect": [1920, 1080, 640, 360],
                               "label": "WKS82261 stands here", "bad": false}]}]}

``rect`` is in that screen's own pixels, from its top-left corner; ``size``
is the screen's size in the layout (to scale ``rect`` to what Qt calls the
screen's size). On a paired computer the host sends the same through a
``hint`` frame (see :func:`curslink.app.hint_command`).
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import QWidget

from curslink.gui.overlay import find_qscreen

log = logging.getLogger(__name__)

OK = QColor("#2F6FED")
BAD = QColor("#D63C3C")


class _HintWindow(QWidget):
    def __init__(self, qscreen, regions: list[tuple[QRectF, str, bool]]):
        flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
                 | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.WindowTransparentForInput)
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setGeometry(qscreen.geometry())
        self.regions = regions

    def set_regions(self, regions: list[tuple[QRectF, str, bool]]) -> None:
        self.regions = regions
        self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = QFont()
        font.setPixelSize(15)
        font.setWeight(QFont.Weight.DemiBold)
        p.setFont(font)
        metrics = QFontMetricsF(font)
        for rect, text, bad in self.regions:
            colour = BAD if bad else OK
            fill = QColor(colour)
            fill.setAlphaF(0.30)
            p.fillRect(rect, fill)
            # White stripes and edge as well as the colour, to show on any wallpaper.
            p.fillRect(rect, QBrush(QColor(255, 255, 255, 110), Qt.BrushStyle.BDiagPattern))
            p.setPen(QPen(QColor(255, 255, 255, 200), 2))
            p.drawRect(rect.adjusted(1, 1, -1, -1))
            p.setPen(QPen(colour, 4))
            p.drawRect(rect.adjusted(4, 4, -4, -4))
            if text:
                w = metrics.horizontalAdvance(text) + 28
                h = metrics.height() + 14
                pill = QRectF(rect.center().x() - w / 2, rect.center().y() - h / 2, w, h)
                # Keep the label on screen when the area is a thin strip.
                pill.moveLeft(max(rect.left() + 6, min(pill.left(), rect.right() - w - 6)) if w < rect.width()
                              else rect.left() + 6)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(colour)
                p.drawRoundedRect(pill, h / 2, h / 2)
                p.setPen(QColor("white"))
                p.drawText(pill, Qt.AlignmentFlag.AlignCenter, text)
        p.end()


def regions_for(qscreen, entry: dict) -> list[tuple[QRectF, str, bool]]:
    """The areas of ``entry`` in the window's own (Qt) coordinates."""
    g = qscreen.geometry()
    size = entry.get("size") or (entry.get("os") or [0, 0, 0, 0])[2:]
    try:
        sw, sh = float(size[0]), float(size[1])
    except (TypeError, ValueError, IndexError):
        return []
    if sw <= 0 or sh <= 0:
        return []
    kx, ky = g.width() / sw, g.height() / sh
    out = []
    for r in entry.get("regions", []):
        try:
            x, y, w, h = (float(v) for v in r["rect"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append((QRectF(x * kx, y * ky, w * kx, h * ky), str(r.get("label", "")), bool(r.get("bad"))))
    return out


class OverlapHint:
    """The hint windows on this computer's monitors. ``show`` again to update
    them while dragging; ``hide`` takes them all down."""

    def __init__(self):
        self.windows: dict[str, _HintWindow] = {}

    def show(self, hint: dict) -> None:
        if hint.get("hide"):
            self.hide()
            return
        wanted: dict[str, list[tuple[QRectF, str, bool]]] = {}
        screens: dict[str, object] = {}
        for entry in hint.get("screens", []):
            if not isinstance(entry, dict) or not isinstance(entry.get("screen"), str):
                continue
            qscreen = find_qscreen(entry["screen"], entry.get("os"))
            if qscreen is None:
                continue
            key = qscreen.name() or entry["screen"]
            screens[key] = qscreen
            wanted.setdefault(key, []).extend(regions_for(qscreen, entry))
        for key in list(self.windows):
            if key not in wanted or not wanted[key]:
                self.windows.pop(key).deleteLater()
        for key, regions in wanted.items():
            if not regions:
                continue
            window = self.windows.get(key)
            if window is None:
                window = _HintWindow(screens[key], regions)
                self.windows[key] = window
                window.show()
                window.raise_()
            else:
                window.set_regions(regions)

    def hide(self) -> None:
        for window in self.windows.values():
            window.hide()
            window.deleteLater()
        self.windows = {}

    @property
    def showing(self) -> bool:
        return bool(self.windows)

