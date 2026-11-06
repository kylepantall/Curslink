"""The "Look here" overlay shown on a screen while calibrating (spec 5.3).

The screen to look at is covered edge to edge in the window tint, with a
pulsing target in the middle to look at, the step, and the buttons. The
host's other screens are dimmed, so the eye goes to the right place.
Space records, S skips, Esc cancels.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Property, QPointF, QPropertyAnimation, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from curslink.gui import theme
from curslink.gui.widgets import button, label

log = logging.getLogger(__name__)

RING = 96       # target ring diameter
DOT = 18        # dot in the middle
HALO = (10, 22)  # halo grows between these, px beyond the ring


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


def _tint() -> QColor:
    th = theme.current()
    if th.dark:
        return QColor(10, 14, 22, round(255 * 0.86))
    c = th.solid("content", "win")
    c.setAlphaF(0.90)
    return c


class Target(QWidget):
    """The ring and dot to look at, with a slow pulse."""

    def __init__(self):
        super().__init__()
        size = RING + 2 * HALO[1] + 4
        self.setFixedSize(size, size)
        self._t = 0.0
        self.anim = QPropertyAnimation(self, b"t", self)
        self.anim.setStartValue(0.0)
        self.anim.setKeyValueAt(0.5, 1.0)
        self.anim.setEndValue(0.0)
        self.anim.setDuration(1600)
        self.anim.setLoopCount(-1)
        self.anim.start()

    def _get(self) -> float:
        return self._t

    def _set(self, v: float) -> None:
        self._t = v
        self.update()

    t = Property(float, _get, _set)

    def paintEvent(self, _e) -> None:  # noqa: N802
        accent = theme.current().color("accent")
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QPointF(self.width() / 2, self.height() / 2)
        r = RING / 2
        halo = HALO[0] + (HALO[1] - HALO[0]) * self._t
        glow = QColor(accent)
        glow.setAlphaF(0.18 * (1 - self._t) + 0.06)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(glow)
        p.drawEllipse(c, r + halo, r + halo)
        soft = QColor(accent)
        soft.setAlphaF(0.08)
        p.setBrush(soft)
        p.setPen(QPen(accent, 4))
        p.drawEllipse(c, r, r)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(accent)
        p.drawEllipse(c, DOT / 2, DOT / 2)
        p.end()


class Progress(QWidget):
    """Dots for the steps: done in the good colour, the current one a bar."""

    def __init__(self, total: int, current: int, done: set[int]):
        super().__init__()
        self.total, self.current, self.done = total, current, done
        self.setFixedSize(max(1, total) * 16 + 22, 10)

    def paintEvent(self, _e) -> None:  # noqa: N802
        th = theme.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        x = 0.0
        for i in range(self.total):
            if i == self.current:
                p.setBrush(th.color("accent"))
                p.drawRoundedRect(QRectF(x, 1, 22, 8), 4, 4)
                x += 22 + 8
                continue
            p.setBrush(th.color("good") if i in self.done else th.color("ink3"))
            p.drawEllipse(QRectF(x, 1, 8, 8))
            x += 8 + 8
        p.end()


class _Layer(QWidget):
    record = Signal()
    skip = Signal()
    cancel = Signal()

    def __init__(self, screen, with_target: bool):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, not with_target)
        self.setGeometry(screen.geometry())
        self.with_target = with_target
        self.record_button = None
        if not with_target:
            return
        t = theme.current().tokens
        v = QVBoxLayout(self)
        v.setSpacing(0)
        v.addStretch(1)
        self.target = Target()
        v.addWidget(self.target, 0, Qt.AlignmentFlag.AlignHCenter)
        v.addSpacing(18)
        self.title = label("Look here")
        f = QFont(self.title.font())
        f.setPixelSize(30)
        f.setWeight(QFont.Weight.Bold)
        self.title.setFont(f)
        self.title.setStyleSheet(f"color: {t['ink']}; font-size: 30px; font-weight: 700;")
        self.text = label("Keep looking at the dot, then press Record.")
        self.text.setStyleSheet(f"color: {t['ink2']}; font-size: 14px;")
        for w in (self.title, self.text):
            w.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            v.addWidget(w)
            v.addSpacing(8)
        v.addSpacing(14)
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addStretch(1)
        self.record_button = button("Record", "primary", self.record.emit)
        self.skip_button = button("Skip this screen", None, self.skip.emit)
        cancel = button("Cancel", None, self.cancel.emit)
        for b in (self.record_button, self.skip_button, cancel):
            b.setMinimumWidth(110)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # Space records, whatever has focus
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        v.addSpacing(22)
        self.step = label("")
        self.step.setStyleSheet(f"color: {t['ink3']}; font-size: 12.5px;")
        self.step.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        v.addWidget(self.step)
        v.addSpacing(10)
        self.progress_row = QHBoxLayout()
        v.addLayout(self.progress_row)
        v.addSpacing(6)
        self.keys = label("Space records · S skips · Esc cancels")
        self.keys.setStyleSheet(f"color: {t['ink3']}; font-size: 12px;")
        self.keys.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        v.addSpacing(8)
        v.addWidget(self.keys)
        v.addStretch(1)

    def set_progress(self, total: int, current: int, done: set[int]) -> None:
        self.progress = Progress(total, current, done)
        self.progress_row.addStretch(1)
        self.progress_row.addWidget(self.progress)
        self.progress_row.addStretch(1)

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), _tint() if self.with_target else QColor(0, 0, 0, round(255 * 0.55)))
        p.end()

    def keyPressEvent(self, e) -> None:  # noqa: N802
        if e.key() == Qt.Key.Key_Space:
            self.record.emit()
        elif e.key() == Qt.Key.Key_Escape:
            self.cancel.emit()
        elif e.key() == Qt.Key.Key_S:
            self.skip.emit()
        else:
            super().keyPressEvent(e)


class LookHere:
    """Shows the overlay on one of this computer's screens and dims the rest."""

    def __init__(self, on_record, on_cancel, on_skip=None):
        self.on_record, self.on_cancel, self.on_skip = on_record, on_cancel, on_skip
        self.layers: list[_Layer] = []

    def show(self, screen_id: str, step: str, recording: bool = False,
             total: int = 0, current: int = 0, done: set[int] | None = None) -> bool:
        """True if the screen is one of this computer's and the overlay is up.
        ``step`` is the caption ("Step 2 of 3 · Work laptop")."""
        target = qscreen_for(screen_id)
        self.hide()
        if target is None:
            return False
        for s in QGuiApplication.screens():
            layer = _Layer(s, s is target)
            layer.record.connect(self.on_record)
            layer.cancel.connect(self.on_cancel)
            if self.on_skip is not None:
                layer.skip.connect(self.on_skip)
            if s is target:
                layer.step.setText(step)
                if total:
                    layer.set_progress(total, current, done or set())
                if self.on_skip is None:
                    layer.skip_button.hide()
                if recording:
                    layer.title.setText("Keep looking…")
                    layer.text.setText("Recording. Hold still for a moment.")
                    layer.record_button.setEnabled(False)
                    layer.skip_button.setEnabled(False)
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
