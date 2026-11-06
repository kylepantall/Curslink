"""Small building blocks for the desktop app's pages, drawn from the theme."""

from __future__ import annotations

import weakref
from functools import lru_cache

from PySide6.QtCore import QByteArray, QPropertyAnimation, QRectF, QSize, Qt, Property, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from curslink.gui import theme
from curslink.gui.common import asset

# ------------------------------------------------------------------ images


@lru_cache(maxsize=256)
def _svg_image(path: str, w: int, h: int, colour: str | None, ratio: float) -> QImage:
    data = asset(path)
    if colour is not None:
        data = data.replace(b"currentColor", colour.encode())
    renderer = QSvgRenderer(QByteArray(data))
    image = QImage(round(w * ratio), round(h * ratio), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(p, QRectF(0, 0, image.width(), image.height()))
    p.end()
    image.setDevicePixelRatio(ratio)
    return image


def svg_pixmap(path: str, w: int, h: int, colour: str | None = None, ratio: float = 2.0) -> QPixmap:
    """An SVG asset (relative to curslink/assets) as a crisp pixmap;
    ``currentColor`` becomes ``colour``."""
    return QPixmap.fromImage(_svg_image(path, w, h, colour, ratio))


def spot(name: str, w: int = 160, h: int = 120) -> QLabel:
    """One of the in-app illustrations (assets/illustrations/spot)."""
    lab = QLabel()
    lab.setPixmap(svg_pixmap(f"illustrations/spot/{name}.svg", w, h))
    lab.setFixedSize(w, h)
    lab.setAccessibleName(name.replace("-", " "))
    lab.setStyleSheet("background: transparent;")
    return lab


def set_spot(lab: QLabel, name: str, w: int = 160, h: int = 120) -> None:
    lab.setPixmap(svg_pixmap(f"illustrations/spot/{name}.svg", w, h))


def nav_icon(name: str, colour: str, size: int = 16) -> QIcon:
    return QIcon(svg_pixmap(f"navigation/{name}.svg", size, size, colour))


def blend(a: QColor, b: QColor, t: float) -> QColor:
    return QColor(round(a.red() + (b.red() - a.red()) * t), round(a.green() + (b.green() - a.green()) * t),
                  round(a.blue() + (b.blue() - a.blue()) * t), round(a.alpha() + (b.alpha() - a.alpha()) * t))


# ----------------------------------------------------------------- controls

class Toggle(QAbstractButton):
    """An on/off switch, shaped like each platform's own."""

    def __init__(self, label: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(label)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._pos = 0.0
        self._anim = QPropertyAnimation(self, b"knob", self)
        self._anim.setDuration(120)
        self.toggled.connect(self._animate)

    @staticmethod
    def dims() -> tuple[int, int, int]:
        plat = theme.current().platform
        return {"win": (40, 20, 12), "mac": (38, 22, 20), "linux": (48, 26, 20)}[plat]

    def sizeHint(self) -> QSize:
        w, h, _ = self.dims()
        return QSize(w + 4, h + 4)

    def _animate(self, on: bool) -> None:
        self._anim.stop()
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def setChecked(self, on: bool) -> None:  # noqa: N802 - Qt naming
        super().setChecked(on)
        self._anim.stop()
        self._pos = 1.0 if on else 0.0
        self.update()

    def _get_knob(self) -> float:
        return self._pos

    def _set_knob(self, value: float) -> None:
        self._pos = value
        self.update()

    knob = Property(float, _get_knob, _set_knob)

    def paintEvent(self, _event) -> None:  # noqa: N802
        th = theme.current()
        w, h, k = self.dims()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(2, (self.height() - h) / 2, w, h)
        accent = th.color("accent")
        if th.platform == "win":
            off_track = QColor(0, 0, 0, 0)
            off_knob = th.color("ink2")
            on_knob = th.color("accent_ink")
        else:
            off_track = th.solid("ctl") if th.platform == "linux" else QColor(120, 120, 128, 70 if th.dark else 60)
            off_knob = on_knob = QColor("#FFFFFF")
        enabled = self.isEnabled()
        track = blend(off_track, accent, self._pos)
        if not enabled:
            track.setAlphaF(track.alphaF() * 0.45)
        if th.platform == "win" and self._pos < 1:
            pen = blend(th.color("ink2"), accent, self._pos)
            p.setPen(pen)
        else:
            p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), h / 2, h / 2)
        if self.hasFocus():
            p.setPen(accent)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r.adjusted(-2, -2, 2, 2), h / 2 + 2, h / 2 + 2)
        knob = blend(off_knob, on_knob, self._pos)
        pad = (h - k) / 2
        x = r.left() + pad + self._pos * (w - k - 2 * pad)
        p.setPen(Qt.PenStyle.NoPen)
        if th.platform != "win":
            p.setBrush(QColor(0, 0, 0, 50))
            p.drawEllipse(QRectF(x, r.top() + pad + 0.8, k, k))
        p.setBrush(knob)
        p.drawEllipse(QRectF(x, r.top() + pad, k, k))
        p.end()


def label(text: str = "", name: str | None = None, wrap: bool = False) -> QLabel:
    w = QLabel(text)
    if name:
        w.setObjectName(name)
    if wrap:
        w.setWordWrap(True)
    w.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return w


def button(text: str, name: str | None = None, slot=None) -> QPushButton:
    b = QPushButton(text)
    if name:
        b.setObjectName(name)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if slot is not None:
        b.clicked.connect(slot)
    return b


_pills: "weakref.WeakKeyDictionary[QLabel, str]" = weakref.WeakKeyDictionary()


def pill(text: str, kind: str) -> QLabel:
    w = QLabel(text)
    set_pill(w, text, kind)
    return w


def set_pill(w: QLabel, text: str, kind: str) -> None:
    w.setText(text)
    w.setStyleSheet(theme.pill_style(kind))
    w.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    _pills[w] = kind


def restyle_pills() -> None:
    """After the theme changes: pills carry their colours inline."""
    for w, kind in list(_pills.items()):
        try:
            w.setStyleSheet(theme.pill_style(kind))
        except RuntimeError:  # deleted on the C++ side
            pass


class Segmented(QWidget):
    """A row of mutually exclusive buttons (System / Light / Dark)."""

    picked = Signal(str)

    def __init__(self, options: list[tuple[str, str]]):
        super().__init__()
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)
        self.group = QButtonGroup(self)
        self.buttons: dict[str, QPushButton] = {}
        for key, text in options:
            b = button(text, "seg")
            b.setCheckable(True)
            self.group.addButton(b)
            self.buttons[key] = b
            b.clicked.connect(lambda _=False, k=key: self.picked.emit(k))
            h.addWidget(b)

    def set_value(self, key: str) -> None:
        if key in self.buttons:
            self.buttons[key].setChecked(True)

    def value(self) -> str:
        return next((k for k, b in self.buttons.items() if b.isChecked()), "")


class Card(QFrame):
    """A panel with an optional heading; ``body`` is its layout."""

    def __init__(self, title: str = "", padding: int = 18, spacing: int = 12, rows: bool = False):
        super().__init__()
        self.setObjectName("card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0 if rows else padding, 6 if rows else padding,
                                 0 if rows else padding, 6 if rows else padding)
        outer.setSpacing(0 if rows else spacing)
        self._rows = rows
        self._padding = padding
        self.heading = None
        if title:
            heading = label(title, "h2")
            if rows:
                heading.setContentsMargins(padding, 10, padding, 8)
            outer.addWidget(heading)
            self.heading = heading
        self.body = outer
        holder = QWidget()
        self.rows = QVBoxLayout(holder)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(0)
        outer.addWidget(holder)

    def add_row(self, *widgets: QWidget, stretch_first: bool = True) -> QFrame:
        """A row with a line above it (for list-style cards)."""
        row = QFrame()
        row.setObjectName("row" if self.rows.count() or self.heading is not None else "rowFirst")
        h = QHBoxLayout(row)
        h.setContentsMargins(self._padding, 10, self._padding, 10)
        h.setSpacing(14)
        row.setMinimumHeight(56)
        for i, w in enumerate(widgets):
            h.addWidget(w, 1 if (i == 0 and stretch_first) else 0)
        self.rows.addWidget(row)
        return row

    def clear_rows(self) -> None:
        clear_layout(self.rows)


class EmptyState(QWidget):
    """An illustration, a title, a line and an optional button, centred."""

    def __init__(self, art: str, title: str, text: str = "", action: QPushButton | None = None, size=(160, 120)):
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 18, 16, 18)
        v.setSpacing(6)
        self.size_ = size
        self.art = spot(art, *size)
        self.title = label(title, "emptyTitle")
        self.text = label(text, "muted", wrap=True)
        v.addWidget(self.art, 0, Qt.AlignmentFlag.AlignHCenter)
        v.addWidget(self.title)
        v.addWidget(self.text)  # full width, or wrapped text gets cut short
        self.title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.text.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.text.setVisible(bool(text))
        self.action = action
        if action is not None:
            v.addSpacing(8)
            v.addWidget(action, 0, Qt.AlignmentFlag.AlignHCenter)

    def set(self, art: str, title: str, text: str = "") -> None:
        set_spot(self.art, art, *self.size_)
        self.title.setText(title)
        self.text.setText(text)
        self.text.setVisible(bool(text))


def text_block(title: str, detail: str) -> QWidget:
    w = QWidget()
    v = QVBoxLayout(w)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(1)
    t = label(title)
    v.addWidget(t)
    d = label(detail, "small", wrap=True)
    d.setVisible(bool(detail))
    v.addWidget(d)
    w.title_label = t  # type: ignore[attr-defined]
    w.detail_label = d  # type: ignore[attr-defined]
    return w


def group_label(text: str) -> QLabel:
    return label(text, "group")


class Page(QScrollArea):
    """A scrolling page with a title and a one-line description."""

    def __init__(self, title: str, description: str = ""):
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.viewport().setAutoFillBackground(False)
        inner = QWidget()
        inner.setObjectName("page")
        self.setWidget(inner)
        self.layout_ = QVBoxLayout(inner)
        plat = theme.current().platform
        top = {"win": 24, "mac": 30, "linux": 18}[plat]
        self.layout_.setContentsMargins(32, top, 32, 32)
        self.layout_.setSpacing(16)
        self.header = QHBoxLayout()
        self.header.setSpacing(12)
        titles = QVBoxLayout()
        titles.setSpacing(4)
        self.title_label = label(title, "h1")
        titles.addWidget(self.title_label)
        self.description = label(description, "muted", wrap=True)
        if description:
            titles.addWidget(self.description)
        self.header.addLayout(titles, 1)
        self.layout_.addLayout(self.header)

    def add(self, widget: QWidget, stretch: int = 0) -> QWidget:
        self.layout_.addWidget(widget, stretch)
        return widget

    def finish(self) -> None:
        self.layout_.addStretch(1)


def clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.hide()
            w.setParent(None)
            w.deleteLater()
        elif item.layout() is not None:
            clear_layout(item.layout())
