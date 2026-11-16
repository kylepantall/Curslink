"""Line up from your seat: the window that walks through joining an overlap
by eye (curslink.seatalign).

The mirror stays on, with a ring drawn half on the screen behind and half
on the mirror at one spot of the edge they share. You look at it from where
you sit and nudge the mirror with the arrow keys until the ring closes,
then go on to the next spot. Each nudge goes to the running service as a
``fit {...}`` command (curslink.mirror.apply_fit), which moves the mirror
at once; Save keeps what you ended with, and with a desk a line-up solved
(curslink.seat) the joined spots also say where your eyes are.
"""

from __future__ import annotations

import json

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from curslink.seatalign import BIG_STEP, STEP, Align

TITLES = ("First corner", "Second corner", "Halfway between")
TEXTS = (
    "Sit how you normally sit and look at the red ring on {behind}. Half of it is drawn on {front}. "
    "Nudge with the arrow keys until it closes into one round ring and the lines through it run straight on.",
    "Now the red ring at the other end. The first one stays where you put it, so this sets the size and the tilt.",
    "Last one, halfway along. Left and right only: nudge until this ring closes too.",
)
ONE_SPOT = ("Sit how you normally sit and look at the red ring on {behind}. Half of it is drawn on {front}. "
            "Nudge with the arrow keys until it closes into one round ring.")


class SeatAlignDialog(QDialog):
    """``behind`` and ``front``: ``{"key": "device/screen", "name", "size": (w, h)}``;
    ``fit``: the overlap as saved (``x``, ``y``, ``scale`` and maybe
    ``quad``), or None if it never was: then it starts from where the
    service says the picture is; ``send``: a command for the running service; ``desk``: the
    desk a line-up solved (curslink.seat.load), if any.

    ``saved`` gives ``(fit command dict, joins)`` when it's done."""

    saved = Signal(object, object)

    def __init__(self, behind: dict, front: dict, fit: dict | None, send, parent=None, desk: dict | None = None):
        super().__init__(parent)
        self.behind, self.front, self.send, self.desk = behind, front, send, desk
        self.start = dict(fit) if fit else None
        self.align: Align | None = None
        if fit:
            self.align = Align.from_fit(float(fit["x"]), float(fit["y"]), float(fit["scale"]), fit.get("quad"),
                                        front["size"], behind["size"])
        self.adopted = False
        self._done = False
        self.setWindowTitle("Line up from your seat")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setMinimumWidth(420)
        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        self.steps = QLabel("")
        self.steps.setObjectName("muted")
        self.title = QLabel("")
        self.title.setObjectName("h2")
        self.text = QLabel("")
        self.text.setWordWrap(True)
        self.readout = QLabel("")
        self.readout.setObjectName("muted")
        for w in (self.steps, self.title, self.text, self.readout):
            lay.addWidget(w)
        keys = QLabel("←  ↑  ↓  →  nudge   ·   Shift moves faster   ·   Enter when the ring closes")
        keys.setObjectName("muted")
        keys.setWordWrap(True)
        lay.addWidget(keys)
        row = QHBoxLayout()
        self.cancel_button = QPushButton("Cancel")
        self.back_button = QPushButton("Back")
        self.next_button = QPushButton("")
        self.next_button.setObjectName("primary")
        for b in (self.cancel_button, self.back_button, self.next_button):
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # arrows and Enter are the window's
        self.cancel_button.clicked.connect(self.reject)
        self.back_button.clicked.connect(self.go_back)
        self.next_button.clicked.connect(self.go_next)
        row.addWidget(self.cancel_button)
        row.addStretch(1)
        row.addWidget(self.back_button)
        row.addWidget(self.next_button)
        lay.addLayout(row)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.command(hold=True, tune=True)
        self.show_step()

    # -- the service -----------------------------------------------------------
    def command(self, **extra) -> None:
        data = {"behind": self.behind["key"], "front": self.front["key"], **extra}
        if self.align is not None:
            data.update(self.align.command(), rings=self.align.rings(), keep=True)
        self.send("fit " + json.dumps(data, separators=(",", ":")))

    def on_event(self, event: dict) -> None:
        """Service events: ``fit`` says where the mirror is (it can be dragged by hand too)."""
        if event.get("event") != "fit" or (event.get("behind"), event.get("front")) != (self.behind["key"],
                                                                                         self.front["key"]):
            return
        try:
            x, y, scale = float(event["x"]), float(event["y"]), float(event["scale"])
        except (KeyError, TypeError, ValueError):
            return
        if self.align is None:
            # never lined up: it starts from where the service has the picture
            self.align = Align.from_fit(x, y, scale, None, self.front["size"], self.behind["size"])
            news = True
        else:
            news = self.align.follow(x, y, scale)
        if news:
            self.command()  # the rings go where the picture now is
            self.show_step()

    # -- the steps -------------------------------------------------------------
    def show_step(self) -> None:
        a = self.align
        if a is None:
            self.steps.setText("")
            self.title.setText("Finding the picture…")
            self.text.setText(f"Putting {self.behind['name']} on {self.front['name']} where it's hidden.")
            self.next_button.setText("The ring closes")
            self.next_button.setEnabled(False)
            self.back_button.setEnabled(False)
            return
        self.next_button.setEnabled(True)
        n = len(a.spots)
        if n == 0:
            self.steps.setText("")
            self.title.setText("These screens don't overlap enough")
            self.text.setText(f"Hardly any of {self.front['name']} is seen in front of {self.behind['name']}. "
                              "Move them in the layout so they overlap, then line up again.")
            self.next_button.setEnabled(False)
            self.back_button.setEnabled(False)
            return
        self.steps.setText(f"Spot {a.step + 1} of {n}")
        self.title.setText(TITLES[a.step] if n > 1 else "One spot")
        text = TEXTS[a.step] if n > 1 else ONE_SPOT
        self.text.setText(text.format(behind=self.behind["name"], front=self.front["name"]))
        self.back_button.setEnabled(a.step > 0)
        self.next_button.setText("Save" if a.step + 1 >= n else "The ring closes")
        x, y = a.seen(a.step)
        was = Align(a.began, a.front_size, a.behind_size)
        bx, by = was.fit.to_behind(a.front_size, *a.spots[a.step])
        self.readout.setText(f"Moved {x - bx:+.0f} across, {y - by:+.0f} down since you began")

    def nudge(self, dx: float, dy: float, big: bool = False) -> None:
        if self.align is None:
            return
        step = BIG_STEP if big else STEP
        self.align.nudge(dx * step, dy * step)
        self.command()
        self.show_step()

    def go_next(self) -> None:
        a = self.align
        if a is None or not a.spots:
            return
        if a.step == 1 and not self.adopted:
            # Two spots joined say where your eyes are; the desk as solved then
            # says how the picture is drawn out between them from there.
            self.adopted = True
            try:
                from curslink import seat

                a.adopt(seat.joined_shape(self.desk, self.behind["key"], self.front["key"], a.joins(2)))
            except Exception:  # noqa: BLE001 - a better start for the last spot, nothing more
                pass
        if a.next():
            self.command()
            self.show_step()
        else:
            self.accept()

    def go_back(self) -> None:
        if self.align is not None and self.align.back():
            self.command()
            self.show_step()

    def keyPressEvent(self, e) -> None:  # noqa: N802
        big = bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        moves = {Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0), Qt.Key.Key_Up: (0, -1), Qt.Key.Key_Down: (0, 1)}
        key = e.key()
        if key in moves:
            self.nudge(*moves[key], big=big)
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.go_next()
        elif key == Qt.Key.Key_Backspace:
            self.go_back()
        else:
            super().keyPressEvent(e)

    def focusNextPrevChild(self, _next: bool) -> bool:  # noqa: N802 - arrows aren't for moving focus here
        return False

    # -- the end ---------------------------------------------------------------
    def stop(self) -> None:
        """The service stopped: nothing to show the mirror with."""
        self._done = True
        self.close()

    def accept(self) -> None:
        if not self._done and self.align is None:
            return self.reject()
        if not self._done:
            self._done = True
            fit = self.align.command()
            data = {"behind": self.behind["key"], "front": self.front["key"], **fit, "keep": True, "hold": False}
            self.send("fit " + json.dumps(data, separators=(",", ":")))
            self.saved.emit(fit, self.align.joins())
        super().accept()

    def reject(self) -> None:
        if not self._done:
            self._done = True
            data = {"behind": self.behind["key"], "front": self.front["key"], "hold": False}
            if self.start:
                data.update(x=self.start["x"], y=self.start["y"], scale=self.start["scale"])
                if self.start.get("quad") is not None:
                    data["quad"] = list(self.start["quad"])
                    for k in ("eye", "depth", "mm"):
                        if self.start.get(k) is not None:
                            data[k] = self.start[k]
            else:
                data["reset"] = True
            self.send("fit " + json.dumps(data, separators=(",", ":")))
        super().reject()
