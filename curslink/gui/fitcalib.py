"""Line up an overlap: where a screen in front really stands on the one behind.

Screens are laid out in their own pixels, which aren't the same size on
every monitor, so the layout only roughly says which part of a screen
another one covers. Lining up takes one click on each of the two screens,
each dimmed in turn with a card saying what to do:

1. On the screen behind: where a corner of the screen in front sits on it
   (one that's on it, such as a laptop's top corner against the monitor).
2. On the screen in front: where a corner of the screen behind sits on it,
   hidden behind it (follow the two edges of the screen behind to where
   they'd meet).

Two corners from opposite sides of the covered area tie the screens
together both ways: how big one's pixels are against the other's, and
where it stands (a :class:`curslink.layout.Fit`, which mirror mode uses).
Which corner is asked for comes from the layout; Tab picks the next one, in
case the guess isn't on the screen.

Drawn the same way on the host (where it takes the clicks itself) and on a
paired computer (where the host catches the clicks and sends the state in a
``hint`` frame, so the window there is click-through), like
:mod:`curslink.gui.edgecalib`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import QWidget

from curslink.gui.edgecalib import DONE, DOT, EDGE_LIT, TINT, _font

CORNERS = ("top-left", "top-right", "bottom-right", "bottom-left")
MIN_SPAN = 24  # layout px between the two corners, at least, along a side


def corner_at(name: str, w: float, h: float) -> tuple[float, float]:
    return (w if name.endswith("right") else 0.0, h if name.startswith("bottom") else 0.0)


@dataclass
class FitState:
    """What one of the two screens shows. ``point`` and ``pointer`` are in
    the window's own pixels."""
    role: str                     # "behind" (click the front one's corner) or "front"
    other: str                    # the other screen, as named to the user
    corner: str                   # which of the other screen's corners to click
    point: QPointF | None = None  # clicked
    pointer: QPointF | None = None
    waiting: str = ""             # set when it's the other screen's turn
    step: int = 1


def paint_fit(p: QPainter, rect: QRectF, st: FitState) -> None:
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.fillRect(rect, TINT)
    vertical, horizontal = st.corner.split("-")

    # The pointer: cross hairs, and the corner's two edges drawn from it so
    # they can be laid along the real ones.
    if st.pointer is not None and st.point is None and not st.waiting:
        c = st.pointer
        p.setPen(QPen(QColor(255, 255, 255, 110), 1.5, Qt.PenStyle.DashLine))
        p.drawLine(QPointF(rect.left(), c.y()), QPointF(rect.right(), c.y()))
        p.drawLine(QPointF(c.x(), rect.top()), QPointF(c.x(), rect.bottom()))
        p.setPen(QPen(EDGE_LIT, 5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        dx = 1 if horizontal == "left" else -1   # the other screen lies that way from its corner
        dy = 1 if vertical == "top" else -1
        arm = 160.0
        p.drawLine(c, QPointF(c.x() + dx * arm, c.y()))
        p.drawLine(c, QPointF(c.x(), c.y() + dy * arm))
        p.setPen(QPen(QColor("white"), 3))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(c, DOT, DOT)
    if st.point is not None:
        p.setPen(QPen(QColor("white"), 3))
        p.setBrush(DONE)
        p.drawEllipse(st.point, DOT, DOT)

    if st.waiting:
        step, title, sub = "", st.waiting, ""
    elif st.role == "behind":
        step = f"Step {st.step} of 2"
        title = f"Click where {st.other}'s {st.corner} corner is"
        sub = (f"Right where that corner of {st.other} stands in front of this screen. "
               "Lay the blue lines along its two edges.")
    else:
        step = f"Step {st.step} of 2"
        title = f"Click where {st.other}'s {st.corner} corner is, behind this screen"
        sub = (f"Follow {st.other}'s two edges past this screen's border to where they'd meet, "
               "and click there. The blue lines show the edges.")
    big, body, mono = _font(32, _font(1).Weight.DemiBold), _font(18), _font(15, _font(1).Weight.Medium)
    width = min(760.0, rect.width() * 0.7)
    left = rect.center().x() - width / 2
    # Keep the card away from the corner being looked for.
    y = rect.top() + rect.height() * (0.58 if vertical == "top" else 0.1)
    flags = Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap
    if step:
        p.setFont(mono)
        p.setPen(EDGE_LIT)
        p.drawText(QRectF(left, y, width, 30), Qt.AlignmentFlag.AlignLeft, step.upper())
        y += 36
    p.setFont(big)
    p.setPen(QColor("white"))
    r = QFontMetricsF(big).boundingRect(QRectF(left, y, width, 400), flags, title)
    p.drawText(r, flags, title)
    y = r.bottom() + 12
    if sub:
        p.setFont(body)
        p.setPen(QColor(200, 208, 220))
        r = QFontMetricsF(body).boundingRect(QRectF(left, y, width, 400), flags, sub)
        p.drawText(r, flags, sub)
        y = r.bottom() + 24
    if not st.waiting:
        p.setFont(mono)
        p.setPen(QColor(154, 165, 182))
        p.drawText(QRectF(left, y, width, 30), Qt.AlignmentFlag.AlignLeft,
                   "Esc cancels  ·  Backspace goes back  ·  Tab: another corner")


class FitWindow(QWidget):
    """The layer on one screen. ``clickable``: it takes the clicks itself
    (this computer's own screen); otherwise it's click-through and the host
    catches the clicks."""

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
    point = None
    pt = fit.get("point")
    if isinstance(pt, list) and len(pt) == 2:
        try:
            point = QPointF(float(pt[0]) * kx, float(pt[1]) * ky)
        except (TypeError, ValueError):
            point = None
    corner = fit.get("corner") if fit.get("corner") in CORNERS else "top-left"
    try:
        step = int(fit.get("step", 1))
    except (TypeError, ValueError):
        step = 1
    return FitState("front" if fit.get("role") == "front" else "behind", str(fit.get("other", "the other screen")),
                    corner, point, waiting=str(fit.get("waiting", "")), step=step)


def solve(front_corner: tuple[float, float], on_behind: tuple[float, float],
          behind_corner: tuple[float, float], on_front: tuple[float, float]) -> tuple[float, float, float] | None:
    """(x, y, scale) of the screen in front on the one behind (see
    :class:`curslink.layout.Fit`) from two corners: ``front_corner`` (in the
    front screen's pixels) was clicked at ``on_behind`` on the screen behind,
    and ``behind_corner`` (the screen behind's pixels) at ``on_front`` on the
    screen in front. Each side the two corners are apart along gives the
    scale; None if they aren't far enough apart along either."""
    (cx, cy), (bx, by) = front_corner, on_behind
    (dx, dy), (fx, fy) = behind_corner, on_front
    scales = []
    for c, f, b, d in ((cx, fx, bx, dx), (cy, fy, by, dy)):
        # x + k*c = b and d = x + k*f, so k = (b - d) / (c - f).
        if abs(c - f) >= MIN_SPAN:
            k = (b - d) / (c - f)
            if k > 0:
                scales.append(k)
    if not scales:
        return None
    k = sum(scales) / len(scales)
    x = ((bx - k * cx) + (dx - k * fx)) / 2
    y = ((by - k * cy) + (dy - k * fy)) / 2
    return (x, y, k)


def guess_corner(inner: tuple[float, float, float, float], outer: tuple[float, float, float, float]) -> str:
    """Which corner of rectangle ``inner`` (x, y, w, h, layout pixels) is
    most likely on ``outer``: the first one inside it, else the nearest."""
    x, y, w, h = inner
    ox, oy, ow, oh = outer
    best, best_d = "top-left", None
    for name in CORNERS:
        px, py = x + corner_at(name, w, h)[0], y + corner_at(name, w, h)[1]
        dx = max(ox - px, 0, px - (ox + ow))
        dy = max(oy - py, 0, py - (oy + oh))
        d = dx * dx + dy * dy
        if best_d is None or d < best_d:
            best, best_d = name, d
    return best


class FitCalibration(QObject):
    """Lining up where ``front`` stands on ``behind``.

    Each of ``behind`` and ``front`` is ``{"device", "name", "screen": {id,
    os_x, os_y, width, height}, "rect": [x, y, w, h]}`` (layout pixels;
    ``rect`` is where it is on the layout, for the corner guesses). ``send``:
    a command for the running service (``pick on``, ``hint ...``).
    ``finished`` gives ``(x, y, scale)`` (see curslink.layout.Fit), or
    ``None`` if cancelled."""

    finished = Signal(object)

    def __init__(self, behind: dict, front: dict, local_device: str, send):
        super().__init__()
        self.ends = [behind, front]
        self.local = local_device
        self.send = send
        rb, rf = behind.get("rect"), front.get("rect")
        if rb and rf:
            # Asked for on each screen: a corner of the other one.
            self.corners = [guess_corner(tuple(rf), tuple(rb)), guess_corner(tuple(rb), tuple(rf))]
        else:
            self.corners = ["top-right", "bottom-left"]
        self.points: list[tuple[float, float] | None] = [None, None]
        self.active = 0
        self.windows: dict[int, FitWindow] = {}
        self._remote = any(e["device"] != local_device for e in self.ends)

    def size(self, i: int) -> tuple[float, float]:
        s = self.ends[i]["screen"]
        return float(s["width"]), float(s["height"])

    def entry(self, i: int) -> dict:
        end, other = self.ends[i], self.ends[1 - i]
        s = end["screen"]
        fit = {"role": "behind" if i == 0 else "front", "other": other["name"], "corner": self.corners[i],
               "step": i + 1}
        if self.points[i] is not None:
            fit["point"] = list(self.points[i])
        if i != self.active:
            fit["waiting"] = (f"Next, on {other['name']}" if i > self.active
                              else f"Now on {other['name']}: one more click there")
        return {"screen": s["id"], "os": [s.get("os_x", 0), s.get("os_y", 0), s["width"], s["height"]],
                "size": [s["width"], s["height"]], "fit": fit}

    def start(self) -> bool:
        if self._remote:
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
                window = FitWindow(qscreen, state_from(qscreen, entry), clickable=True)
                window.clicked.connect(lambda pos, i=i: self.local_click(i, pos))
                window.key.connect(self.on_key)
                self.windows[i] = window
                window.show()
                window.raise_()
                window.activateWindow()
            else:
                window.set_state(state_from(window.screen(), entry))
        if remote or self._remote:
            self.send("hint " + json.dumps({"devices": remote}, separators=(",", ":")))
        return True

    def local_click(self, i: int, pos: QPointF) -> None:
        window = self.windows.get(i)
        if window is None or i != self.active:
            return
        kx, ky = _scales(window.screen(), self.entry(i))
        self.add(pos.x() / kx, pos.y() / ky)

    def on_event(self, event: dict) -> None:
        """A ``picked`` event from the service: a click (or a key) on another computer."""
        if event.get("event") != "picked":
            return
        if event.get("key"):
            self.on_key(event["key"])
            return
        end = self.ends[self.active]
        if event.get("device") != end["device"] or event.get("screen") != end["screen"]["id"]:
            return
        try:
            self.add(float(event["x"]), float(event["y"]))
        except (KeyError, TypeError, ValueError):
            return

    def add(self, x: float, y: float) -> None:
        w, h = self.size(self.active)
        self.points[self.active] = (max(0.0, min(w, x)), max(0.0, min(h, y)))
        if self.active == 0:
            self.active = 1
            self.render()
            return
        fit = self.result()
        if fit is None:
            self.points[1] = None  # the corners are too close together: try another
            self.render()
            return
        self.stop()
        self.finished.emit(fit)

    def result(self) -> tuple[float, float, float] | None:
        if self.points[0] is None or self.points[1] is None:
            return None
        fw, fh = self.size(1)
        bw, bh = self.size(0)
        return solve(corner_at(self.corners[0], fw, fh), self.points[0],
                     corner_at(self.corners[1], bw, bh), self.points[1])

    def on_key(self, key: str) -> None:
        if key == "esc":
            self.stop()
            self.finished.emit(None)
        elif key == "backspace":
            if self.points[self.active] is None and self.active == 1:
                self.active = 0
            self.points[self.active] = None
            self.render()
        elif key == "tab":
            i = self.active
            self.corners[i] = CORNERS[(CORNERS.index(self.corners[i]) + 1) % len(CORNERS)]
            self.points[i] = None
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
