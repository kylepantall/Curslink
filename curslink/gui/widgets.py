"""Small building blocks for the desktop app's pages."""

from __future__ import annotations

from PySide6.QtCore import QPropertyAnimation, QRectF, QSize, Qt, Property
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from curslink.gui.common import ACCENT, pill_style


class Toggle(QAbstractButton):
    """An on/off switch."""

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

    def sizeHint(self) -> QSize:
        return QSize(52, 30)

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
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(0, 0, 52, 30)
        off, on = QColor("#B8C2CF"), QColor(ACCENT)
        if not self.isEnabled():
            off, on = QColor("#D9DEE5"), QColor("#9BB9D9")
        track = QColor(
            int(off.red() + (on.red() - off.red()) * self._pos),
            int(off.green() + (on.green() - off.green()) * self._pos),
            int(off.blue() + (on.blue() - off.blue()) * self._pos),
        )
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(r, 15, 15)
        if self.hasFocus():
            p.setPen(QColor("#3FA9FF"))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r.adjusted(1, 1, -1, -1), 14, 14)
            p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("white"))
        x = 3 + self._pos * 22
        p.drawEllipse(QRectF(x, 3, 24, 24))
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


def pill(text: str, kind: str) -> QLabel:
    w = QLabel(text)
    set_pill(w, text, kind)
    return w


def set_pill(w: QLabel, text: str, kind: str) -> None:
    w.setText(text)
    w.setStyleSheet(pill_style(kind))
    w.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)


class Card(QFrame):
    """A white panel with an optional heading; ``body`` is its layout."""

    def __init__(self, title: str = "", padding: int = 20, spacing: int = 12, rows: bool = False):
        super().__init__()
        self.setObjectName("card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0 if rows else padding, 8 if rows else padding,
                                 0 if rows else padding, 8 if rows else padding)
        outer.setSpacing(0 if rows else spacing)
        self._rows = rows
        self._padding = padding
        if title:
            heading = label(title, "h2")
            if rows:
                heading.setContentsMargins(padding, 10, padding, 10)
            outer.addWidget(heading)
        self.body = outer
        holder = QWidget()
        self.rows = QVBoxLayout(holder)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(0)
        outer.addWidget(holder)

    def add_row(self, *widgets: QWidget, stretch_first: bool = True) -> QFrame:
        """A row with a line above it (for list-style cards)."""
        row = QFrame()
        row.setObjectName("row")
        h = QHBoxLayout(row)
        h.setContentsMargins(self._padding, 12, self._padding, 12)
        h.setSpacing(16)
        for i, w in enumerate(widgets):
            h.addWidget(w, 1 if (i == 0 and stretch_first) else 0)
        self.rows.addWidget(row)
        return row

    def clear_rows(self) -> None:
        clear_layout(self.rows)


def text_block(title: str, detail: str) -> QWidget:
    w = QWidget()
    v = QVBoxLayout(w)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(2)
    t = label(title)
    t.setStyleSheet("font-weight: 600; font-size: 15px;")
    v.addWidget(t)
    d = label(detail, "small", wrap=True)
    d.setVisible(bool(detail))
    v.addWidget(d)
    w.title_label = t  # type: ignore[attr-defined]
    w.detail_label = d  # type: ignore[attr-defined]
    return w


class Page(QScrollArea):
    """A scrolling page with a title and a one-line description."""

    def __init__(self, title: str, description: str = ""):
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        inner.setObjectName("page")
        self.setWidget(inner)
        self.layout_ = QVBoxLayout(inner)
        self.layout_.setContentsMargins(40, 32, 40, 32)
        self.layout_.setSpacing(20)
        self.header = QHBoxLayout()
        self.header.setSpacing(16)
        titles = QVBoxLayout()
        titles.setSpacing(4)
        titles.addWidget(label(title, "h1"))
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
