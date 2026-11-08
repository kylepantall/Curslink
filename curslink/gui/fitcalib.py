"""Line up an overlap: show where a screen in front really stands on the one behind.

Screens are laid out in their own pixels, which aren't the same size on
every monitor, so the layout only roughly says which part of a screen
another one covers. Here, on the screen behind (dimmed, with a card saying
what to do), you click where the screen in front's top-left corner sits,
then its top-right corner. From the pointer a blue frame shows where the
screen in front would be, so you can see it lines up before the second
click. If its top edge isn't on the screen behind, Tab switches to its
bottom corners. That gives its real position and size there (a
:class:`curslink.layout.Fit`), which mirror mode uses.

Drawn the same way on the host (where it takes the clicks itself) and on a
paired computer (where the host catches the clicks and sends the state in a
``hint`` frame, so the window there is click-through), like
:mod:`curslink.gui.edgecalib`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import QWidget

from curslink.gui.edgecalib import DONE, DOT, EDGE_LIT, TINT, _font

MIN_SPAN = 24  # layout px between the two corners, at least


@dataclass
class FitState:
    """What the screen behind shows. ``points`` and ``pointer`` are in the
    window's own pixels."""
    front: str                    # the screen in front, as named to the user
    aspect: float                 # its width / height
    corners: str = "top"          # which of its corners: "top" or "bottom"
    points: list[QPointF] = field(default_factory=list)
    pointer: QPointF | None = None


def frame_for(st: FitState) -> QRectF | None:
    """Where the screen in front would be, from the first corner to the
    second (or to the pointer)."""
    if not st.points:
        return None
    a = st.points[0]
    b = st.points[1] if len(st.points) > 1 else st.pointer
    if b is None or abs(b.x() - a.x()) < 2:
        return None
    w = abs(b.x() - a.x())
    h = w / st.aspect if st.aspect > 0 else w
    y = (a.y() + b.y()) / 2
    top = y if st.corners == "top" else y - h
    return QRectF(min(a.x(), b.x()), top, w, h)


def paint_fit(p: QPainter, rect: QRectF, st: FitState) -> None:
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.fillRect(rect, TINT)
    frame = frame_for(st)
    if frame is not None:
        fill = QColor(EDGE_LIT)
        fill.setAlphaF(0.18 if len(st.points) < 2 else 0.28)
        p.fillRect(frame, fill)
        p.setPen(QPen(EDGE_LIT, 4))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(frame)
        p.setFont(_font(18, _font(1).Weight.DemiBold))
        p.setPen(QColor("white"))
        label_y = frame.top() + 10 if st.corners == "top" else frame.bottom() - 34
        p.drawText(QRectF(frame.left(), label_y, frame.width(), 24), Qt.AlignmentFlag.AlignCenter, st.front)

    # The pointer: cross hairs, so the corner can be placed exactly.
    if st.pointer is not None and len(st.points) < 2:
        p.setPen(QPen(QColor(255, 255, 255, 200), 1.5, Qt.PenStyle.DashLine))
        p.drawLine(QPointF(rect.left(), st.pointer.y()), QPointF(rect.right(), st.pointer.y()))
        p.drawLine(QPointF(st.pointer.x(), rect.top()), QPointF(st.pointer.x(), rect.bottom()))
        p.setPen(QPen(QColor("white"), 3))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(st.pointer, DOT, DOT)

    side = "top" if st.corners == "top" else "bottom"
    names = [f"{side}-left", f"{side}-right"]
    small = _font(15, _font(1).Weight.DemiBold)
    for i, c in enumerate(st.points):
        p.setPen(QPen(QColor("white"), 3))
        p.setBrush(DONE)
        p.drawEllipse(c, DOT, DOT)
        p.setFont(small)
        p.setPen(QColor("white"))
        p.drawText(QRectF(c.x() - DOT, c.y() - DOT, DOT * 2, DOT * 2), Qt.AlignmentFlag.AlignCenter, str(i + 1))
        p.drawText(QRectF(c.x() - 80, c.y() + DOT + 4, 160, 22), Qt.AlignmentFlag.AlignCenter, names[i])

    # The card with what to do: away from where the screen in front stands.
    if not st.points:
        step = "Corner 1 of 2"
        title = f"Click where {st.front}'s {names[0]} corner is"
        sub = (f"On this screen, right where the corner of {st.front} sits in front of it. "
               "Line the cross hairs up with the corner.")
    else:
        step = "Corner 2 of 2"
        title = f"Now its {names[1]} corner"
        sub = f"The blue frame shows where {st.front} will be. Line it up with the real screen, then click."
    big, body, mono = _font(32, _font(1).Weight.DemiBold), _font(18), _font(15, _font(1).Weight.Medium)
    width = min(760.0, rect.width() * 0.62)
    left = rect.center().x() - width / 2
    y = rect.top() + rect.height() * (0.08 if st.corners == "bottom" else 0.55)
    if frame is not None and st.corners == "top" and frame.top() < rect.height() * 0.5:
        y = rect.top() + rect.height() * 0.08
    flags = Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap
    p.setFont(mono)
    p.setPen(EDGE_LIT)
    p.drawText(QRectF(left, y, width, 30), Qt.AlignmentFlag.AlignLeft, step.upper())
    y += 36
    p.setFont(big)
    p.setPen(QColor("white"))
    r = QFontMetricsF(big).boundingRect(QRectF(left, y, width, 400), flags, title)
    p.drawText(r, flags, title)
    y = r.bottom() + 12
    p.setFont(body)
    p.setPen(QColor(200, 208, 220))
    r = QFontMetricsF(body).boundingRect(QRectF(left, y, width, 400), flags, sub)
    p.drawText(r, flags, sub)
    y = r.bottom() + 24
    p.setFont(mono)
    p.setPen(QColor(154, 165, 182))
    other = "bottom" if st.corners == "top" else "top"
    p.drawText(QRectF(left, y, width, 30), Qt.AlignmentFlag.AlignLeft,
               f"Esc cancels  ·  Backspace undoes  ·  Tab uses its {other} corners")


class FitWindow(QWidget):
    """The layer on the screen behind. ``clickable``: it takes the clicks
    itself (this computer's own screen); otherwise it's click-through and
    the host catches the clicks."""

    clicked = Signal(QPointF)  # in the window's pixels
    key = Signal(str)          # "esc", "backspace" or "tab"

    def __init__(self, qscreen, state: FitState, clickable: bool = True):
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

    def set_state(self, state: FitState) -> None:
        state.pointer = self.state.pointer
        self.state = state
        self.update()

    def _follow(self) -> None:
        pos = QPointF(self.mapFromGlobal(QCursor.pos()))
        pointer = pos if QRectF(self.rect()).contains(pos) else None
        if pointer != self.state.pointer:
            self.state.pointer = pointer
            self.update()

    def mousePressEvent(self, e) -> None:  # noqa: N802
        if self.clickable and e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(QPointF(e.position()))

    def focusNextPrevChild(self, _next: bool) -> bool:  # noqa: N802 - let Tab reach keyPressEvent
        return False

    def keyPressEvent(self, e) -> None:  # noqa: N802
        keys = {Qt.Key.Key_Escape: "esc", Qt.Key.Key_Backspace: "backspace", Qt.Key.Key_Tab: "tab"}
        if e.key() in keys:
            self.key.emit(keys[e.key()])

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        paint_fit(p, QRectF(self.rect()), self.state)
        p.end()


def _scales(qscreen, entry: dict) -> tuple[float, float]:
    """Window pixels per layout pixel, across and down."""
    g = qscreen.geometry()
    size = entry.get("size") or [g.width(), g.height()]
    try:
        w, h = float(size[0]), float(size[1])
    except (TypeError, ValueError, IndexError):
        return 1.0, 1.0
    return (g.width() / w if w > 0 else 1.0, g.height() / h if h > 0 else 1.0)


def state_from(qscreen, entry: dict) -> FitState | None:
    """The :class:`FitState` a hint entry's ``fit`` asks for, scaled from
    layout pixels to the window's."""
    fit = entry.get("fit")
    if not isinstance(fit, dict):
        return None
    kx, ky = _scales(qscreen, entry)
    points = []
    for pt in fit.get("points", [])[:2]:
        try:
            points.append(QPointF(float(pt[0]) * kx, float(pt[1]) * ky))
        except (TypeError, ValueError, IndexError):
            continue
    try:
        aspect = float(fit.get("aspect", 16 / 9))
    except (TypeError, ValueError):
        aspect = 16 / 9
    corners = "bottom" if fit.get("corners") == "bottom" else "top"
    return FitState(str(fit.get("front", "the screen in front")), aspect, corners, points)


def solve(points: list[tuple[float, float]], corners: str, front_size: tuple[float, float]) -> tuple[float, float, float] | None:
    """(x, y, scale) of the screen in front on the one behind, from its two
    clicked corners there (layout pixels of the screen behind)."""
    if len(points) != 2:
        return None
    (x1, y1), (x2, y2) = sorted(points)
    fw, fh = front_size
    if x2 - x1 < MIN_SPAN or fw <= 0 or fh <= 0:
        return None
    scale = (x2 - x1) / fw
    y = (y1 + y2) / 2
    return (x1, y if corners == "top" else y - fh * scale, scale)


class FitCalibration(QObject):
    """Lining up where ``front`` stands on ``behind``.

    ``behind``: ``{"device", "name", "screen": {id, os_x, os_y, width, height}}``;
    ``front``: ``{"name", "width", "height"}`` (layout pixels). ``send``: a
    command for the running service (``pick on``, ``hint ...``).
    ``finished`` gives ``(x, y, scale)`` (see curslink.layout.Fit), or
    ``None`` if cancelled."""

    finished = Signal(object)

    def __init__(self, behind: dict, front: dict, local_device: str, send):
        super().__init__()
        self.behind = behind
        self.front = front
        self.local = local_device
        self.send = send
        self.points: list[tuple[float, float]] = []
        self.corners = "top"
        self.window: FitWindow | None = None
        self._remote = behind["device"] != local_device

    def entry(self) -> dict:
        s = self.behind["screen"]
        fw, fh = self.front["width"], self.front["height"]
        return {"screen": s["id"], "os": [s.get("os_x", 0), s.get("os_y", 0), s["width"], s["height"]],
                "size": [s["width"], s["height"]],
                "fit": {"front": self.front["name"], "aspect": fw / fh if fh else 16 / 9,
                        "corners": self.corners, "points": [list(p) for p in self.points]}}

    def start(self) -> bool:
        if self._remote:
            self.send("pick on")
        if not self.render():
            self.stop()
            return False
        return True

    def render(self) -> bool:
        from curslink.gui.overlay import find_qscreen

        entry = self.entry()
        if self._remote:
            self.send("hint " + json.dumps({"devices": {self.behind["device"]: {"screens": [entry]}}},
                                           separators=(",", ":")))
            return True
        if self.window is None:
            qscreen = find_qscreen(entry["screen"], entry["os"])
            if qscreen is None:
                return False
            self.window = FitWindow(qscreen, state_from(qscreen, entry), clickable=True)
            self.window.clicked.connect(self.local_click)
            self.window.key.connect(self.on_key)
            self.window.show()
            self.window.raise_()
            self.window.activateWindow()
        else:
            self.window.set_state(state_from(self.window.screen(), entry))
        return True

    def local_click(self, pos: QPointF) -> None:
        if self.window is None:
            return
        kx, ky = _scales(self.window.screen(), self.entry())
        self.add(pos.x() / kx, pos.y() / ky)

    def on_event(self, event: dict) -> None:
        """A ``picked`` event from the service: a click (or a key) on another computer."""
        if event.get("event") != "picked":
            return
        if event.get("key"):
            self.on_key(event["key"])
            return
        if event.get("device") != self.behind["device"] or event.get("screen") != self.behind["screen"]["id"]:
            return
        try:
            self.add(float(event["x"]), float(event["y"]))
        except (KeyError, TypeError, ValueError):
            return

    def add(self, x: float, y: float) -> None:
        s = self.behind["screen"]
        self.points.append((max(0.0, min(float(s["width"]), x)), max(0.0, min(float(s["height"]), y))))
        if len(self.points) == 2:
            fit = solve(self.points, self.corners, (self.front["width"], self.front["height"]))
            if fit is None:
                self.points.pop()  # too close together: click the second corner again
            else:
                self.stop()
                self.finished.emit(fit)
                return
        self.render()

    def on_key(self, key: str) -> None:
        if key == "esc":
            self.stop()
            self.finished.emit(None)
        elif key == "backspace":
            if self.points:
                self.points.pop()
            self.render()
        elif key == "tab":
            self.corners = "bottom" if self.corners == "top" else "top"
            self.points = []
            self.render()

    def stop(self) -> None:
        if self.window is not None:
            self.window.hide()
            self.window.deleteLater()
            self.window = None
        if self._remote:
            self.send("pick off")
            self.send('hint {"devices":{}}')
            self._remote = False
