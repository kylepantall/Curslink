"""Layout: drag the screens of every device into place, like the real desk.

Screens can't overlap unless "Allow overlap" is on (a laptop standing in
front of a monitor's corner). While you drag, the area two screens share is
striped: red when overlap isn't allowed (and Save stays off), grey on the
screen behind when it is, since that part of it is blocked out.
"""

from __future__ import annotations

import json
import sys

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetricsF, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QBoxLayout,
    QGraphicsItem,
    QLabel,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QGridLayout,
    QHBoxLayout,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from curslink.configedit import snap, touching_edges
from curslink.gui import theme
from curslink.gui.widgets import Card, Page, Toggle, button, label

SCALE = 0.1  # scene units per layout pixel
CLICK_SLOP_PX = 3  # a press and release this close on screen is a click, not a drag
SNAP_VIEW_PX = 8  # a dragged screen snaps to edges this close, in on-screen pixels


def overlap_of(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> tuple[int, int, int, int] | None:
    """The area two ``(x, y, w, h)`` rects share, or None."""
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    return (x0, y0, x1 - x0, y1 - y0) if x0 < x1 and y0 < y1 else None


def push_apart(rect: tuple[int, int, int, int], others: list[tuple[int, int, int, int]]) -> tuple[int, int]:
    """Move ``rect`` the shortest way out of every screen it overlaps, so it
    ends up touching instead (overlap isn't allowed)."""
    x, y, w, h = rect
    for _ in range(8):
        hit = next((o for o in others if overlap_of((x, y, w, h), o)), None)
        if hit is None:
            break
        ox, oy, ow, oh = hit
        moves = [(ox - (x + w), 0), (ox + ow - x, 0), (0, oy - (y + h)), (0, oy + oh - y)]
        dx, dy = min(moves, key=lambda m: abs(m[0]) + abs(m[1]))
        x, y = x + dx, y + dy
    return x, y


def _colours(home: bool, selected: bool) -> tuple[QColor, QPen, QColor]:
    """Fill, border and text for a screen, from the theme (spec 4.3)."""
    t = theme.current().tokens
    if home:
        fill = theme.over(t["accent_soft"], t["card"])
        border = QColor(t["ink3"])
    else:
        fill = QColor(theme.OTHER_DEVICE)
        fill.setAlphaF(0.16)
        border = QColor(theme.OTHER_DEVICE)
        border.setAlphaF(0.7)
    pen = QPen(theme.qcolor(t["accent"]), 3) if selected else QPen(border, 1.2)
    return fill, pen, theme.qcolor(t["ink"])


def detect_screens() -> list[dict]:
    """This computer's monitors in its OS coordinates, as Curslink uses them.

    macOS works in points; Windows and X11 in physical pixels, which is what
    Qt's logical geometry times the device pixel ratio gives (exact for one
    scale factor, close enough to adjust by hand when monitors mix them).
    """
    out = []
    for i, screen in enumerate(QGuiApplication.screens()):
        g = screen.geometry()
        ratio = 1.0 if sys.platform == "darwin" else screen.devicePixelRatio()
        out.append({
            "id": "main" if screen == QGuiApplication.primaryScreen() else f"screen{i + 1}",
            "x": round(g.x() * ratio), "y": round(g.y() * ratio),
            "width": round(g.width() * ratio), "height": round(g.height() * ratio),
        })
    return out


class ScreenItem(QGraphicsRectItem):
    def __init__(self, page, device_id: str, device_name: str, screen: dict, home: bool):
        super().__init__(0, 0, screen["width"] * SCALE, screen["height"] * SCALE)
        self.page = page
        self.device_id, self.device_name, self.home = device_id, device_name, home
        self.screen = dict(screen)
        self.setPos(screen["x"] * SCALE, screen["y"] * SCALE)
        self.setZValue(int(screen.get("z", 0)))
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable
                      | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
                      | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.text = QGraphicsSimpleTextItem(self)
        # Constant on-screen size, however the view is zoomed.
        self.text.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
        f = QFont()
        f.setPointSizeF(9.5)
        f.setWeight(QFont.Weight.DemiBold)
        self.text.setFont(f)
        self.refresh()

    @property
    def key(self) -> str:
        return f"{self.device_id}/{self.screen['id']}"

    def layout_rect(self) -> tuple[int, int, int, int]:
        return (round(self.pos().x() / SCALE), round(self.pos().y() / SCALE),
                int(self.screen["width"]), int(self.screen["height"]))

    def refresh(self) -> None:
        w, h = self.screen["width"] * SCALE, self.screen["height"] * SCALE
        self.setRect(0, 0, w, h)
        fill, pen, ink = _colours(self.home, self.isSelected())
        self.setBrush(QBrush(fill))
        self.setPen(pen)
        self.text.setText(f"{self.device_name}\n{self.screen['id']} · {self.screen['width']}×{self.screen['height']}")
        self.text.setBrush(ink)
        self.text.setPos(w * 0.05, h * 0.08)

    def paint(self, painter, option, widget=None) -> None:
        # Our own border shows the selection; skip Qt's dotted one.
        from PySide6.QtWidgets import QStyle

        option.state &= ~QStyle.StateFlag.State_Selected
        super().paint(painter, option, widget)
        # Stripe what this screen shares with others: red where that isn't
        # allowed, grey where another screen stands in front of this one.
        mine = self.layout_rect()
        allowed = self.page.allow_overlap
        t = theme.current().tokens
        for other in self.page.items:
            if other is self:
                continue
            shared = overlap_of(mine, other.layout_rect())
            if shared is None or (allowed and not self.page.in_front(other, self)):
                continue
            colour = theme.qcolor(t["bad"] if not allowed else t["ink3"])
            rect = QRectF((shared[0] - mine[0]) * SCALE, (shared[1] - mine[1]) * SCALE,
                          shared[2] * SCALE, shared[3] * SCALE)
            painter.save()
            painter.fillRect(rect, QBrush(colour, Qt.BrushStyle.BDiagPattern))
            if not allowed:
                painter.setPen(QPen(colour, 0))
                painter.drawRect(rect)
            painter.restore()

    def itemChange(self, change, value):  # noqa: N802
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange and self.dragging():
            return self.snapped(value)
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self.refresh()
            self.page.selection_changed()
        elif change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged and self.page.items:
            if not self.page.dirty:
                self.page.set_dirty(True)
            self.page.overlaps_changed(dragging=self)
        return super().itemChange(change, value)

    def dragging(self) -> bool:
        scene = self.scene()
        return scene is not None and scene.mouseGrabberItem() is self

    def snapped(self, pos: QPointF) -> QPointF:
        """Snap to nearby screens' edges *while* dragging, so the screen
        stays exactly where you let go. Within a few pixels on screen,
        whatever the zoom; hold Alt (Option) to place it freely."""
        if QApplication.keyboardModifiers() & Qt.KeyboardModifier.AltModifier:
            return pos
        views = self.scene().views()
        zoom = views[0].transform().m11() if views else 1.0
        distance = max(1, round(SNAP_VIEW_PX / max(zoom * SCALE, 1e-6)))
        w, h = int(self.screen["width"]), int(self.screen["height"])
        x, y = round(pos.x() / SCALE), round(pos.y() / SCALE)
        others = [i.layout_rect() for i in self.page.items if i is not self]
        sx, sy = snap((x, y, w, h), others, distance)
        if (sx, sy) == (x, y):
            return pos
        return QPointF(sx * SCALE if sx != x else pos.x(), sy * SCALE if sy != y else pos.y())

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._press = (self.pos(), self.page.dirty, self.zValue())
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        super().mouseReleaseEvent(event)
        press = getattr(self, "_press", None)
        self._press = None
        travel = (event.screenPos() - event.buttonDownScreenPos(Qt.MouseButton.LeftButton)).manhattanLength()
        if press is not None and travel < CLICK_SLOP_PX:
            # A click, not a drag: put it back exactly where it was (a hand
            # shakes a pixel or two, which is many layout pixels), and don't
            # snap it anywhere. A click on an overlapping screen brings it
            # to the front.
            pos, was_dirty, z = press
            self.setPos(pos)
            others = [i.layout_rect() for i in self.page.items if i is not self]
            if self.page.allow_overlap and any(overlap_of(self.layout_rect(), o) for o in others):
                self.page.bring_to_front(self)
            if self.zValue() != z:
                self.page.moved(self)
            else:
                self.page.set_dirty(was_dirty)
                self.page.overlaps_changed()
            return
        others = [i.layout_rect() for i in self.page.items if i is not self]
        # Snapping already happened during the drag; it stays where you let go.
        # Only an overlap that isn't allowed is undone (it showed red).
        if not self.page.allow_overlap and any(overlap_of(self.layout_rect(), o) for o in others):
            x, y = push_apart(self.layout_rect(), others)
            self.setPos(x * SCALE, y * SCALE)
        if self.page.allow_overlap and any(overlap_of(self.layout_rect(), o) for o in others):
            self.page.bring_to_front(self)  # the screen you drop goes in front
        self.page.moved(self)


class CoverTag(QGraphicsItem):
    """While dragging: how much of the screen behind is covered, in real pixels."""

    def __init__(self):
        super().__init__()
        self.text = ""
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
        self.setZValue(1e6)
        self.font = QFont()
        self.font.setPointSizeF(9)

    def set_text(self, text: str) -> None:
        self.prepareGeometryChange()
        self.text = text

    def boundingRect(self) -> QRectF:  # noqa: N802
        w = QFontMetricsF(self.font).horizontalAdvance(self.text) + 20
        return QRectF(-w / 2, -11, w, 22)

    def paint(self, painter, option, widget=None) -> None:
        t = theme.current().tokens
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(theme.qcolor(t["stroke"]), 1))
        painter.setBrush(theme.qcolor(t["card"]))
        r = self.boundingRect()
        painter.drawRoundedRect(r, 11, 11)
        painter.setFont(self.font)
        painter.setPen(theme.qcolor(t["ink"]))
        painter.drawText(r, Qt.AlignmentFlag.AlignCenter, self.text)


class LayoutView(QGraphicsView):
    def __init__(self, scene):
        super().__init__(scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setMinimumHeight(260)
        self.setStyleSheet("border: 0; background: transparent;")
        self.restyle()

    def restyle(self) -> None:
        self.setBackgroundBrush(theme.qcolor(theme.current().tokens["plot"]))

    def fit(self) -> None:
        rect = self.scene().itemsBoundingRect().adjusted(-40, -40, 40, 40)
        if rect.isValid():
            self.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.fit()

    def drawBackground(self, painter, rect: QRectF) -> None:  # noqa: N802
        super().drawBackground(painter, rect)
        th = theme.current()
        painter.setPen(QPen(QColor(255, 255, 255, 22) if th.dark else theme.qcolor(th.tokens["stroke"]), 0))
        step = 16
        x0 = int(rect.left()) - int(rect.left()) % step
        y0 = int(rect.top()) - int(rect.top()) % step
        for x in range(x0, int(rect.right()), step):
            for y in range(y0, int(rect.bottom()), step):
                painter.drawPoint(QPointF(x, y))


class LayoutPage(Page):
    def __init__(self, app):
        super().__init__("Layout", "Arrange the screens like your real desk. The pointer crosses where two screens touch.")
        self.app = app
        self.items: list[ScreenItem] = []
        self.dirty = False
        self.allow_overlap = False
        self.calibration = None  # EdgeCalibration, while calibrating an edge
        # Overlaps to line up once the layout is saved and the service is back:
        # [(behind key, front key)], see save().
        self.pending_lineups: list[tuple[str, str]] = []
        # Lined up by clicks, in mirror mode: next, check it by eye with the guides (tune_fit).
        self.pending_tune: tuple[str, str] | None = None
        self.edge_note = None
        app.service.event.connect(self._on_service_event)
        app.service.state_changed.connect(self._on_service_state)
        self.overlap_box = QWidget()
        ob = QHBoxLayout(self.overlap_box)
        ob.setContentsMargins(0, 0, 8, 0)
        ob.setSpacing(6)
        self.overlap_toggle = Toggle("Allow overlap")
        self.overlap_toggle.setToolTip("Let a screen stand in front of part of another, like a laptop in front of a "
                                       "monitor's corner. The part behind it is blocked out.")
        self.overlap_toggle.toggled.connect(self.overlap_toggled)
        ob.addWidget(self.overlap_toggle)
        overlap_label = QLabel("Allow overlap")
        overlap_label.setBuddy(self.overlap_toggle)
        ob.addWidget(overlap_label)
        self.detect_button = button("Detect my screens", None, self.detect)
        self.save_button = button("Save layout", "primary", self.save)
        self.header.addWidget(self.overlap_box)
        self.header.addWidget(self.detect_button)
        self.header.addWidget(self.save_button)

        self.agent_note = label("", "warn", wrap=True)
        self.add(self.agent_note)

        self.overlap_row = QWidget()
        orow = QHBoxLayout(self.overlap_row)
        orow.setContentsMargins(0, 0, 0, 0)
        self.overlap_note = label("", "error", wrap=True)
        orow.addWidget(self.overlap_note, 1)
        self.front_button = button("Bring to front", None, self.front_clicked)
        orow.addWidget(self.front_button)
        self.overlap_row.hide()
        self.add(self.overlap_row)

        self.canvas = Card(padding=12)
        self.scene = QGraphicsScene(self)
        self.tag: CoverTag | None = None
        self.hint = None  # OverlapHint on this computer's monitors, while editing
        self._hint_sent = ""
        self.view = LayoutView(self.scene)
        self.canvas.body.addWidget(self.view)
        self.add(self.canvas, 1)

        bottom = QWidget()
        # Side by side when there's room, stacked in a narrow window: side by
        # side the two cards need ~650 px, more than a smaller Mac window
        # leaves, and the page ran off its right edge.
        grid = self.bottom_row = QBoxLayout(QBoxLayout.Direction.LeftToRight, bottom)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(16)

        self.inspector = Card()
        self.sel_title = label("Pick a screen", "h2")
        self.inspector.body.addWidget(self.sel_title)
        fields = QGridLayout()
        self.spins = {}
        for i, (key, text) in enumerate((("x", "X"), ("y", "Y"), ("width", "Width"), ("height", "Height"))):
            spin = QSpinBox()
            spin.setRange(-100000 if key in "xy" else 100, 100000)
            spin.setAccessibleName(text)
            # Sized for its range (-100000) it wanted 110 px each, which with
            # the Edges card beside it made the page wider than the window.
            spin.setMinimumWidth(64)
            spin.valueChanged.connect(lambda v, k=key: self.spin_changed(k, v))
            self.spins[key] = spin
            fields.addWidget(label(text, "small"), 0, i)
            fields.addWidget(spin, 1, i)
        self.inspector.body.addLayout(fields)
        self.reported = button("Use the size it reports", None, self.use_reported)
        self.inspector.body.addWidget(self.reported)
        self.inspector.body.addWidget(label("Drag screens on the canvas; they snap to each other's edges "
                                            "(hold Alt or Option to place freely).", "small",
                                            wrap=True))
        grid.addWidget(self.inspector, 3)

        self.edges_card = Card("Edges that connect devices")
        self.edges_box = QVBoxLayout()
        self.edges_card.body.addLayout(self.edges_box)
        self.edges_card.body.addStretch(1)
        grid.addWidget(self.edges_card, 2)
        self.add(bottom)

        app.config_changed.connect(self.load_if_clean)
        self.load()

    def side_by_side_width(self) -> int:
        """Page width the bottom cards need next to each other."""
        m = self.layout_.contentsMargins()
        return (self.inspector.minimumSizeHint().width() + self.edges_card.minimumSizeHint().width()
                + self.bottom_row.spacing() + m.left() + m.right())

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.fit_bottom_row()

    def fit_bottom_row(self) -> None:
        # Measured rather than a fixed width, since the fonts (and so the
        # cards) differ between platforms and as the edges listed change.
        narrow = self.viewport().width() < self.side_by_side_width()
        want = QBoxLayout.Direction.TopToBottom if narrow else QBoxLayout.Direction.LeftToRight
        if self.bottom_row.direction() != want:
            self.bottom_row.setDirection(want)

    # -- loading and saving -----------------------------------------------------
    def theme_changed(self) -> None:
        self.view.restyle()
        for item in self.items:
            item.refresh()
        self.view.viewport().update()

    def shown(self) -> None:
        self.load_if_clean()
        self.update_hint()

    def load_if_clean(self) -> None:
        if not self.dirty:
            self.load()

    def load(self) -> None:
        # Forget them first: clear() deselects the old screens as it deletes
        # them, and selection_changed then mustn't reach the deleted tag.
        self.items = []
        self.tag = None
        self.scene.clear()
        if not self.app.has_config():
            return
        role = self.app.role
        is_host = role == "host"
        self.canvas.setVisible(is_host)
        self.inspector.parentWidget().setVisible(is_host)
        self.detect_button.setVisible(is_host)
        self.save_button.setVisible(is_host)
        self.overlap_box.setVisible(is_host)
        self.overlap_row.hide()
        self.agent_note.setVisible(not is_host)
        if not is_host:
            self.agent_note.setText("The layout lives on the host. Open Curslink on the computer your keyboard "
                                    "and mouse are plugged into to arrange the screens.")
            return
        cfg = self.app.config()
        me = cfg.get("host", "device_id")
        self.allow_overlap = bool(cfg.get("layout", "allow_overlap", False))
        self.overlap_toggle.blockSignals(True)
        self.overlap_toggle.setChecked(self.allow_overlap)
        self.overlap_toggle.blockSignals(False)
        for d in cfg.devices():
            for i, s in enumerate(d.get("screens", [])):
                screen = {"id": str(s.get("id", f"screen{i}")), "x": int(s.get("x", 0)), "y": int(s.get("y", 0)),
                          "width": int(s.get("width", 1920)), "height": int(s.get("height", 1080)),
                          "z": int(s.get("z", 0)), "os_x": int(s.get("os_x", 0)), "os_y": int(s.get("os_y", 0))}
                item = ScreenItem(self, d.get("id"), d.get("name", d.get("id")), screen, d.get("id") == me)
                self.scene.addItem(item)
                self.items.append(item)
        if self.items:
            self.items[0].setSelected(True)
        self.tag = CoverTag()
        self.tag.hide()
        self.scene.addItem(self.tag)
        self.set_dirty(False)
        self.view.fit()
        self.refresh_edges()
        self.overlaps_changed()

    def current_devices(self) -> list[dict]:
        devices: dict[str, dict] = {}
        for item in self.items:
            x, y, w, h = item.layout_rect()
            devices.setdefault(item.device_id, {"id": item.device_id, "screens": []})["screens"].append(
                {"id": item.screen["id"], "x": x, "y": y, "width": w, "height": h})
        return list(devices.values())

    def save(self) -> None:
        if self.blocking_overlap():
            return  # the button is off; this is the belt to its braces
        from curslink.config import ConfigError, parse_layout

        cfg = self.app.config()
        overlapping = {k for pair in self.overlap_pairs() for k in (pair[0].key, pair[1].key)}
        order = {item.key: rank for rank, item in enumerate(sorted(self.items, key=lambda i: i.zValue()))}
        for item in self.items:
            x, y, w, h = item.layout_rect()
            cfg.move_screen(item.device_id, item.screen["id"], x, y)
            cfg.resize_screen(item.device_id, item.screen["id"], w, h)
            # Keep the stacking order only where it matters.
            z = order[item.key] if self.allow_overlap and item.key in overlapping else None
            cfg.set_screen_z(item.device_id, item.screen["id"], z)
        # A calibrated edge goes with the screens still meeting there.
        touching = {frozenset((a, b)) for a, _edge, b in touching_edges(self.current_devices())}
        for t in cfg.links():
            if frozenset((t.get("a"), t.get("b"))) not in touching:
                cfg.remove_link(t.get("a"), t.get("b"))
        if self.allow_overlap:
            cfg.set("layout", "allow_overlap", True)
        else:
            cfg.remove("layout", "allow_overlap")
        # A line-up goes with its two screens still overlapping that way round;
        # new overlaps are lined up straight after saving, as part of placing them.
        pairs = {(b.key, f.key) for b, f, _shared in self.overlap_pairs()} if self.allow_overlap else set()
        fitted = set()
        for t in cfg.fits():
            pair = (t.get("behind"), t.get("front"))
            if pair in pairs:
                fitted.add(pair)
            else:
                cfg.remove_fit(*pair)
        lineups = sorted(pairs - fitted) if cfg.get("layout", "mirror", False) else []
        try:
            parse_layout(cfg.raw())
        except ConfigError as exc:
            cfg.reload()
            self.overlap_note.setObjectName("error")
            self.overlap_note.setText(f"Not saved: {exc}.")
            self._restyle(self.overlap_note)
            self.front_button.hide()
            self.overlap_row.show()
            return
        cfg.save()
        self.set_dirty(False)
        self.update_hint([])
        self.app.config_saved()
        self.pending_lineups = lineups
        if lineups:
            from PySide6.QtCore import QTimer

            self._say_edge("Saved. Next, line up where the screens really overlap: a click on each screen.")
            if not self.app.service.is_alive():
                QTimer.singleShot(0, self.next_lineup)
            # Otherwise the service restarts with the new layout first (_on_service_state).

    def next_lineup(self) -> None:
        """Line up the next overlap left from save(), once the service is
        running (when another computer's screen takes part)."""
        from curslink.gui.service import RUNNING

        if not self.pending_lineups or self.calibration is not None or self.dirty:
            return
        me = self.app.config().get("host", "device_id")
        behind, front = self.pending_lineups[0]
        local = {behind.split("/", 1)[0], front.split("/", 1)[0]} == {me}
        if not local and self.app.service.state != RUNNING:
            return  # _on_service_state brings us back
        self.pending_lineups.pop(0)
        self.calibrate_fit(behind, front)

    def continue_lineup(self) -> None:
        """After a restart: check the overlap just lined up on its mirror, else line up the next."""
        if self.pending_tune is not None and self.calibration is None and not self.dirty:
            behind, front = self.pending_tune
            self.pending_tune = None
            self.tune_fit(behind, front)
            if self.calibration is not None:
                self.calibration.finished.connect(lambda _code: self.next_lineup())
                return
        self.next_lineup()

    def set_dirty(self, dirty: bool) -> None:
        self.dirty = dirty
        blocked = self.blocking_overlap()
        self.save_button.setEnabled(dirty and not blocked)
        self.save_button.setText("Save layout" if dirty else "Saved")
        if blocked:
            self.save_button.setToolTip("Screens overlap. Move them apart or turn on Allow overlap.")
        else:
            self.save_button.setToolTip("")

    # -- overlap --------------------------------------------------------------
    def in_front(self, a: ScreenItem, b: ScreenItem) -> bool:
        """Is ``a`` in front of ``b``? (Ties: the one listed later, as the service decides.)"""
        if a.zValue() != b.zValue():
            return a.zValue() > b.zValue()
        return self.items.index(a) > self.items.index(b)

    def overlap_pairs(self) -> list[tuple[ScreenItem, ScreenItem, tuple[int, int, int, int]]]:
        """Every overlapping pair as ``(behind, in front, shared area)``."""
        out = []
        for i, a in enumerate(self.items):
            for b in self.items[i + 1:]:
                shared = overlap_of(a.layout_rect(), b.layout_rect())
                if shared:
                    out.append((b, a, shared) if self.in_front(a, b) else (a, b, shared))
        return out

    def blocking_overlap(self) -> bool:
        return not self.allow_overlap and bool(self.overlap_pairs())

    def bring_to_front(self, item: ScreenItem) -> None:
        top = max((i.zValue() for i in self.items if i is not item), default=0)
        if item.zValue() <= top:
            item.setZValue(top + 1)
        for i in self.items:
            i.update()

    def front_clicked(self) -> None:
        item = self.selected()
        if item is not None:
            self.bring_to_front(item)
            self.moved(item)

    def overlap_toggled(self, on: bool) -> None:
        self.allow_overlap = on
        self.set_dirty(True)
        self.overlaps_changed()
        self.refresh_edges()

    def overlaps_changed(self, dragging: ScreenItem | None = None) -> None:
        """Restripe and re-explain the overlaps (live while dragging)."""
        for item in self.items:
            item.update()
        pairs = self.overlap_pairs()
        if self.tag is not None:
            mine = [p for p in pairs if dragging in (p[0], p[1])] if dragging else []
            if mine:
                behind, front, (x, y, w, h) = max(mine, key=lambda p: p[2][2] * p[2][3])
                other = behind if front is dragging else front
                verb = "covers" if front is dragging or not self.allow_overlap else "is behind"
                self.tag.set_text(f"{verb} {w} × {h} px of {other.device_name}" if verb == "covers"
                                  else f"{w} × {h} px hidden")
                self.tag.setPos((x + w / 2) * SCALE, (y + h / 2) * SCALE)
                self.tag.show()
            else:
                self.tag.hide()
        if not pairs:
            self.overlap_row.hide()
        else:
            behind, front, _shared = pairs[0]
            names = f"{front.device_name} / {front.screen['id']}", f"{behind.device_name} / {behind.screen['id']}"
            more = f" (and {len(pairs) - 1} more)" if len(pairs) > 1 else ""
            if self.allow_overlap:
                self.overlap_note.setObjectName("info")
                self.overlap_note.setText(f"{names[0]} stands in front of {names[1]}{more}. The striped part of "
                                          f"{names[1]} is blocked out: the pointer moves to {names[0]} there.")
                sel = self.selected()
                self.front_button.setVisible(sel is not None and any(sel is p[0] for p in pairs))
                if sel is not None:
                    self.front_button.setText(f"Bring {sel.screen['id']} to front")
            else:
                self.overlap_note.setObjectName("error")
                self.overlap_note.setText(f"{names[0]} overlaps {names[1]}{more}. Drag them apart, or turn on "
                                          "Allow overlap if one really stands in front of the other.")
                self.front_button.hide()
            self._restyle(self.overlap_note)
            self.overlap_row.show()
        if self.items:
            self.set_dirty(self.dirty)
        self.update_hint(pairs)

    # -- the overlap, on the real monitors --------------------------------------
    def hint_payload(self, pairs=None) -> dict[str, dict]:
        """``{device_id: {"screens": [...]}}``: on each screen that another
        stands in front of, the area it covers (see curslink.gui.overlaphint).
        Only while there are unsaved changes and the page is on screen."""
        if not (self.dirty and self.isVisible() and self.app.role == "host"):
            return {}
        pairs = self.overlap_pairs() if pairs is None else pairs
        out: dict[str, dict] = {}
        entries: dict[str, dict] = {}
        for behind, front, (x, y, w, h) in pairs:
            bx, by, bw, bh = behind.layout_rect()
            s = behind.screen
            entry = entries.get(behind.key)
            if entry is None:
                entry = {"screen": s["id"], "os": [s.get("os_x", 0), s.get("os_y", 0), bw, bh],
                         "size": [bw, bh], "regions": []}
                entries[behind.key] = entry
                out.setdefault(behind.device_id, {"screens": []})["screens"].append(entry)
            label = (f"{front.device_name} stands here" if self.allow_overlap
                     else f"Overlaps {front.device_name}: drag apart or allow overlap")
            entry["regions"].append({"rect": [x - bx, y - by, w, h], "label": label, "bad": not self.allow_overlap})
        return out

    def update_hint(self, pairs=None) -> None:
        payload = self.hint_payload(pairs)
        me = self.app.config().get("host", "device_id") if self.app.has_config() else None
        local = payload.pop(me, None) if me else None
        if local:
            if self.hint is None:
                from curslink.gui.overlaphint import OverlapHint

                self.hint = OverlapHint()
            self.hint.show(local)
        elif self.hint is not None:
            self.hint.hide()
        # Paired computers' screens: through the running service.
        command = "hint " + json.dumps({"devices": payload}, separators=(",", ":"))
        if command != self._hint_sent and (payload or self._hint_sent):
            self.app.service.send(command)
            self._hint_sent = command if payload else ""

    # -- calibrating an edge ---------------------------------------------------
    def _say_edge(self, text: str) -> None:
        if self.edge_note is not None:
            self.edge_note.setText(text)
            self.edge_note.setVisible(bool(text))

    def calibrate_edge(self, a: str, edge: str, b: str) -> None:
        """Click where screens ``a`` and ``b`` join, on the real monitors
        (see curslink.gui.edgecalib)."""
        from curslink.gui.edgecalib import EdgeCalibration
        from curslink.gui.service import RUNNING
        from curslink.layout import OPPOSITE

        if self.calibration is not None:
            return
        if self.dirty:
            self._say_edge("Save the layout first, then calibrate the edge.")
            return
        if self.app.service.state != RUNNING:
            self._say_edge("Start linking first: you'll click on both computers' screens.")
            return
        me = self.app.config().get("host", "device_id")
        items = {i.key: i for i in self.items}
        ends = []
        for ref, side in ((a, edge), (b, OPPOSITE[edge])):
            item = items.get(ref)
            if item is None:
                return
            _x, _y, w, h = item.layout_rect()
            ends.append({"ref": ref, "device": item.device_id, "name": item.device_name, "side": side,
                         "screen": {**item.screen, "width": w, "height": h}})
        ends.sort(key=lambda e: e["device"] != me)  # start on this computer
        self.calibration = EdgeCalibration(ends, me, self.app.service.send)
        self.calibration.finished.connect(lambda spans, ends=ends: self.edge_calibrated(ends, spans))
        self._say_edge("")
        if not self.calibration.start():
            self.calibration = None
            self._say_edge("Couldn't find that screen on this computer.")

    def edge_calibrated(self, ends: list[dict], spans) -> None:
        self.calibration = None
        if spans is None:
            return
        cfg = self.app.config()
        (ea, sa), (eb, sb) = zip(ends, spans)
        cfg.set_link(ea["ref"], ea["side"], sa, eb["ref"], eb["side"], sb)
        cfg.save()
        self.app.config_saved()
        self.refresh_edges()
        self._say_edge("Edge calibrated. The pointer now crosses along the stretches you clicked.")

    def calibrate_fit(self, behind: str, front: str) -> None:
        """Click where the two screens' corners really are, one on each
        (see curslink.gui.fitcalib), for mirror mode."""
        from curslink.gui.fitcalib import FitCalibration
        from curslink.gui.service import RUNNING

        if self.calibration is not None:
            return
        if self.dirty:
            self._say_edge("Save the layout first, then line up the overlap.")
            return
        items = {i.key: i for i in self.items}
        b, f = items.get(behind), items.get(front)
        if b is None or f is None:
            return
        me = self.app.config().get("host", "device_id")
        if {b.device_id, f.device_id} != {me} and self.app.service.state != RUNNING:
            self._say_edge("Start linking first: you'll click on the other computer's screen too.")
            return

        def end(item) -> dict:
            x, y, w, h = item.layout_rect()
            return {"device": item.device_id, "name": item.device_name, "rect": [x, y, w, h],
                    "screen": {**item.screen, "width": w, "height": h}}

        self.calibration = FitCalibration(end(b), end(f), me, self.app.service.send)
        self.calibration.finished.connect(lambda fit, b=behind, f=front: self.fit_calibrated(b, f, fit))
        self._say_edge(f"Click on {b.device_name}'s screen first, then on {f.device_name}'s.")
        if not self.calibration.start():
            self.calibration = None
            self._say_edge("Couldn't find that screen on this computer.")

    def fit_calibrated(self, behind: str, front: str, fit) -> None:
        self.calibration = None
        if fit is None:
            self.pending_lineups = []
            self._say_edge("Not lined up: mirrors go by the screens' real sizes. "
                           "Use \u201cLine up overlap\u2026\u201d below to do it later.")
            return
        cfg = self.app.config()
        cfg.set_fit(behind, front, *fit)
        cfg.save()
        self.app.config_saved()
        self.refresh_edges()
        if cfg.get("layout", "mirror", False):
            # Clicks get close; the guides on the mirror itself get it right.
            self.pending_tune = (behind, front)
            self._say_edge("Overlap lined up. Next, line up the coloured lines on the mirror…")
            if not self.app.service.is_alive():
                self.pending_tune = None
                self._say_edge("Overlap lined up. Start linking, then use \u201cFine-tune\u2026\u201d to check it on the mirror.")
            return
        self._say_edge("Overlap lined up. Mirror mode now shows exactly what the screen in front covers.")

    def tune_fit(self, behind: str, front: str) -> None:
        """Nudge the mirror by eye while it's showing (see curslink.gui.fittune)."""
        from curslink.gui.fittune import FitTuneDialog
        from curslink.gui.service import RUNNING

        if self.calibration is not None:
            return
        if self.dirty:
            self._say_edge("Save the layout first, then fine-tune the overlap.")
            return
        cfg = self.app.config()
        if not cfg.get("layout", "mirror", False):
            self._say_edge("Turn on mirror mode first (Settings › Overlapping screens), then fine-tune it here.")
            return
        if self.app.service.state != RUNNING:
            self._say_edge("Start linking first: you fine-tune the mirror while it's showing.")
            return
        items = {i.key: i for i in self.items}
        b, f = items.get(behind), items.get(front)
        if b is None or f is None:
            return
        saved = None
        for t in cfg.fits():
            if (t.get("behind"), t.get("front")) == (behind, front):
                try:
                    saved = (float(t["x"]), float(t["y"]), float(t["scale"]))
                except (KeyError, TypeError, ValueError):
                    saved = None
        dialog = FitTuneDialog({"key": behind, "name": b.device_name}, {"key": front, "name": f.device_name},
                               saved, self.app.service.send, self)
        dialog.saved.connect(lambda fit, b=behind, f=front: self.fit_tuned(b, f, fit))
        self.calibration = dialog
        dialog.finished.connect(lambda _code: setattr(self, "calibration", None))
        dialog.show()
        from curslink.gui.native import bring_to_front

        bring_to_front(dialog)

    def fit_tuned(self, behind: str, front: str, fit) -> None:
        cfg = self.app.config()
        cfg.set_fit(behind, front, *fit)
        cfg.save()
        # The service already has it (the fit command), so no restart.
        self.app.config_saved(restart=False)
        self.refresh_edges()
        self._say_edge("Overlap fine-tuned and saved.")

    def reset_fit(self, behind: str, front: str) -> None:
        cfg = self.app.config()
        if cfg.remove_fit(behind, front):
            cfg.save()
            self.app.config_saved()
        self.refresh_edges()

    def reset_edge(self, a: str, b: str) -> None:
        cfg = self.app.config()
        if cfg.remove_link(a, b):
            cfg.save()
            self.app.config_saved()
        self.refresh_edges()

    def _on_service_event(self, event: dict) -> None:
        if self.calibration is not None:
            self.calibration.on_event(event)

    def _on_service_state(self, state: str) -> None:
        from curslink.gui.service import RUNNING

        if state == RUNNING and self.isVisible():
            self.tell_setup(True)

        if state == RUNNING and (self.pending_lineups or self.pending_tune):
            from PySide6.QtCore import QTimer

            QTimer.singleShot(3000, self.continue_lineup)  # give the other computers a moment to reconnect
        if state != RUNNING and self.calibration is not None:
            calibration, self.calibration = self.calibration, None
            calibration.stop()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.tell_setup(True)

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self.update_hint([])
        self.tell_setup(False)

    def tell_setup(self, on: bool) -> None:
        """While this page is open the service plays no crossing animations
        and shows no edge glow: they only get in the way of arranging."""
        if self.app.role == "host" and self.app.service.is_alive():
            self.app.service.send("setup on" if on else "setup off")

    @staticmethod
    def _restyle(w) -> None:
        w.style().unpolish(w)
        w.style().polish(w)

    # -- editing ----------------------------------------------------------------
    def selected(self) -> ScreenItem | None:
        return next((i for i in self.items if i.isSelected()), None)

    def selection_changed(self) -> None:
        item = self.selected()
        for spin in self.spins.values():
            spin.setEnabled(item is not None)
        if item is None:
            self.sel_title.setText("Pick a screen")
            self.reported.hide()
            return
        self.sel_title.setText(f"{item.device_name} / {item.screen['id']}")
        x, y, w, h = item.layout_rect()
        for key, value in (("x", x), ("y", y), ("width", w), ("height", h)):
            spin = self.spins[key]
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
        self.reported.setVisible(bool(self.reported_screens(item)))
        if self.overlap_row.isVisible():
            self.overlaps_changed()

    def reported_screens(self, item: ScreenItem) -> list[dict]:
        agent = self.app.status.agents.get(item.device_id)
        return [s for s in (agent.screens if agent else []) if "width" in s and "height" in s]

    def use_reported(self) -> None:
        item = self.selected()
        if item is None:
            return
        reported = self.reported_screens(item)
        if reported:
            index = [i for i in self.items if i.device_id == item.device_id].index(item)
            s = reported[min(index, len(reported) - 1)]
            self.spins["width"].setValue(int(s["width"]))
            self.spins["height"].setValue(int(s["height"]))

    def spin_changed(self, key: str, value: int) -> None:
        item = self.selected()
        if item is None:
            return
        if key in ("width", "height"):
            item.screen[key] = value
            item.refresh()
        else:
            x, y, _w, _h = item.layout_rect()
            item.setPos((value if key == "x" else x) * SCALE, (value if key == "y" else y) * SCALE)
        self.moved(item)

    def moved(self, item: ScreenItem) -> None:
        self.set_dirty(True)
        self.selection_changed()
        self.refresh_edges()
        self.overlaps_changed()

    def detect(self) -> None:
        """Replace this computer's screens with what the OS reports, keeping
        the group where it was on the canvas."""
        cfg = self.app.config()
        me = cfg.get("host", "device_id")
        mine = [i for i in self.items if i.device_id == me]
        if not mine:
            return
        found = detect_screens()
        if not found:
            return
        left = min(i.layout_rect()[0] for i in mine)
        top = min(i.layout_rect()[1] for i in mine)
        min_x, min_y = min(s["x"] for s in found), min(s["y"] for s in found)
        screens = [{"id": s["id"], "x": s["x"] - min_x + left, "y": s["y"] - min_y + top,
                    "width": s["width"], "height": s["height"], "os_x": s["x"], "os_y": s["y"]} for s in found]
        cfg.replace_screens(me, screens)
        from curslink.config import ConfigError, parse_layout

        try:
            parse_layout(cfg.raw())
        except ConfigError as exc:
            # The screens found would overlap another device's: don't save that.
            cfg.reload()
            self.overlap_note.setObjectName("error")
            self.overlap_note.setText(f"Your screens weren't updated: as detected, {exc}. Move the other "
                                      "screens out of the way, save, and detect again.")
            self._restyle(self.overlap_note)
            self.front_button.hide()
            self.overlap_row.show()
            return
        cfg.save()
        self.set_dirty(False)
        self.app.config_saved()
        self.load()

    def refresh_edges(self) -> None:
        from curslink.gui.widgets import clear_layout, pill

        clear_layout(self.edges_box)
        names = {i.device_id: i.device_name for i in self.items}
        linked = set()
        if self.app.has_config():
            linked = {frozenset((t.get("a"), t.get("b"))) for t in self.app.config().links()}
        seen = set()
        for a, edge, b in touching_edges(self.current_devices()):
            if (b, a) in seen:
                continue
            seen.add((a, b))
            ad, ascreen = a.split("/", 1)
            bd, bscreen = b.split("/", 1)
            self.edges_box.addWidget(label(f"{names.get(ad, ad)} / {ascreen}  {edge} edge  →  "
                                           f"{names.get(bd, bd)} / {bscreen}", wrap=True))
            row = QHBoxLayout()
            done = frozenset((a, b)) in linked
            row.addWidget(pill("Calibrated" if done else "By layout", "on" if done else "off"))
            row.addStretch(1)
            row.addWidget(button("Calibrate again…" if done else "Calibrate edge…", None,
                                 lambda _=False, a=a, edge=edge, b=b: self.calibrate_edge(a, edge, b)))
            if done:
                row.addWidget(button("Reset", None, lambda _=False, a=a, b=b: self.reset_edge(a, b)))
            self.edges_box.addLayout(row)
        self.edge_note = label("", "small", wrap=True)
        self.edge_note.hide()
        self.edges_box.addWidget(self.edge_note)
        if self.allow_overlap:
            fitted = set()
            if self.app.has_config():
                fitted = {(t.get("behind"), t.get("front")) for t in self.app.config().fits()}
            for behind, front, _shared in self.overlap_pairs():
                if behind.device_id != front.device_id:
                    seen.add((front.key, behind.key))
                self.edges_box.addWidget(label(f"{front.device_name} / {front.screen['id']}  in front of  "
                                               f"{behind.device_name} / {behind.screen['id']}", wrap=True))
                # Mirror mode: where the screen in front really stands on the one behind.
                row = QHBoxLayout()
                done = (behind.key, front.key) in fitted
                row.addWidget(pill("Lined up" if done else "By layout", "on" if done else "off"))
                row.addStretch(1)
                row.addWidget(button("Line up again…" if done else "Line up overlap…", None,
                                     lambda _=False, b=behind.key, f=front.key: self.calibrate_fit(b, f)))
                row.addWidget(button("Fine-tune…", None,
                                     lambda _=False, b=behind.key, f=front.key: self.tune_fit(b, f)))
                if done:
                    row.addWidget(button("Reset", None, lambda _=False, b=behind.key, f=front.key: self.reset_fit(b, f)))
                self.edges_box.addLayout(row)
        if not seen:
            w = label("No screens of different devices touch, so the pointer can't cross by the edges. "
                      "Move a screen next to another device's.", "warn", wrap=True)
            self.edges_box.addWidget(w)
        self.fit_bottom_row()
