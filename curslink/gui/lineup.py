"""Auto line-up on screen: the patterns on every monitor, and the steps on
the host's Layout page (see curslink.lineup and curslink.lineup_scene for
the maths).

The host's Layout page starts a :class:`LineupFlow`: it puts the patterns on
this computer's monitors (:class:`Markers`) and, through the host service, on
every connected paired computer's (a ``lineup`` frame, which they show with
their own :class:`Markers`), and asks every connected phone with the
Curslink app for its camera (``lineup_camera``, live). The phone sends frame
after frame as it's moved about; each is read on a worker thread, the desk
is solved again with every new view, and a small window on top shows the
layout moving into place and says what to do next (the same goes back to
the phone's screen, ``lineup_live``). When every screen's place is sure it
ends by itself, and the page shows the result to check and save. A photo
from any other camera (or an older phone app) can be used instead: one
view, so less sure.

A marker payload (one computer's) is::

    {"screens": [{"screen": "main", "os": [0, 0, 1920, 1080], "number": 1,
                  "title": "Kyle's Mac", "markers": [[bits, x, y, w, h], ...],
                  "sets": [markers, markers, ...], "state": "placing"}],
     "seconds": 300, "period": 320}

``sets`` (newer hosts): the patterns change, set after set every ``period``
ms, so the camera gets points all over the screen from wherever it's held
(``markers`` is the first set, all an older Curslink shows). ``state``:
how the scan is going for that screen ("locked": it's measured, shown as a
tick). The same payload sent again with only the states changed updates
the windows in place.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid

from PySide6.QtCore import QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

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
        self.shown = 0  # which set of patterns is up

    def step(self) -> None:
        """On to the next set of patterns."""
        self.shown += 1
        self.update()

    def set_states(self, screens: list[dict]) -> None:
        for mine, new in zip(self.screens, screens):
            mine["state"] = new.get("state")
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        w, h = self.width(), self.height()
        # Dark behind, so the white around each pattern stands out in the photo.
        p.fillRect(self.rect(), QColor(16, 18, 24, 235))
        first = True
        for screen in self.screens:
            sets = screen.get("sets") or [screen["markers"]]
            first = first and self.shown % len(sets) == 0
            for marker in sets[self.shown % len(sets)]:
                bits, fx, fy, fw, fh = marker
                draw_marker(p, int(bits), QRectF(fx * w, fy * h, fw * w, fh * h))
        locked = bool(self.screens) and all(s.get("state") == "locked" for s in self.screens)
        if locked:
            # Measured: a green edge all round, seen from across the desk.
            edge = max(6, round(min(w, h) * 0.012))
            p.setPen(QPen(STATE_COLOURS["locked"], edge))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(QRectF(edge / 2, edge / 2, w - edge, h - edge))
        if not first:
            # The other sets use the middle too: the number waits for the first one.
            p.end()
            return
        th = theme.current()
        accent = th.color("accent")
        short = min(w, h)
        big, small = QFont(), QFont()
        big.setPixelSize(max(40, round(short * 0.10)))
        big.setWeight(QFont.Weight.Bold)
        small.setPixelSize(max(14, round(short * 0.024)))
        small.setWeight(QFont.Weight.DemiBold)
        mb, ms = QFontMetricsF(big), QFontMetricsF(small)
        lines = [s for s in self.screens if s.get("number") or s.get("title")]
        if lines:
            s = lines[0]
            number, title = str(s.get("number") or ""), str(s.get("title") or "")[:80]
            note = ("Measured \u2713" if locked else "Lining up: keep these patterns in the camera's view")
            bw = max(mb.horizontalAdvance(number), ms.horizontalAdvance(title), ms.horizontalAdvance(note)) + 2 * ms.height()
            bh = mb.height() + 2.6 * ms.height() + ms.height()
            # In the middle, clear of the patterns along the edges (the top and bottom
            # middle ones reach 0.19 of a 16:9 screen's height in).
            box = QRectF((w - bw) / 2, (h - bh) / 2, bw, bh)
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
            p.setPen(STATE_COLOURS["locked"] if locked else QColor(255, 255, 255, 170))
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
        sets = []
        if isinstance(entry.get("sets"), list):
            for one in entry["sets"][:16]:
                good = [m for m in one if _numbers(m, 5)] if isinstance(one, list) else []
                if good:
                    sets.append(good)
        out.append((qscreen, {"markers": markers, "sets": sets or [markers], "number": entry.get("number"),
                              "title": entry.get("title"),
                              "state": entry.get("state") if isinstance(entry.get("state"), str) else None}))
    return out


class Markers:
    """The patterns on this computer's monitors until hidden (or ``seconds``)."""

    def __init__(self):
        self.windows: list[_MarkerWindow] = []
        self.timer = QTimer()
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.hide)
        self.cycle = QTimer()  # on to each screen's next set of patterns
        self.cycle.timeout.connect(self._step)
        self.what = None  # what's showing, without the states

    def _step(self) -> None:
        for window in self.windows:
            window.step()

    def show(self, payload: dict) -> int:
        if payload.get("hide"):
            self.hide()
            return 0
        found = screens_of(payload)
        by_screen: dict[int, tuple[object, list[dict]]] = {}
        for qscreen, screen in found:
            by_screen.setdefault(id(qscreen), (qscreen, []))[1].append(screen)
        what = [(id(q), [{k: v for k, v in s.items() if k != "state"} for s in ss]) for q, ss in by_screen.values()]
        if self.windows and what == self.what:
            # The same patterns, only how it's going has changed: no need to start again.
            for window, (_q, screens) in zip(self.windows, by_screen.values()):
                window.set_states(screens)
            return len(self.windows)
        self.hide()
        self.what = what
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
            most = max(len(s.get("sets") or [1]) for w in self.windows for s in w.screens)
            if most > 1:
                try:
                    period = float(payload.get("period", 320))
                except (TypeError, ValueError):
                    period = 320.0
                self.cycle.start(round(max(120.0, min(5000.0, period))))
            log.info("line-up patterns on %d monitor(s), %d set(s) each at most", len(self.windows), most)
        return len(self.windows)

    def hide(self) -> None:
        self.timer.stop()
        self.cycle.stop()
        self.what = None
        for window in self.windows:
            window.hide()
            window.deleteLater()
        self.windows = []

    @property
    def showing(self) -> bool:
        return bool(self.windows)


STATE_COLOURS = {"unseen": QColor(120, 130, 150), "seen": QColor(120, 130, 150),
                 "placing": QColor(255, 180, 84), "locked": QColor(79, 214, 164)}


class LayoutPreview(QWidget):
    """The layout as the camera has it so far, small: each screen where the
    last solve put it, amber while it's still being placed, green once sure."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(380, 130)
        self.rects: list[tuple[str, float, float, float, float, str]] = []  # number, x, y, w, h, state

    def show_layout(self, rects: list[tuple[str, float, float, float, float, str]]) -> None:
        self.rects = rects
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        back = QPainterPath()
        back.addRoundedRect(area, 8, 8)
        p.fillPath(back, QColor(16, 20, 28))
        if not self.rects:
            p.setPen(QColor(150, 160, 178))
            p.drawText(area, Qt.AlignmentFlag.AlignCenter, "The layout appears here as it's measured")
            p.end()
            return
        x0 = min(r[1] for r in self.rects)
        y0 = min(r[2] for r in self.rects)
        x1 = max(r[1] + r[3] for r in self.rects)
        y1 = max(r[2] + r[4] for r in self.rects)
        pad = 10.0
        k = min((area.width() - 2 * pad) / max(1.0, x1 - x0), (area.height() - 2 * pad) / max(1.0, y1 - y0))
        ox = area.left() + (area.width() - (x1 - x0) * k) / 2
        oy = area.top() + (area.height() - (y1 - y0) * k) / 2
        font = QFont()
        font.setBold(True)
        p.setFont(font)
        for number, x, y, w, h, state in self.rects:
            box = QRectF(ox + (x - x0) * k, oy + (y - y0) * k, w * k, h * k).adjusted(1, 1, -1, -1)
            colour = STATE_COLOURS.get(state, STATE_COLOURS["unseen"])
            path = QPainterPath()
            path.addRoundedRect(box, 4, 4)
            p.fillPath(path, QColor(34, 42, 58, 235))
            p.setPen(QPen(colour, 2))
            p.drawPath(path)
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, number)
        p.end()


class LineupDialog(QDialog):
    """What to do while the patterns are up: on top of them, in the middle of this screen."""

    choose = Signal()
    again = Signal()
    use = Signal()

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Dialog | Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle("Line up screens")
        self.setModal(False)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(10)
        self.title = QLabel("Scan your desk with your phone")
        f = self.title.font()
        f.setPointSizeF(f.pointSizeF() * 1.35)
        f.setBold(True)
        self.title.setFont(f)
        lay.addWidget(self.title)
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setMinimumWidth(380)
        lay.addWidget(self.text)
        # Live: how far along, and the layout as it's measured.
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        self.bar.hide()
        lay.addWidget(self.bar)
        self.preview = LayoutPreview()
        self.preview.hide()
        lay.addWidget(self.preview)
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.detail.setObjectName("small")
        self.detail.hide()
        lay.addWidget(self.detail)
        row = QHBoxLayout()
        self.again_button = QPushButton("Ask the phone again")
        self.again_button.clicked.connect(self.again.emit)
        self.use_button = QPushButton("Use it as it is")
        self.use_button.setToolTip("Stop scanning and take the layout as measured so far.")
        self.use_button.clicked.connect(self.use.emit)
        self.use_button.hide()
        self.choose_button = QPushButton("Choose a photo…")
        self.choose_button.clicked.connect(self.choose.emit)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        row.addWidget(self.again_button)
        row.addStretch(1)
        row.addWidget(self.use_button)
        row.addWidget(self.choose_button)
        row.addWidget(cancel)
        lay.addLayout(row)

    def say(self, title: str, text: str, busy: bool = False, again: bool = False) -> None:
        self.title.setText(title)
        self.text.setText(text)
        self.choose_button.setEnabled(not busy)
        self.again_button.setVisible(again and not busy)
        self.adjustSize()

    def show_live(self, progress: float, rects: list, detail: str, usable: bool) -> None:
        """The live line-up's state: the bar, the little layout, the numbers."""
        first = not self.bar.isVisible()
        self.bar.show()
        self.preview.show()
        self.bar.setValue(round(100 * min(1.0, max(0.0, progress))))
        self.preview.show_layout(rects)
        self.detail.setText(detail)
        self.detail.setVisible(bool(detail))
        self.use_button.setVisible(usable)
        self.again_button.hide()
        self.choose_button.hide()  # the phone's doing it
        if first:
            self.adjustSize()


def describe(result, names: dict[str, str]) -> str:
    """A line on how the screens stand, from a curslink.lineup.Result: what
    one photo couldn't tell."""
    parts = []
    for key in result.found:
        st = result.stance.get(key) or {}
        if key == result.reference:
            continue
        yaw, pitch = st.get("yaw", 0.0), st.get("pitch", 0.0)
        bits = []
        if abs(yaw) >= 6:
            bits.append(f"turned {abs(yaw):.0f}° to the {'right' if yaw > 0 else 'left'}")
        if abs(pitch) >= 8:
            bits.append(f"tipped {abs(pitch):.0f}° {'back' if pitch > 0 else 'forward'}")
        if bits:
            parts.append(f"{names.get(key, key)} is {' and '.join(bits)}")
    return "; ".join(parts)


class LineupFlow(QObject):
    """One auto line-up, from putting the patterns up to the result.

    ``screens``: every screen in the layout, ``{"key", "device", "screen",
    "x", "y", "width", "height", "os", "number", "title", "local", "phone"}``.
    ``send(line)`` talks to the host service. ``finished`` gives a
    curslink.lineup.Result, or None if it was cancelled.
    """

    finished = Signal(object)
    _analysed = Signal(object, str)  # (Result or None, error), from the worker thread
    _status = Signal(object)  # a live frame was read: how it's going (a dict), from the worker thread

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
        self.dialog.use.connect(self.use_now)
        self._analysed.connect(self._done)
        self._status.connect(self._on_status)
        self.planned: dict[str, dict] = {}
        self.phones: list[dict] = []
        self.busy = False
        self.over = False
        # Live: frames wait here for the worker thread, which owns ``live``.
        self.frames: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.live = None  # curslink.lineup.Live
        self.latest = None  # the last Result it gave
        self.live_device: str | None = None
        self.ending = False
        self.shown_local: list[dict] = []
        self.shown_remote: dict[str, dict] = {}
        self.locked: set[str] = set()
        self.period = 320

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
                     "title": s.get("title", ""), "markers": p["markers"], "sets": p.get("sets") or [p["markers"]],
                     "key": s["key"]}
            if s.get("local"):
                local.append(entry)
            else:
                remote.setdefault(s["device"], {"screens": [], "seconds": SECONDS,
                                                "period": lineup.PERIOD_MS})["screens"].append(entry)
        import json

        self.shown_local, self.shown_remote = local, remote

        # This computer's monitors' real sizes (the others' come with lineup_sizes).
        try:
            from curslink.screensize import physical_sizes

            self.take_sizes({s["device"]: physical_sizes() for s in shown if s.get("local")})
        except Exception:  # noqa: BLE001 - only a help to the sums
            log.debug("couldn't read this computer's monitor sizes", exc_info=True)
        self.markers.show({"screens": local, "seconds": SECONDS, "period": lineup.PERIOD_MS})
        self.send("lineup " + json.dumps({"devices": remote}, separators=(",", ":")))
        self.dialog.say("Scan your desk with your phone", "Asking your phone…")
        self.dialog.show()
        # In the middle of the screen, over its number: clear of its patterns.
        self.centre_dialog()
        self.dialog.raise_()
        self.dialog.activateWindow()
        self.ask_phones()
        return True

    def show_states(self, states: dict[str, str]) -> None:
        """Each screen shows how the scan is going for it (a tick once it's
        measured): the same patterns again with the states, to the computers
        whose screens' states changed."""
        import json

        locked = {k for k, v in states.items() if v == "locked"}
        if locked == self.locked:
            return
        changed = locked ^ self.locked
        self.locked = locked

        def with_states(entries: list[dict]) -> list[dict]:
            return [{**e, "state": "locked" if e.get("key") in locked else "placing"} for e in entries]

        if any(e.get("key") in changed for e in self.shown_local):
            self.markers.show({"screens": with_states(self.shown_local), "seconds": SECONDS,
                               "period": self.period})
        remote = {d: {**p, "screens": with_states(p["screens"])} for d, p in self.shown_remote.items()
                  if any(e.get("key") in changed for e in p["screens"])}
        if remote:
            self.send("lineup " + json.dumps({"devices": remote}, separators=(",", ":")))

    def centre_dialog(self) -> None:
        area = self.dialog.screen().geometry() if self.dialog.screen() is not None else None
        if area is not None:
            self.dialog.adjustSize()
            self.dialog.move(area.center() - self.dialog.rect().center())

    def take_sizes(self, devices: dict) -> None:
        """Monitors' real sizes, per computer (as curslink.screensize gives them)."""
        from curslink.screensize import match

        for s in self.screens:
            sizes = devices.get(s["device"])
            if not isinstance(sizes, list) or not sizes:
                continue
            try:
                os_x, os_y, w, h = (int(v) for v in s["os"])
                mm = match([m for m in sizes if isinstance(m, dict)], os_x, os_y, w, h)
            except (TypeError, ValueError, KeyError):
                mm = None
            if mm is not None:
                s["width_mm"], s["height_mm"] = mm

    def ask_phones(self) -> None:
        import json

        self.send("lineup_camera " + json.dumps({"id": self.id, "live": True}))

    def on_event(self, event: dict) -> None:
        kind = event.get("event")
        if self.over or event.get("id") not in (None, self.id):
            return
        if kind == "lineup_sizes":
            if isinstance(event.get("devices"), dict) and self.live is None:
                self.take_sizes(event["devices"])
        elif kind == "lineup_camera":
            self.phones = [d for d in event.get("devices", []) if isinstance(d, dict)]
            self.say_waiting()
        elif kind == "lineup_photo":
            live_frame = isinstance(event.get("seq"), int)
            if event.get("cancelled"):
                self.dialog.say("Scan your desk with your phone",
                                "It was cancelled on the phone. Ask it again, or choose a photo "
                                "taken with another camera.", again=bool(self.phones))
            elif event.get("done"):
                self.use_now()
            elif event.get("error"):
                if live_frame:  # one frame lost: on with the next
                    self.send_live(event.get("device"), {"seq": event["seq"]})
                else:
                    self.dialog.say("That photo didn't arrive", f"{event['error']}. Ask the phone again.", again=True)
            elif isinstance(event.get("path"), str):
                if live_frame:
                    self.live_frame(event)
                else:
                    self.read_file(event["path"], delete=True)

    def say_waiting(self) -> None:
        if self.worker is not None:
            return  # frames are already coming
        if self.phones:
            names = ", ".join(str(d.get("name") or d.get("id")) for d in self.phones)
            self.dialog.say("Scan your desk with your phone",
                            f"The camera is opening on {names}. Sit where you usually sit and hold the phone up in "
                            "front of your eyes with every screen in the picture. The patterns move around each "
                            "screen to measure it; a screen shows a green edge once it's done. The layout appears "
                            "here and keeps adjusting until every screen is lined up.\n\n"
                            "No Curslink on your phone? Take a photo of the desk with any camera and choose it here.",
                            again=True)
        else:
            self.dialog.say("Take a photo of your desk",
                            "No phone with the Curslink app is connected, so take a photo with any camera and "
                            "choose it here (AirDrop or copy it over first). Stand back far enough to get every "
                            "screen in, and make sure the patterns are sharp. One photo is less sure than a "
                            "scan with the app: it can't tell how far away each screen is.")

    # -- live: frame after frame from the phone --------------------------------
    def live_frame(self, event: dict) -> None:
        if self.busy or self.ending:
            _remove(event["path"])
            return
        if self.worker is None:
            self.worker = threading.Thread(target=self._work, name="curslink-lineup-live", daemon=True)
            self.worker.start()
            self.dialog.say("Scanning your desk", "Reading the patterns…")
        if self.frames.qsize() >= 2:
            # Behind (it shouldn't be: the phone waits for each frame's answer): skip this one.
            _remove(event["path"])
            self.send_live(event.get("device"), {"seq": event["seq"]})
            return
        self.frames.put((event["path"], event["seq"], event.get("focal"), event.get("device")))

    def _work(self) -> None:
        """The worker thread: read each frame, solve again, say how it's going."""
        import cv2
        import numpy as np

        from curslink import lineup

        numbers = {s["key"]: s.get("number") for s in self.screens}
        # For the log (and so a diagnostics file): how the scan went, every couple of seconds.
        began = said = time.monotonic()
        count = size = 0
        spent = 0.0
        while True:
            item = self.frames.get()
            if item is None:
                if self.live is not None:
                    log.info("line-up scan ended after %.1fs: %s", time.monotonic() - began, _progress_line(self.live))
                return
            path, seq, focal, device = item
            status = {"seq": seq, "device": device}
            t0 = time.monotonic()
            try:
                with open(path, "rb") as f:
                    data = f.read()
                gray = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
                if gray is not None:
                    if self.live is None:
                        self.live = lineup.Live(self.screens, self.planned, self.reference)
                        log.info("line-up scan: first frame %dx%d, %d kB, focal %s", gray.shape[1], gray.shape[0],
                                 len(data) // 1024, focal)
                    self.live.frame(gray, focal)
                    self.live.update()
                    count += 1
                    size += len(data)
                    spent += time.monotonic() - t0
                    now = time.monotonic()
                    if now - said >= 2.0 or self.live.aligned:
                        log.info("line-up scan %.1fs: %d frames (%.1f/s, %d kB each, %.0f ms to read and solve); %s",
                                 now - began, count, count / max(0.001, now - said), size // count // 1024,
                                 1000 * spent / count, _progress_line(self.live))
                        said, count, size, spent = now, 0, 0, 0.0
            except Exception:  # noqa: BLE001 - a frame OpenCV chokes on: on with the next
                log.warning("couldn't read a line-up frame", exc_info=True)
            finally:
                _remove(path)
            live = self.live
            if live is not None:
                status.update(progress=live.progress(), hint=live.hint(numbers), states=live.states(),
                              result=live.result, aligned=live.aligned, usable=live.usable,
                              frames=live.frames)
            self._status.emit(status)

    def send_live(self, device, data: dict) -> None:
        import json

        if device:
            self.send("lineup_live " + json.dumps({"device": device, "id": self.id, **data}, separators=(",", ":")))

    def _on_status(self, status: dict) -> None:
        if self.over or self.ending:
            return
        self.live_device = status.get("device") or self.live_device
        states = status.get("states") or {}
        result = status.get("result")
        if result is not None:
            self.latest = result
        info = {s["key"]: s for s in self.screens}
        numbers = [[int(info[k]["number"]), v] for k, v in states.items()
                   if k in info and isinstance(info[k].get("number"), int)]
        aligned = bool(status.get("aligned"))
        self.send_live(status.get("device"), {"seq": status.get("seq"), "progress": status.get("progress", 0.0),
                                              "hint": status.get("hint", ""), "screens": numbers,
                                              **({"done": True} if aligned else {})})
        rects = []
        if result is not None:
            for key, (x, y) in result.positions.items():
                s = info.get(key)
                if s is not None and key in result.found:
                    rects.append((str(s.get("number") or ""), x, y, s["width"], s["height"], states.get(key, "placing")))
        detail = ""
        if result is not None:
            worst = max((e for e in result.errors.values()), default=0.0)
            detail = (f"{result.views} view{'s' if result.views != 1 else ''} kept, patterns fit to "
                      f"{result.rms:.1f} px, placed to ±{worst:.0f} px" if worst >= 1 else
                      f"{result.views} views kept, patterns fit to {result.rms:.1f} px, placed to under a pixel")
            how = describe(result, {s["key"]: f"screen {s.get('number')}" for s in self.screens})
            if how:
                detail += ". " + how[:1].upper() + how[1:] + "."
        self.show_states(states)
        self.dialog.say("Lined up" if aligned else "Scanning your desk", status.get("hint") or "Reading the patterns…")
        self.dialog.show_live(status.get("progress", 0.0), rects, detail, bool(status.get("usable")) and not aligned)
        if aligned and self.latest is not None:
            # A moment to see it's done, then the result.
            self.ending = True
            QTimer.singleShot(900, lambda: self._end(self.latest) if not self.over else None)

    def use_now(self) -> None:
        """Stop the scan and take the layout as measured so far."""
        if self.over or self.ending:
            return
        if self.latest is None or len(self.latest.found) < 2:
            self.dialog.say("Not enough yet", "At least two screens need to be in the camera's view together. "
                                              "Keep scanning, or cancel.", again=bool(self.phones))
            return
        self.send_live(self.live_device, {"progress": 1.0, "hint": "Stopped", "screens": [], "done": True})
        self._end(self.latest)

    # -- one photo, from any camera ---------------------------------------------
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
                _remove(path)
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
        if self.over:
            return
        self.over = True
        self.frames.put(None)  # the worker thread, if any, stops
        self.markers.hide()
        self.send('lineup {"hide":true}')
        self.dialog.blockSignals(True)
        self.dialog.hide()
        self.dialog.deleteLater()
        self.finished.emit(result)


def _progress_line(live) -> str:
    """Where a curslink.lineup.Live has got to, for the log."""
    result = live.result
    seen = ",".join(k for k in live.wanted if k in live.last_seen) or "none"
    if result is None:
        return f"{live.frames} frames in, no layout yet (in view: {seen}){'; ' + live.error if live.error else ''}"
    states = live.states()
    each = ", ".join(f"{k} {states.get(k, '?')} ±{result.errors[k]:.0f}px" if k in result.errors
                     else f"{k} {states.get(k, '?')}" for k in live.wanted)
    return (f"{live.frames} frames in, {result.views} views kept, fit {result.rms:.2f} px, asks ±{live.tolerance:.0f} px; "
            f"{each}; in view: {seen}; solves agree: {'yes' if live.steady() else 'no'}; "
            f"{'lined up' if live.aligned else 'settled' if live.settled else 'not settled'}"
            f"{'; ' + live.error if live.error else ''}")


def _remove(path: str) -> None:
    import os

    try:
        os.remove(path)
    except OSError:
        pass
