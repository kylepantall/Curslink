"""Calibrate an edge: click where two monitors join, on the real monitors.

While calibrating the edge between two screens of different computers, each
of those screens is dimmed and the joining edge lights up along its side. You
click where the other screen starts along that edge, then where it ends;
only the position along the edge counts, so a click anywhere near it will
do. A guide follows the pointer and shows the point a click would record.

The same drawing is used on the host (where it takes the clicks itself) and
on a paired computer (where the host catches the clicks and sends the state
in a ``hint`` frame, so the window there is click-through).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from PySide6.QtCore import QLineF, QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QCursor, QFont, QFontMetricsF, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QWidget

EDGE = QColor("#2F6FED")
EDGE_LIT = QColor("#5B8FF5")
DONE = QColor("#1F9D5B")
TINT = QColor(10, 14, 22, round(255 * 0.74))
GLOW = 110      # how far the glow reaches into the screen, px
LINE = 8        # width of the lit edge
DOT = 15        # radius of a clicked point


@dataclass
class EdgeState:
    """What one screen shows. ``points`` and ``pointer`` are along the edge,
    in the window's own pixels (y for left/right edges, x for top/bottom)."""
    side: str                     # left, right, top, bottom
    other: str                    # the screen on the other side, as named to the user
    points: list[float] = field(default_factory=list)
    pointer: QPointF | None = None
    waiting: str = ""             # set when it's the other screen's turn


def _font(px: int, weight=QFont.Weight.Normal) -> QFont:
    f = QFont()
    f.setPixelSize(px)
    f.setWeight(weight)
    return f


def paint_edge(p: QPainter, rect: QRectF, st: EdgeState) -> None:
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.fillRect(rect, TINT)
    vertical = st.side in ("left", "right")

    def at(t: float, inset: float = 0.0) -> QPointF:
        """The point t along the edge, ``inset`` px into the screen (kept
        far enough from the corners for a dot to show whole)."""
        if inset:
            t = max(DOT + 4, min(t, length - DOT - 4))
        if st.side == "left":
            return QPointF(rect.left() + inset, t)
        if st.side == "right":
            return QPointF(rect.right() - inset, t)
        if st.side == "top":
            return QPointF(t, rect.top() + inset)
        return QPointF(t, rect.bottom() - inset)

    length = rect.height() if vertical else rect.width()

    # Glow and the lit edge.
    if vertical:
        band = QRectF(rect.right() - GLOW, rect.top(), GLOW, rect.height()) if st.side == "right" \
            else QRectF(rect.left(), rect.top(), GLOW, rect.height())
        g = QLinearGradient(band.right(), 0, band.left(), 0) if st.side == "right" \
            else QLinearGradient(band.left(), 0, band.right(), 0)
    else:
        band = QRectF(rect.left(), rect.bottom() - GLOW, rect.width(), GLOW) if st.side == "bottom" \
            else QRectF(rect.left(), rect.top(), rect.width(), GLOW)
        g = QLinearGradient(0, band.bottom(), 0, band.top()) if st.side == "bottom" \
            else QLinearGradient(0, band.top(), 0, band.bottom())
    glow = QColor(EDGE_LIT)
    glow.setAlphaF(0.75)
    g.setColorAt(0, glow)
    glow.setAlphaF(0.0)
    g.setColorAt(1, glow)
    p.fillRect(band, QBrush(g))
    p.setPen(QPen(EDGE_LIT, LINE, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
    p.drawLine(at(0, LINE / 2), at(length, LINE / 2))

    # The stretch so far (or from the first point to the pointer).
    pts = sorted(st.points)
    if len(pts) == 2 or (len(pts) == 1 and st.pointer is not None and not st.waiting):
        a = pts[0]
        b = pts[1] if len(pts) == 2 else (st.pointer.y() if vertical else st.pointer.x())
        colour = QColor(DONE)
        if len(pts) == 1:
            colour.setAlphaF(0.55)
        p.setPen(QPen(colour, LINE * 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(at(a, DOT + 6), at(b, DOT + 6))

    # Guide from the pointer to the edge, with a ghost of the next point.
    if st.pointer is not None and len(pts) < 2 and not st.waiting:
        t = st.pointer.y() if vertical else st.pointer.x()
        p.setPen(QPen(QColor(255, 255, 255, 170), 2, Qt.PenStyle.DashLine))
        p.drawLine(QLineF(st.pointer, at(t, DOT + 6)))
        p.setPen(QPen(QColor("white"), 3))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(at(t, DOT + 6), DOT + 4, DOT + 4)

    # Clicked points, numbered, with what they mean.
    small = _font(15, QFont.Weight.DemiBold)
    for i, t in enumerate(st.points):
        c = at(t, DOT + 6)
        p.setPen(QPen(QColor("white"), 3))
        p.setBrush(DONE)
        p.drawEllipse(c, DOT, DOT)
        p.setFont(small)
        p.setPen(QColor("white"))
        p.drawText(QRectF(c.x() - DOT, c.y() - DOT, DOT * 2, DOT * 2), Qt.AlignmentFlag.AlignCenter, str(i + 1))
        tag = "starts" if i == 0 else "ends"
        tag_at = at(t, 2 * DOT + 76)
        p.drawText(QRectF(tag_at.x() - 50, tag_at.y() - 12, 100, 24), Qt.AlignmentFlag.AlignCenter, tag)

    # The card with what to do.
    if st.waiting:
        title, sub, step = st.waiting, "", ""
    elif len(pts) == 0:
        title = f"Click where {st.other} starts along this edge"
        sub = "Then where it ends. Only the position along the edge counts, so anywhere near it will do."
        step = "Point 1 of 2"
    elif len(pts) == 1:
        title = f"Now click where {st.other} ends"
        sub = "The green stretch is where the pointer will cross."
        step = "Point 2 of 2"
    else:
        title = "Done on this screen"
        sub = f"Move the pointer across to {st.other} and do the same there."
        step = ""
    big, body, mono = _font(34, QFont.Weight.DemiBold), _font(19), _font(16, QFont.Weight.Medium)
    width = min(760.0, rect.width() * 0.62)
    cx = rect.center().x() - (GLOW / 2 if st.side == "right" else -GLOW / 2 if st.side == "left" else 0)
    top = rect.center().y() - 90
    left = cx - width / 2
    flags = Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap
    y = top
    if step:
        p.setFont(mono)
        p.setPen(EDGE_LIT)
        p.drawText(QRectF(left, y, width, 30), Qt.AlignmentFlag.AlignLeft, step.upper())
        y += 38
    p.setFont(big)
    p.setPen(QColor("white"))
    r = QFontMetricsF(big).boundingRect(QRectF(left, y, width, 400), flags, title)
    p.drawText(r, flags, title)
    y = r.bottom() + 14
    if sub:
        p.setFont(body)
        p.setPen(QColor(200, 208, 220))
        r = QFontMetricsF(body).boundingRect(QRectF(left, y, width, 400), flags, sub)
        p.drawText(r, flags, sub)
        y = r.bottom() + 28
    if not st.waiting:
        p.setFont(mono)
        p.setPen(QColor(154, 165, 182))
        p.drawText(QRectF(left, y, width, 30), Qt.AlignmentFlag.AlignLeft, "Esc cancels  ·  Backspace undoes a point")


class EdgeWindow(QWidget):
    """The calibration layer on one monitor. ``clickable``: it takes the
    clicks itself (this computer's own screen); otherwise it's click-through
    and the host catches the clicks. Either way the guide follows the pointer."""

    clicked = Signal(float)   # along the edge, in the window's pixels
    key = Signal(str)         # "esc" or "backspace"

    def __init__(self, qscreen, state: EdgeState, clickable: bool = True):
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
        if not clickable:
            flags |= Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.WindowTransparentForInput
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        if not clickable:
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setMouseTracking(True)
        self.clickable = clickable
        if qscreen is not None:
            self.setGeometry(qscreen.geometry())
        self.state = state
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._follow)
        self._timer.start(30)

    def set_state(self, state: EdgeState) -> None:
        state.pointer = self.state.pointer
        self.state = state
        self.update()

    def _follow(self) -> None:
        pos = QPointF(self.mapFromGlobal(QCursor.pos()))
        inside = QRectF(self.rect()).contains(pos)
        pointer = pos if inside else None
        if pointer != self.state.pointer:
            self.state.pointer = pointer
            self.update()

    def along(self, pos: QPointF) -> float:
        return pos.y() if self.state.side in ("left", "right") else pos.x()

    def mousePressEvent(self, e) -> None:  # noqa: N802
        if self.clickable and e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.along(e.position()))

    def keyPressEvent(self, e) -> None:  # noqa: N802
        if e.key() == Qt.Key.Key_Escape:
            self.key.emit("esc")
        elif e.key() == Qt.Key.Key_Backspace:
            self.key.emit("backspace")

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        paint_edge(p, QRectF(self.rect()), self.state)
        p.end()


def _scale(qscreen, entry: dict, side: str) -> float:
    """Window pixels per layout pixel, along the edge."""
    g = qscreen.geometry()
    size = entry.get("size") or [g.width(), g.height()]
    try:
        n = float(size[1] if side in ("left", "right") else size[0])
    except (TypeError, ValueError, IndexError):
        return 1.0
    return (g.height() if side in ("left", "right") else g.width()) / n if n > 0 else 1.0


def state_from(qscreen, entry: dict) -> EdgeState | None:
    """The :class:`EdgeState` a hint entry's ``edge`` asks for, scaled from
    layout pixels to the window's."""
    edge = entry.get("edge")
    if not isinstance(edge, dict) or edge.get("side") not in ("left", "right", "top", "bottom"):
        return None
    k = _scale(qscreen, entry, edge["side"])
    points = [float(v) * k for v in edge.get("points", []) if isinstance(v, (int, float))][:2]
    return EdgeState(edge["side"], str(edge.get("other", "the other screen")), points,
                     waiting=str(edge.get("waiting", "")))


class EdgeCalibration(QObject):
    """Calibrating the edge between two screens of different devices.

    ``ends``: the two sides, each ``{"device", "name", "screen": {id, os_x,
    os_y, width, height}, "side"}``, the one to click first first.
    ``send``: a command for the running service (``pick on``, ``hint ...``).
    ``finished`` gives ``(a_span, b_span)`` (layout pixels along each edge),
    or ``None`` if cancelled."""

    finished = Signal(object)

    def __init__(self, ends: list[dict], local_device: str, send):
        super().__init__()
        self.ends = ends
        self.local = local_device
        self.send = send
        self.points: list[list[float]] = [[], []]
        self.active = 0
        self.windows: dict[int, EdgeWindow] = {}
        self._remote = False

    # -- what each screen shows ------------------------------------------------
    def entry(self, i: int) -> dict:
        end, other = self.ends[i], self.ends[1 - i]
        s = end["screen"]
        waiting = ""
        if i > self.active:
            waiting = f"Finish on {other['name']} first, then come across"
        edge = {"side": end["side"], "other": other["name"], "points": list(self.points[i])}
        if waiting:
            edge["waiting"] = waiting
        return {"screen": s["id"], "os": [s.get("os_x", 0), s.get("os_y", 0), s["width"], s["height"]],
                "size": [s["width"], s["height"]], "edge": edge}

    def start(self) -> bool:
        if any(e["device"] != self.local for e in self.ends):
            self._remote = True
            self.send("pick on")
        if not self.render():
            self.stop()
            return False
        return True

    def render(self) -> bool:
        from curslink.gui.overlay import find_qscreen

        remote: dict[str, dict] = {}
        for i, end in enumerate(self.ends):
            entry = self.entry(i)
            if end["device"] != self.local:
                remote.setdefault(end["device"], {"screens": []})["screens"].append(entry)
                continue
            window = self.windows.get(i)
            if window is None:
                qscreen = find_qscreen(entry["screen"], entry["os"])
                if qscreen is None:
                    return False
                window = EdgeWindow(qscreen, state_from(qscreen, entry), clickable=True)
                window.clicked.connect(lambda t, i=i: self.local_click(i, t))
                window.key.connect(self.on_key)
                self.windows[i] = window
                window.show()
                window.raise_()
                window.activateWindow()
            else:
                window.set_state(state_from(window.screen(), entry))
        if remote:
            self.send("hint " + json.dumps({"devices": remote}, separators=(",", ":")))
        return True

    # -- clicks ------------------------------------------------------------------
    def local_click(self, i: int, t: float) -> None:
        window = self.windows.get(i)
        if i != self.active or window is None:
            return
        self.add(t / _scale(window.screen(), self.entry(i), self.ends[i]["side"]))

    def on_event(self, event: dict) -> None:
        """A ``picked`` event from the service: a click (or Esc, Backspace)
        on another computer."""
        if event.get("event") != "picked":
            return
        if event.get("key"):
            self.on_key(event["key"])
            return
        end = self.ends[self.active]
        if event.get("device") != end["device"] or event.get("screen") != end["screen"]["id"]:
            return
        try:
            t = float(event["y"] if end["side"] in ("left", "right") else event["x"])
        except (KeyError, TypeError, ValueError):
            return
        self.add(t)

    def add(self, t: float) -> None:
        end = self.ends[self.active]
        n = end["screen"]["height"] if end["side"] in ("left", "right") else end["screen"]["width"]
        self.points[self.active].append(max(0.0, min(float(n), t)))
        if len(self.points[self.active]) == 2:
            if self.active == 1:
                spans = tuple(tuple(sorted(p)) for p in self.points)
                self.stop()
                self.finished.emit(spans)
                return
            self.active = 1
        self.render()

    def on_key(self, key: str) -> None:
        if key == "esc":
            self.stop()
            self.finished.emit(None)
        elif key == "backspace":
            if not self.points[self.active] and self.active == 1:
                self.active = 0
            if self.points[self.active]:
                self.points[self.active].pop()
            self.render()

    def stop(self) -> None:
        for window in self.windows.values():
            window.hide()
            window.deleteLater()
        self.windows = {}
        if self._remote:
            self.send("pick off")
            self.send('hint {"devices":{}}')
            self._remote = False


class RemoteEdges:
    """On a paired computer: the edge layers the host's calibration asks for
    (through ``hint`` entries with an ``edge``). Click-through: the host
    catches the clicks."""

    def __init__(self):
        self.windows: dict[str, EdgeWindow] = {}

    def show(self, entries: list[dict]) -> None:
        from curslink.gui.overlay import find_qscreen

        wanted = {}
        for entry in entries:
            qscreen = find_qscreen(entry["screen"], entry.get("os"))
            state = state_from(qscreen, entry) if qscreen is not None else None
            if state is None:
                continue
            key = qscreen.name() or entry["screen"]
            wanted[key] = state
            window = self.windows.get(key)
            if window is None:
                window = EdgeWindow(qscreen, state, clickable=False)
                self.windows[key] = window
                window.show()
                window.raise_()
            else:
                window.set_state(state)
        for key in list(self.windows):
            if key not in wanted:
                self.windows.pop(key).deleteLater()

    def hide(self) -> None:
        for window in self.windows.values():
            window.hide()
            window.deleteLater()
        self.windows = {}
