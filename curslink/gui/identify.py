"""Which monitor is which: a big number and name on every screen for a few
seconds, like Windows' Identify, so you can match the real monitors on your
desk to the screens on the Layout page.

"Detect my screens" shows it on this computer's monitors and, through the
host, on every connected paired computer (an ``identify`` frame, see
:func:`curslink.app.identify_command`). A payload is::

    {"screens": [{"screen": "screen2", "os": [2560, 0, 2560, 1440],
                  "number": 2, "title": "Kyle's Mac", "detail": "screen2 · 2560×1440"}],
     "seconds": 4}

Click-through and never takes focus; it fades away on its own.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QPropertyAnimation, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from curslink.gui import theme
from curslink.gui.overlay import find_qscreen

log = logging.getLogger(__name__)

SECONDS = 4.0
MAX_SECONDS = 15.0
FADE_MS = 450
BORDER = 6


class _IdentifyWindow(QWidget):
    def __init__(self, qscreen, cards: list[dict]):
        flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
                 | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.WindowTransparentForInput)
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setGeometry(qscreen.geometry())
        # Two layout screens can land on one monitor (an agent that reports
        # one screen for two): show each card, side by side.
        self.cards = cards
        self.fade = QPropertyAnimation(self, b"windowOpacity", self)

    def paintEvent(self, _e) -> None:
        th = theme.current()
        accent = th.color("accent")
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # The screen's edge in the accent colour, so you see which monitor
        # it is from across the desk, whatever's on it.
        edge = QRectF(self.rect()).adjusted(BORDER / 2, BORDER / 2, -BORDER / 2, -BORDER / 2)
        p.setPen(QPen(accent, BORDER))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(edge)

        short = min(self.width(), self.height())
        big = QFont()
        big.setPixelSize(max(72, round(short * 0.28)))
        big.setWeight(QFont.Weight.Bold)
        name = QFont()
        name.setPixelSize(max(18, round(short * 0.035)))
        name.setWeight(QFont.Weight.DemiBold)
        small = QFont()
        small.setPixelSize(max(13, round(short * 0.02)))
        mb, mn, ms = QFontMetricsF(big), QFontMetricsF(name), QFontMetricsF(small)
        pad = mn.height()

        sizes = []
        for card in self.cards:
            w = max(mb.horizontalAdvance(card["number"]), mn.horizontalAdvance(card["title"]),
                    ms.horizontalAdvance(card["detail"])) + 2 * pad
            h = mb.height() + mn.height() + (ms.height() * 1.4 if card["detail"] else 0) + 1.6 * pad
            sizes.append((w, h))
        gap = pad
        total = sum(w for w, _ in sizes) + gap * (len(sizes) - 1)
        x = (self.width() - total) / 2
        if th.dark:
            fill, ink, ink2 = QColor(18, 22, 30, 225), QColor(255, 255, 255), QColor(255, 255, 255, 170)
        else:
            fill, ink, ink2 = QColor(255, 255, 255, 235), QColor(20, 24, 32), QColor(20, 24, 32, 160)
        for card, (w, h) in zip(self.cards, sizes):
            box = QRectF(x, (self.height() - h) / 2, w, h)
            path = QPainterPath()
            path.addRoundedRect(box, pad, pad)
            p.setPen(QPen(accent, 3))
            p.setBrush(fill)
            p.drawPath(path)
            y = box.top() + 0.6 * pad
            p.setFont(big)
            p.setPen(accent)
            p.drawText(QRectF(box.left(), y, w, mb.height()), Qt.AlignmentFlag.AlignCenter, card["number"])
            y += mb.height()
            p.setFont(name)
            p.setPen(ink)
            p.drawText(QRectF(box.left(), y, w, mn.height()), Qt.AlignmentFlag.AlignCenter, card["title"])
            y += mn.height()
            if card["detail"]:
                p.setFont(small)
                p.setPen(ink2)
                p.drawText(QRectF(box.left(), y + ms.height() * 0.2, w, ms.height()), Qt.AlignmentFlag.AlignCenter,
                           card["detail"])
            x += w + gap
        p.end()

    def fade_out(self) -> None:
        self.fade.stop()
        self.fade.setDuration(FADE_MS)
        self.fade.setStartValue(self.windowOpacity())
        self.fade.setEndValue(0.0)
        self.fade.start()


def cards_for(payload: dict) -> list[tuple[object, dict]]:
    """(QScreen, card) for each screen in ``payload`` that's on this computer."""
    out = []
    for entry in payload.get("screens", []) if isinstance(payload.get("screens"), list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("screen"), str):
            continue
        os_rect = entry.get("os")
        if not (isinstance(os_rect, list) and len(os_rect) == 4
                and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in os_rect)):
            os_rect = None
        qscreen = find_qscreen(entry["screen"], os_rect)
        if qscreen is None:
            continue
        number = entry.get("number")
        out.append((qscreen, {
            "number": str(number) if isinstance(number, (int, str)) and str(number) else "•",
            "title": str(entry.get("title") or "")[:80],
            "detail": str(entry.get("detail") or "")[:80],
        }))
    return out


class Identify:
    """The cards on this computer's monitors. ``show`` again replaces them;
    they fade by themselves after ``seconds``."""

    def __init__(self):
        self.windows: list[_IdentifyWindow] = []
        self.timer = QTimer()
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._fade)

    def show(self, payload: dict) -> int:
        """Put the cards up; how many monitors got one."""
        self.hide()
        if payload.get("hide"):
            return 0
        by_screen: dict[int, tuple[object, list[dict]]] = {}
        for qscreen, card in cards_for(payload):
            by_screen.setdefault(id(qscreen), (qscreen, []))[1].append(card)
        for qscreen, cards in by_screen.values():
            window = _IdentifyWindow(qscreen, cards)
            window.show()
            window.raise_()
            self.windows.append(window)
        try:
            seconds = float(payload.get("seconds", SECONDS))
        except (TypeError, ValueError):
            seconds = SECONDS
        seconds = max(0.5, min(MAX_SECONDS, seconds))
        if self.windows:
            self.timer.start(round(seconds * 1000))
            log.info("identify cards on %d monitor(s)", len(self.windows))
        return len(self.windows)

    def _fade(self) -> None:
        fading, self.windows = self.windows, []
        for window in fading:
            window.fade_out()
        QTimer.singleShot(FADE_MS + 50, lambda: self._close(fading))

    @staticmethod
    def _close(windows: list[_IdentifyWindow]) -> None:
        for window in windows:
            window.hide()
            window.deleteLater()

    def hide(self) -> None:
        self.timer.stop()
        self._close(self.windows)
        self.windows = []

    @property
    def showing(self) -> bool:
        return bool(self.windows)
