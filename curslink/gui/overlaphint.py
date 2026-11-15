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

A region may also have ``quad``: eight numbers, the four corners (top left,
top right, bottom right, bottom left, same pixels) of a screen that leans
against this one, as a line-up measured it. The area is then drawn in that
shape (``rect`` is the rectangle round it, for a Curslink too old to).

``rect`` is in that screen's own pixels, from its top-left corner; ``size``
is the screen's size in the layout (to scale ``rect`` to what Qt calls the
screen's size). On a paired computer the host sends the same through a
``hint`` frame (see :func:`curslink.app.hint_command`).
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetricsF, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

from curslink.gui.overlay import find_qscreen

log = logging.getLogger(__name__)

OK = QColor("#2F6FED")
BAD = QColor("#D63C3C")


class _HintWindow(QWidget):
    def __init__(self, qscreen, regions: list[tuple[QRectF, str, bool]], shapes: list | None = None):
        flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
                 | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.WindowTransparentForInput)
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setGeometry(qscreen.geometry())
        self.regions = regions
        self.shapes = shapes or []  # a QPolygonF for each region that leans, else None

    def set_regions(self, regions: list[tuple[QRectF, str, bool]], shapes: list | None = None) -> None:
        self.regions = regions
        self.shapes = shapes or []
        self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = QFont()
        font.setPixelSize(15)
        font.setWeight(QFont.Weight.DemiBold)
        p.setFont(font)
        metrics = QFontMetricsF(font)
        for n, (rect, text, bad) in enumerate(self.regions):
            colour = BAD if bad else OK
            fill = QColor(colour)
            fill.setAlphaF(0.30)
            shape = self.shapes[n] if n < len(self.shapes) else None
            if shape is not None:
                # A screen that leans against this one: the shape it's seen as.
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(fill)
                p.drawPolygon(shape)
                p.setBrush(QBrush(QColor(255, 255, 255, 110), Qt.BrushStyle.BDiagPattern))
                p.drawPolygon(shape)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(QColor(255, 255, 255, 200), 5))
                p.drawPolygon(shape)
                p.setPen(QPen(colour, 3))
                p.drawPolygon(shape)
                rect = shape.boundingRect().intersected(QRectF(self.rect()))
                if text and not rect.isEmpty():
                    w = metrics.horizontalAdvance(text) + 28
                    h = metrics.height() + 14
                    pill = QRectF(rect.center().x() - w / 2, rect.center().y() - h / 2, w, h)
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(colour)
                    p.drawRoundedRect(pill, h / 2, h / 2)
                    p.setPen(QColor("white"))
                    p.drawText(pill, Qt.AlignmentFlag.AlignCenter, text)
                continue
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


def shapes_for(qscreen, entry: dict) -> list:
    """For each area :func:`regions_for` gives, its leaning shape (``quad``)
    as a QPolygonF in the window's coordinates, or None for a plain one."""
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
            [float(v) for v in r["rect"]][3]
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        quad = r.get("quad")
        try:
            pts = [QPointF(float(quad[i]) * kx, float(quad[i + 1]) * ky) for i in range(0, 8, 2)] \
                if isinstance(quad, list) and len(quad) == 8 else None
        except (TypeError, ValueError):
            pts = None
        out.append(QPolygonF(pts) if pts else None)
    return out


class OverlapHint:
    """The hint windows on this computer's monitors. ``show`` again to update
    them while dragging; ``hide`` takes them all down."""

    def __init__(self):
        self.windows: dict[str, _HintWindow] = {}
        self.edges = None  # RemoteEdges, while the host calibrates an edge

    def show(self, hint: dict) -> None:
        if hint.get("hide"):
            self.hide()
            return
        # Entries with an ``edge``: the host is calibrating an edge (see curslink.gui.edgecalib).
        # Or one with a ``fit``: lining up an overlap (see curslink.gui.fitcalib).
        edges = [e for e in hint.get("screens", []) if isinstance(e, dict) and isinstance(e.get("screen"), str)
                 and (isinstance(e.get("edge"), dict) or isinstance(e.get("fit"), dict))]
        if edges or self.edges is not None:
            if self.edges is None:
                from curslink.gui.edgecalib import RemoteEdges

                self.edges = RemoteEdges()
            self.edges.show(edges)
        hint = {**hint, "screens": [e for e in hint.get("screens", [])
                                if not (isinstance(e, dict) and ("edge" in e or "fit" in e))]}
        wanted: dict[str, list[tuple[QRectF, str, bool]]] = {}
        shapes: dict[str, list] = {}
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
            shapes.setdefault(key, []).extend(shapes_for(qscreen, entry))
        for key in list(self.windows):
            if key not in wanted or not wanted[key]:
                self.windows.pop(key).deleteLater()
        for key, regions in wanted.items():
            if not regions:
                continue
            window = self.windows.get(key)
            if window is None:
                window = _HintWindow(screens[key], regions, shapes.get(key))
                self.windows[key] = window
                window.show()
                window.raise_()
            else:
                window.set_regions(regions, shapes.get(key))

    def hide(self) -> None:
        for window in self.windows.values():
            window.hide()
            window.deleteLater()
        self.windows = {}
        if self.edges is not None:
            self.edges.hide()

    @property
    def showing(self) -> bool:
        return bool(self.windows) or bool(self.edges and self.edges.windows)

