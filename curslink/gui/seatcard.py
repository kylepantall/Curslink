"""The "Where you sit" card on the Layout page (curslink.seat): how far back
you are, your eye level against the main screen, what the screen in front
hides, how far the overlap moved when it was aimed at your eyes, and what
moving something would change. Distances only, no angles."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget

from curslink import seat
from curslink.gui import theme
from curslink.gui.widgets import Card, button, label


class SideView(QWidget):
    """The desk from the side: the main screen, the screen in front of it,
    your eyes, and what you see over the front screen's top edge from there
    (solid) and from where the phone was held for the scan (dashed)."""

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(120)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName("Your desk from the side")
        self.shape: dict | None = None

    def show_desk(self, desk: dict, fits: list[dict], main: str) -> None:
        """Everything in the main screen's terms: (how far before it, how far down it), mm."""
        mm = seat.mm_of(desk)
        s = desk["screens"][main]
        k = s["scale"] * mm

        def side(point) -> tuple[float, float]:
            _u, v, before = seat.local(s, point)
            return before * k, v * k

        shape = {"main": (side(seat.at(s, s["width"] / 2, 0.0)), side(seat.at(s, s["width"] / 2, s["height"]))),
                 "eye": side(desk["eye"]), "scan": None, "front": None}
        if desk.get("how") != "scan" and isinstance(desk.get("scan_eye"), list):
            shape["scan"] = side(desk["scan_eye"])
        for f in fits:
            front = desk["screens"].get(f.get("front"))
            if f.get("behind") == main and front is not None:
                shape["front"] = (side(seat.at(front, front["width"] / 2, 0.0)),
                                  side(seat.at(front, front["width"] / 2, front["height"])))
                break
        self.shape = shape
        self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802
        if self.shape is None:
            return
        sh = self.shape
        pts = [*sh["main"], sh["eye"]] + ([sh["scan"]] if sh["scan"] else []) + (list(sh["front"]) if sh["front"] else [])
        far = max(p[0] for p in pts)
        top, bottom = min(p[1] for p in pts), max(p[1] for p in pts)
        pad = 14.0
        k = min((self.width() - 2 * pad) / max(1.0, far), (self.height() - 2 * pad) / max(1.0, bottom - top))
        left = (self.width() - far * k) / 2

        def at(p) -> QPointF:  # you on the left, the screens on the right
            return QPointF(left + (far - p[0]) * k, pad + (p[1] - top) * k)

        t = theme.current().tokens
        ink, soft, accent = theme.qcolor(t["ink"]), theme.qcolor(t["ink3"]), theme.qcolor(t["accent"])
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # what's seen over the front screen's top edge, carried on to the main screen
        if sh["front"]:
            edge = min(sh["front"], key=lambda q: q[1])
            for eye, pen in ((sh["scan"], QPen(soft, 1.2, Qt.PenStyle.DashLine)), (sh["eye"], QPen(accent, 1.6))):
                if eye is None or eye[0] - edge[0] <= 1e-6:
                    continue
                down = eye[1] + (edge[1] - eye[1]) * eye[0] / (eye[0] - edge[0])
                p.setPen(pen)
                p.drawLine(at(eye), at((0.0, down)))
        p.setPen(QPen(ink, 4.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(at(sh["main"][0]), at(sh["main"][1]))
        if sh["front"]:
            p.setPen(QPen(ink, 3.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(at(sh["front"][0]), at(sh["front"][1]))
        if sh["scan"]:
            p.setPen(QPen(soft, 1.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(at(sh["scan"]), 4.0, 4.0)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(accent)
        p.drawEllipse(at(sh["eye"]), 5.0, 5.0)
        p.end()


class SeatCard(Card):
    """``measure``: line up again (the phone step finds the eyes). ``find``:
    find them with the computer's camera. ``reset``: back to where the phone
    was held for the scan."""

    measure = Signal()
    find = Signal()
    reset = Signal()

    def __init__(self):
        super().__init__("Where you sit")
        self.source = label("", "small", wrap=True)
        self.body.addWidget(self.source)
        self.side = SideView()
        self.body.addWidget(self.side)
        self.caption = label("From the side: you on the left. The solid line is what you see over the front screen's "
                             "top edge from your seat; the dashed one, from where the phone was held for the scan.",
                             "small", wrap=True)
        self.body.addWidget(self.caption)
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(10)
        self.tiles: dict[str, tuple[QWidget, object, object]] = {}
        for i, (key, title) in enumerate((("distance", "Distance"), ("eye", "Eye level"),
                                          ("hidden", "Behind the screen in front"), ("moved", "Overlap re-aimed"))):
            w = QWidget()
            v = QVBoxLayout(w)
            v.setContentsMargins(0, 0, 0, 0)
            v.setSpacing(1)
            v.addWidget(label(title, "small"))
            value = label("", "h2")
            detail = label("", "small", wrap=True)
            v.addWidget(value)
            v.addWidget(detail)
            grid.addWidget(w, i // 2, i % 2)
            self.tiles[key] = (w, value, detail)
        self.body.addLayout(grid)
        self.tips_title = label("Suggestions", "small")
        self.body.addWidget(self.tips_title)
        self.tips = QVBoxLayout()
        self.tips.setSpacing(4)
        self.body.addLayout(self.tips)
        row = QHBoxLayout()
        self.measure_button = button("Measure again", None, self.measure.emit)
        self.measure_button.setToolTip("Line up with the camera again: its last step finds your eyes with the phone.")
        self.find_button = button("Find with the camera…", None, self.find.emit)
        self.find_button.setToolTip("No phone: look at this computer's camera, then at each screen.")
        self.reset_button = button("Aim from the scan again", None, self.reset.emit)
        self.reset_button.setToolTip("Aim the overlaps from where the phone was held for the scan, as before.")
        for b in (self.measure_button, self.find_button, self.reset_button):
            row.addWidget(b)
        row.addStretch(1)
        self.body.addLayout(row)
        self.body.addWidget(label("Moved a monitor? Line up again; it takes about 10 seconds.", "small", wrap=True))
        self.summary: dict | None = None
        self.hide()

    def show_desk(self, desk: dict | None, fits: list[dict], names: dict[str, str], tips: bool = True) -> None:
        from curslink.gui.widgets import clear_layout

        self.summary = info = seat.summary(desk, fits, names) if desk is not None else None
        self.setVisible(info is not None)
        if info is None:
            return
        how = desk.get("how")
        when = str(desk.get("when") or "").replace("T", " at ")[:19]
        self.source.setText(
            {"phone": "Measured with your phone at your eyes", "camera": "Found with this computer's camera (rougher "
             "than the phone)"}.get(how, "Not measured yet: this is where the phone was held for the scan, which is "
                                         "usually nearer than you sit. Line up again and do its last step, or find "
                                         "your seat with the camera") + (f", {when}." if when else "."))
        self.side.show_desk(desk, fits, info["main"])
        self.caption.setVisible(self.side.shape is not None and self.side.shape["front"] is not None)
        for key, (w, value, detail) in self.tiles.items():
            pair = info.get(key)
            w.setVisible(pair is not None)
            if pair is not None:
                value.setText(pair[0])
                detail.setText(pair[1])
        clear_layout(self.tips)
        said = info["tips"] if tips else []
        for tip in said or (["Nothing to change: you're well placed."] if tips else []):
            self.tips.addWidget(label(tip, None, wrap=True))
        self.tips_title.setVisible(tips)
        self.reset_button.setVisible(how != "scan")
