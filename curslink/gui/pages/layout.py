"""Layout: drag the screens of every device into place, like the real desk."""

from __future__ import annotations

import sys

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import (
    QGraphicsItem,
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
from curslink.gui.widgets import Card, Page, button, label

SCALE = 0.1  # scene units per layout pixel


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

    def itemChange(self, change, value):  # noqa: N802
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self.refresh()
            self.page.selection_changed()
        return super().itemChange(change, value)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        super().mouseReleaseEvent(event)
        others = [i.layout_rect() for i in self.page.items if i is not self]
        x, y = snap(self.layout_rect(), others)
        self.setPos(x * SCALE, y * SCALE)
        self.page.moved(self)


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
        self.detect_button = button("Detect my screens", None, self.detect)
        self.save_button = button("Save layout", "primary", self.save)
        self.header.addWidget(self.detect_button)
        self.header.addWidget(self.save_button)

        self.agent_note = label("", "warn", wrap=True)
        self.add(self.agent_note)

        self.canvas = Card(padding=12)
        self.scene = QGraphicsScene(self)
        self.view = LayoutView(self.scene)
        self.canvas.body.addWidget(self.view)
        self.add(self.canvas, 1)

        bottom = QWidget()
        grid = QGridLayout(bottom)
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
            spin.valueChanged.connect(lambda v, k=key: self.spin_changed(k, v))
            self.spins[key] = spin
            fields.addWidget(label(text, "small"), 0, i)
            fields.addWidget(spin, 1, i)
        self.inspector.body.addLayout(fields)
        self.reported = button("Use the size it reports", None, self.use_reported)
        self.inspector.body.addWidget(self.reported)
        self.inspector.body.addWidget(label("Drag screens on the canvas; they snap to each other's edges.", "small",
                                            wrap=True))
        grid.addWidget(self.inspector, 0, 0)

        self.edges_card = Card("Edges that connect devices")
        self.edges_box = QVBoxLayout()
        self.edges_card.body.addLayout(self.edges_box)
        self.edges_card.body.addStretch(1)
        grid.addWidget(self.edges_card, 0, 1)
        self.add(bottom)

        app.config_changed.connect(self.load_if_clean)
        self.load()

    # -- loading and saving -----------------------------------------------------
    def theme_changed(self) -> None:
        self.view.restyle()
        for item in self.items:
            item.refresh()
        self.view.viewport().update()

    def shown(self) -> None:
        self.load_if_clean()

    def load_if_clean(self) -> None:
        if not self.dirty:
            self.load()

    def load(self) -> None:
        self.scene.clear()
        self.items = []
        if not self.app.has_config():
            return
        role = self.app.role
        is_host = role == "host"
        self.canvas.setVisible(is_host)
        self.inspector.parentWidget().setVisible(is_host)
        self.detect_button.setVisible(is_host)
        self.save_button.setVisible(is_host)
        self.agent_note.setVisible(not is_host)
        if not is_host:
            self.agent_note.setText("The layout lives on the host. Open Curslink on the computer your keyboard "
                                    "and mouse are plugged into to arrange the screens.")
            return
        cfg = self.app.config()
        me = cfg.get("host", "device_id")
        for d in cfg.devices():
            for i, s in enumerate(d.get("screens", [])):
                screen = {"id": str(s.get("id", f"screen{i}")), "x": int(s.get("x", 0)), "y": int(s.get("y", 0)),
                          "width": int(s.get("width", 1920)), "height": int(s.get("height", 1080))}
                item = ScreenItem(self, d.get("id"), d.get("name", d.get("id")), screen, d.get("id") == me)
                self.scene.addItem(item)
                self.items.append(item)
        if self.items:
            self.items[0].setSelected(True)
        self.set_dirty(False)
        self.view.fit()
        self.refresh_edges()

    def current_devices(self) -> list[dict]:
        devices: dict[str, dict] = {}
        for item in self.items:
            x, y, w, h = item.layout_rect()
            devices.setdefault(item.device_id, {"id": item.device_id, "screens": []})["screens"].append(
                {"id": item.screen["id"], "x": x, "y": y, "width": w, "height": h})
        return list(devices.values())

    def save(self) -> None:
        cfg = self.app.config()
        for item in self.items:
            x, y, w, h = item.layout_rect()
            cfg.move_screen(item.device_id, item.screen["id"], x, y)
            cfg.resize_screen(item.device_id, item.screen["id"], w, h)
        cfg.save()
        self.set_dirty(False)
        self.app.config_saved()

    def set_dirty(self, dirty: bool) -> None:
        self.dirty = dirty
        self.save_button.setEnabled(dirty)
        self.save_button.setText("Save layout" if dirty else "Saved")

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
        cfg.save()
        self.set_dirty(False)
        self.app.config_saved()
        self.load()

    def refresh_edges(self) -> None:
        from curslink.gui.widgets import clear_layout

        clear_layout(self.edges_box)
        names = {i.device_id: i.device_name for i in self.items}
        seen = set()
        for a, edge, b in touching_edges(self.current_devices()):
            if (b, a) in seen:
                continue
            seen.add((a, b))
            ad, ascreen = a.split("/", 1)
            bd, bscreen = b.split("/", 1)
            self.edges_box.addWidget(label(f"{names.get(ad, ad)} / {ascreen}  {edge} edge  →  "
                                           f"{names.get(bd, bd)} / {bscreen}"))
        if not seen:
            w = label("No screens of different devices touch, so the pointer can't cross by the edges. "
                      "Move a screen next to another device's.", "warn", wrap=True)
            self.edges_box.addWidget(w)
