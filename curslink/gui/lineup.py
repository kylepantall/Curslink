"""Auto line-up on screen: the patterns on every monitor, and the steps on
the host's Layout page (see curslink.lineup for the maths).

The host's Layout page starts a :class:`LineupFlow`: it puts the patterns on
this computer's monitors (:class:`Markers`) and, through the host service, on
every connected paired computer's (a ``lineup`` frame, which they show with
their own :class:`Markers`), and asks every connected phone with the
Curslink app for a photo (``lineup_camera``). A small window on top says
what to do; a photo from any other camera can be chosen there instead. The
photo is read on a worker thread, and the page shows the result to check
and save.

A marker payload (one computer's) is::

    {"screens": [{"screen": "main", "os": [0, 0, 1920, 1080], "number": 1,
                  "title": "Kyle's Mac", "markers": [[bits, x, y, w, h], ...]}],
     "seconds": 300}
"""

from __future__ import annotations

import logging
import threading
import uuid

from PySide6.QtCore import QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QDialog, QFileDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from curslink.gui import theme
from curslink.gui.overlay import find_qscreen

log = logging.getLogger(__name__)

SECONDS = 300.0  # the patterns come down by themselves after this long
MAX_SECONDS = 900.0


def _numbers(values, n: int) -> bool:
    return (isinstance(values, list) and len(values) == n
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values))


class _MarkerWindow(QWidget):
    """One monitor: a dark veil, its patterns on white, its number in the middle."""

    def __init__(self, qscreen, screens: list[dict]):
        flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
                 | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.WindowTransparentForInput)
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setGeometry(qscreen.geometry())
        # Normally one; an agent reporting one monitor as two shows both sets.
        self.screens = screens

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        w, h = self.width(), self.height()
        # Dark behind, so the white around each pattern stands out in the photo.
        p.fillRect(self.rect(), QColor(16, 18, 24, 235))
        for screen in self.screens:
            for marker in screen["markers"]:
                bits, fx, fy, fw, fh = marker
                draw_marker(p, int(bits), QRectF(fx * w, fy * h, fw * w, fh * h))
        th = theme.current()
        accent = th.color("accent")
        short = min(w, h)
        big, small = QFont(), QFont()
        big.setPixelSize(max(48, round(short * 0.16)))
        big.setWeight(QFont.Weight.Bold)
        small.setPixelSize(max(15, round(short * 0.028)))
        small.setWeight(QFont.Weight.DemiBold)
        mb, ms = QFontMetricsF(big), QFontMetricsF(small)
        lines = [s for s in self.screens if s.get("number") or s.get("title")]
        if lines:
            s = lines[0]
            number, title = str(s.get("number") or ""), str(s.get("title") or "")[:80]
            note = "Lining up: keep this screen in the photo"
            bw = max(mb.horizontalAdvance(number), ms.horizontalAdvance(title), ms.horizontalAdvance(note)) + 2 * ms.height()
            bh = mb.height() + 2.6 * ms.height() + ms.height()
            box = QRectF((w - bw) / 2, h * 0.30 - bh / 2, bw, bh)
            path = QPainterPath()
            path.addRoundedRect(box, ms.height(), ms.height())
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(QPen(accent, 3))
            p.setBrush(QColor(18, 22, 30, 240))
            p.drawPath(path)
            y = box.top() + ms.height() / 2
            p.setFont(big)
            p.setPen(accent)
            p.drawText(QRectF(box.left(), y, bw, mb.height()), Qt.AlignmentFlag.AlignCenter, number)
            y += mb.height()
            p.setFont(small)
            p.setPen(QColor(255, 255, 255))
            p.drawText(QRectF(box.left(), y, bw, ms.height() * 1.3), Qt.AlignmentFlag.AlignCenter, title)
            p.setPen(QColor(255, 255, 255, 170))
            p.drawText(QRectF(box.left(), y + ms.height() * 1.3, bw, ms.height() * 1.3),
                       Qt.AlignmentFlag.AlignCenter, note)
        p.end()


def draw_marker(p: QPainter, bits: int, box: QRectF) -> None:
    """A 4x4 ArUco marker (curslink.lineup.marker_bits) filling ``box``, with
    a white border one cell wide around it (the quiet zone it's found by)."""
    p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    cw, ch = box.width() / 6, box.height() / 6
    p.fillRect(box.adjusted(-cw, -ch, cw, ch), QColor(255, 255, 255))
    p.fillRect(box, QColor(0, 0, 0))
    for r in range(4):
        for c in range(4):
            if bits >> (15 - (r * 4 + c)) & 1:
                p.fillRect(QRectF(box.left() + (c + 1) * cw, box.top() + (r + 1) * ch, cw, ch), QColor(255, 255, 255))


def screens_of(payload: dict) -> list[tuple[object, dict]]:
    """(QScreen, screen) for each screen in a marker payload that's on this computer."""
    out = []
    entries = payload.get("screens") if isinstance(payload.get("screens"), list) else []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("screen"), str):
            continue
        markers = [m for m in entry.get("markers", []) if _numbers(m, 5)] if isinstance(entry.get("markers"), list) else []
        if not markers:
            continue
        os_rect = entry.get("os") if _numbers(entry.get("os"), 4) else None
        qscreen = find_qscreen(entry["screen"], os_rect)
        if qscreen is None:
            continue
        out.append((qscreen, {"markers": markers, "number": entry.get("number"), "title": entry.get("title")}))
    return out


class Markers:
    """The patterns on this computer's monitors until hidden (or ``seconds``)."""

    def __init__(self):
        self.windows: list[_MarkerWindow] = []
        self.timer = QTimer()
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.hide)

    def show(self, payload: dict) -> int:
        self.hide()
        if payload.get("hide"):
            return 0
        by_screen: dict[int, tuple[object, list[dict]]] = {}
        for qscreen, screen in screens_of(payload):
            by_screen.setdefault(id(qscreen), (qscreen, []))[1].append(screen)
        for qscreen, screens in by_screen.values():
            window = _MarkerWindow(qscreen, screens)
            window.show()
            window.raise_()
            self.windows.append(window)
        try:
            seconds = float(payload.get("seconds", SECONDS))
        except (TypeError, ValueError):
            seconds = SECONDS
        if self.windows:
            self.timer.start(round(max(5.0, min(MAX_SECONDS, seconds)) * 1000))
            log.info("line-up patterns on %d monitor(s)", len(self.windows))
        return len(self.windows)

    def hide(self) -> None:
        self.timer.stop()
        for window in self.windows:
            window.hide()
            window.deleteLater()
        self.windows = []

    @property
    def showing(self) -> bool:
        return bool(self.windows)


class LineupDialog(QDialog):
    """What to do while the patterns are up: on top of them, in the middle of this screen."""

    choose = Signal()
    again = Signal()

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Dialog | Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle("Line up screens")
        self.setModal(False)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(10)
        self.title = QLabel("Take a photo of your desk")
        f = self.title.font()
        f.setPointSizeF(f.pointSizeF() * 1.35)
        f.setBold(True)
        self.title.setFont(f)
        lay.addWidget(self.title)
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setMinimumWidth(380)
        lay.addWidget(self.text)
        row = QHBoxLayout()
        self.again_button = QPushButton("Ask the phone again")
        self.again_button.clicked.connect(self.again.emit)
        self.choose_button = QPushButton("Choose a photo…")
        self.choose_button.clicked.connect(self.choose.emit)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        row.addWidget(self.again_button)
        row.addStretch(1)
        row.addWidget(self.choose_button)
        row.addWidget(cancel)
        lay.addLayout(row)

    def say(self, title: str, text: str, busy: bool = False, again: bool = False) -> None:
        self.title.setText(title)
        self.text.setText(text)
        self.choose_button.setEnabled(not busy)
        self.again_button.setVisible(again and not busy)
        self.adjustSize()


class LineupFlow(QObject):
    """One auto line-up, from putting the patterns up to the result.

    ``screens``: every screen in the layout, ``{"key", "device", "screen",
    "x", "y", "width", "height", "os", "number", "title", "local", "phone"}``.
    ``send(line)`` talks to the host service. ``finished`` gives a
    curslink.lineup.Result, or None if it was cancelled.
    """

    finished = Signal(object)
    _analysed = Signal(object, str)  # (Result or None, error), from the worker thread

    def __init__(self, screens: list[dict], send, reference: str | None, parent=None):
        super().__init__(parent)
        self.screens = screens
        self.send = send
        self.reference = reference
        self.id = uuid.uuid4().hex[:10]
        self.markers = Markers()
        self.dialog = LineupDialog()
        self.dialog.rejected.connect(self.cancel)
        self.dialog.choose.connect(self.choose_photo)
        self.dialog.again.connect(self.ask_phones)
        self._analysed.connect(self._done)
        self.planned: dict[str, dict] = {}
        self.phones: list[dict] = []
        self.busy = False
        self.over = False

    def start(self) -> bool:
        from curslink import lineup

        shown = [s for s in self.screens if not s.get("phone")]
        try:
            self.planned = lineup.plan([{"key": s["key"], "width": s["width"], "height": s["height"]} for s in shown])
        except ImportError:
            return False
        local, remote = [], {}
        for s in shown:
            p = self.planned.get(s["key"])
            if p is None:
                continue
            entry = {"screen": s["screen"], "os": s["os"], "number": s.get("number"),
                     "title": s.get("title", ""), "markers": p["markers"]}
            if s.get("local"):
                local.append(entry)
            else:
                remote.setdefault(s["device"], {"screens": [], "seconds": SECONDS})["screens"].append(entry)
        import json

        self.markers.show({"screens": local, "seconds": SECONDS})
        self.send("lineup " + json.dumps({"devices": remote}, separators=(",", ":")))
        self.dialog.say("Take a photo of your desk", "Asking your phone…")
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
        self.ask_phones()
        return True

    def ask_phones(self) -> None:
        import json

        self.send("lineup_camera " + json.dumps({"id": self.id}))

    def on_event(self, event: dict) -> None:
        kind = event.get("event")
        if self.over or event.get("id") not in (None, self.id):
            return
        if kind == "lineup_camera":
            self.phones = [d for d in event.get("devices", []) if isinstance(d, dict)]
            self.say_waiting()
        elif kind == "lineup_photo":
            if event.get("cancelled"):
                self.dialog.say("Take a photo of your desk",
                                "The photo was cancelled on the phone. Ask it again, or choose a photo "
                                "taken with another camera.", again=bool(self.phones))
            elif event.get("error"):
                self.dialog.say("That photo didn't arrive", f"{event['error']}. Ask the phone again.", again=True)
            elif isinstance(event.get("path"), str):
                self.read_file(event["path"], delete=True)

    def say_waiting(self) -> None:
        tips = ("Stand back far enough to get every screen in, hold the phone level, and make sure "
                "the patterns are sharp.")
        if self.phones:
            names = ", ".join(str(d.get("name") or d.get("id")) for d in self.phones)
            self.dialog.say("Take a photo of your desk",
                            f"The camera is opening on {names}. {tips}\n\n"
                            "No Curslink on your phone? Take the photo with any camera and choose it here.",
                            again=True)
        else:
            self.dialog.say("Take a photo of your desk",
                            f"No phone with the Curslink app is connected, so take the photo with any camera and "
                            f"choose it here (AirDrop or copy it over first). {tips}")

    def choose_photo(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(self.dialog, "Choose the photo of your desk", "",
                                                    "Photos (*.jpg *.jpeg *.png *.heic *.webp);;All files (*)")
        if path:
            self.read_file(path)

    def read_file(self, path: str, delete: bool = False) -> None:
        if self.busy:
            return
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as exc:
            self.dialog.say("Couldn't open that photo", str(exc), again=bool(self.phones))
            return
        finally:
            if delete:
                import os

                try:
                    os.remove(path)
                except OSError:
                    pass
        self.analyse(data)

    def analyse(self, data: bytes) -> None:
        self.busy = True
        self.dialog.say("Lining up…", "Finding each screen's patterns in the photo.", busy=True)

        def work() -> None:
            from curslink import lineup

            try:
                result = lineup.line_up(data, self.screens, self.planned, self.reference)
                self._analysed.emit(result, "")
            except lineup.LineupError as exc:
                self._analysed.emit(None, str(exc))
            except Exception as exc:  # a photo OpenCV chokes on
                log.warning("line-up failed", exc_info=True)
                self._analysed.emit(None, f"something went wrong reading the photo ({exc})")

        threading.Thread(target=work, name="curslink-lineup", daemon=True).start()

    def _done(self, result, error: str) -> None:
        self.busy = False
        if self.over:
            return
        if result is None:
            msg = error[:1].upper() + error[1:] if error else "That didn't work"
            self.dialog.say("Try another photo", msg + ".", again=bool(self.phones))
            return
        self._end(result)

    def cancel(self) -> None:
        if not self.over:
            import json

            self.send("lineup_camera " + json.dumps({"id": self.id, "cancel": True}))
            self._end(None)

    def _end(self, result) -> None:
        self.over = True
        self.markers.hide()
        self.send('lineup {"hide":true}')
        self.dialog.blockSignals(True)
        self.dialog.hide()
        self.dialog.deleteLater()
        self.finished.emit(result)
