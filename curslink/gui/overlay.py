"""The "Look here" overlay shown on a screen while calibrating (spec 5.3).

The screen to look at gets a dimmed full-screen layer with a card in the
middle; the host's other screens get the dim layer alone, so the eye goes
to the right place. Space records, Esc cancels.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from curslink.gui import theme
from curslink.gui.widgets import button, label

log = logging.getLogger(__name__)


def qscreen_for(screen_id: str):
    """The QScreen a layout screen id names on this computer ("main" is the
    primary, "screenN" is the Nth in Qt's list, as Layout > Detect names them)."""
    screens = QGuiApplication.screens()
    if screen_id == "main":
        return QGuiApplication.primaryScreen()
    if screen_id.startswith("screen") and screen_id[6:].isdigit():
        n = int(screen_id[6:]) - 1
        if 0 <= n < len(screens):
            return screens[n]
    return None


class _Layer(QWidget):
    record = Signal()
    cancel = Signal()

    def __init__(self, screen, with_card: bool):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, not with_card)
        self.setObjectName("root")
        self.setGeometry(screen.geometry())
        self.with_card = with_card
        self.card = None
        if with_card:
            outer = QVBoxLayout(self)
            outer.addStretch(1)
            self.card = QWidget()
            self.card.setObjectName("overlayCard")
            self.card.setFixedWidth(380)
            v = QVBoxLayout(self.card)
            v.setContentsMargins(28, 26, 28, 22)
            v.setSpacing(8)
            self.eyebrow = label("", "eyebrow")
            self.title = label("Look here", "big")
            self.text = label("Look at the middle of this screen, then press Space.", "muted", wrap=True)
            for w in (self.eyebrow, self.title, self.text):
                v.addWidget(w)  # full width, so the wrapped line isn't cut short
                w.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            row = QHBoxLayout()
            row.addStretch(1)
            row.addWidget(button("Cancel", None, self.cancel.emit))
            self.record_button = button("Record", "primary", self.record.emit)
            row.addWidget(self.record_button)
            row.addStretch(1)
            v.addSpacing(8)
            v.addLayout(row)
            outer.addWidget(self.card, 0, Qt.AlignmentFlag.AlignHCenter)
            outer.addStretch(1)

    def paintEvent(self, _e) -> None:  # noqa: N802
        th = theme.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(0, 0, 0, 120 if self.with_card else 150))
        if self.card is not None:
            r = QRectF(self.card.geometry())
            ring = theme.qcolor(th.tokens["accent"])
            ring.setAlphaF(0.35)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(ring)
            p.drawRoundedRect(r.adjusted(-6, -6, 6, 6), 22, 22)
            p.setBrush(th.solid("card", "content"))
            p.drawRoundedRect(r, 16, 16)
        p.end()

    def keyPressEvent(self, e) -> None:  # noqa: N802
        if e.key() == Qt.Key.Key_Space:
            self.record.emit()
        elif e.key() == Qt.Key.Key_Escape:
            self.cancel.emit()
        else:
            super().keyPressEvent(e)


class LookHere:
    """Shows the overlay on one of this computer's screens and dims the rest."""

    def __init__(self, on_record, on_cancel):
        self.on_record, self.on_cancel = on_record, on_cancel
        self.layers: list[_Layer] = []

    def show(self, screen_id: str, step: str, recording: bool = False) -> bool:
        """True if the screen is one of this computer's and the overlay is up."""
        target = qscreen_for(screen_id)
        self.hide()
        if target is None:
            return False
        for s in QGuiApplication.screens():
            layer = _Layer(s, s is target)
            layer.record.connect(self.on_record)
            layer.cancel.connect(self.on_cancel)
            if s is target:
                layer.eyebrow.setText(step.upper())
                if recording:
                    layer.title.setText("Keep looking…")
                    layer.text.setText("Recording. Hold still for a moment.")
                    layer.record_button.setEnabled(False)
            layer.show()
            if s is target:
                layer.raise_()
                layer.activateWindow()
                layer.setFocus()
            self.layers.append(layer)
        log.debug("look-here overlay on %s", screen_id)
        return True

    def hide(self) -> None:
        for layer in self.layers:
            layer.hide()
            layer.deleteLater()
        self.layers = []
