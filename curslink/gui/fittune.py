"""Fine-tuning an overlap by eye, on the mirror itself, for mirror mode.

Lining up with clicks (curslink.gui.fitcalib), or the screens' real sizes,
get close, but what looks right depends on where you sit: the screen in
front is nearer to you than the one behind. Here the mirror stays on while
you nudge it with the arrow keys and grow or shrink it with + and -, until
the picture on the screen in front carries on from the screen behind it.

Each press goes to the running service as a ``fit {...}`` command (see
curslink.mirror.apply_fit), which moves the mirror at once and answers
with a ``fit`` event saying where it is now; Save keeps that in the config
as a ``[[layout.fit]]``.
"""

from __future__ import annotations

import json

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

STEP = 1.0  # screen behind's pixels per arrow press
BIG_STEP = 10.0  # with Shift
GROW = 1.005  # per + or - press
BIG_GROW = 1.05


class FitTuneDialog(QDialog):
    """Nudge the mirror until it lines up. ``behind`` and ``front`` are
    ``{"key": "device/screen", "name"}``; ``saved_fit`` is the calibrated
    (x, y, scale) there is already, if any; ``send`` a command for the
    running service. ``saved`` gives the new (x, y, scale)."""

    saved = Signal(object)

    def __init__(self, behind: dict, front: dict, saved_fit: tuple[float, float, float] | None, send, parent=None,
                 start: dict | None = None):
        super().__init__(parent)
        self.behind, self.front, self.send = behind, front, send
        self.saved_fit = tuple(saved_fit) if saved_fit else None
        self.fit: tuple[float, float, float] | None = None  # as the service last said
        # ``start``: where to begin (a fit command, such as the one-click place); Save keeps it.
        self.moved = start is not None
        self._done = False
        self.setWindowTitle("Fine-tune overlap")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        lay = QVBoxLayout(self)
        intro = QLabel(f"Coloured lines and a ring now cross {behind['name']}, and the mirror on {front['name']} "
                       "draws them too. Sit where you normally do and adjust the mirror right there until every "
                       "line carries on straight past the bezel and the ring is round:\n\n"
                       "• drag it to move it (or with a finger, on a phone)\n• scroll or pinch on it to zoom\n"
                       "• drag a side or corner to stretch it\n\n"
                       "Then Save here.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.readout = QLabel("")
        lay.addWidget(self.readout)
        row = QHBoxLayout()
        reset = QPushButton("Start over")
        reset.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        reset.clicked.connect(self.start_over)
        cancel = QPushButton("Cancel")
        cancel.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        cancel.clicked.connect(self.reject)
        save = QPushButton("Save")
        save.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        save.setDefault(True)
        save.clicked.connect(self.accept)
        row.addWidget(reset)
        row.addStretch(1)
        row.addWidget(cancel)
        row.addWidget(save)
        lay.addLayout(row)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.command(hold=True, tune=True, **(start or {}))

    # -- changes ---------------------------------------------------------------
    def command(self, **data) -> None:
        data = {"behind": self.behind["key"], "front": self.front["key"], **data}
        self.send("fit " + json.dumps(data, separators=(",", ":")))

    def move_by(self, dx: float, dy: float, big: bool = False) -> None:
        step = BIG_STEP if big else STEP
        self.moved = True
        self.command(dx=dx * step, dy=dy * step)

    def grow_by(self, factor: float) -> None:
        self.moved = True
        self.command(grow=factor)

    def put_back(self, **extra) -> None:
        if self.saved_fit is None:
            self.command(reset=True, **extra)
        else:
            x, y, k = self.saved_fit
            self.command(x=x, y=y, scale=k, **extra)

    def start_over(self) -> None:
        self.moved = False
        self.put_back()

    def keyPressEvent(self, e) -> None:  # noqa: N802
        big = bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        moves = {Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0), Qt.Key.Key_Up: (0, -1), Qt.Key.Key_Down: (0, 1)}
        key = e.key()
        if key in moves:
            self.move_by(*moves[key], big=big)
        elif key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.grow_by(BIG_GROW if big else GROW)
        elif key in (Qt.Key.Key_Minus, Qt.Key.Key_Underscore):
            self.grow_by(1 / (BIG_GROW if big else GROW))
        else:
            super().keyPressEvent(e)

    def focusNextPrevChild(self, _next: bool) -> bool:  # noqa: N802 - arrows aren't for moving focus here
        return False

    def on_event(self, event: dict) -> None:
        """Service events (the Layout page passes them on): ``fit`` says where the mirror is now."""
        if event.get("event") != "fit" or (event.get("behind"), event.get("front")) != (self.behind["key"],
                                                                                         self.front["key"]):
            return
        try:
            self.fit = (float(event["x"]), float(event["y"]), float(event["scale"]))
        except (KeyError, TypeError, ValueError):
            return
        self.readout.setText(f"Position {self.fit[0]:.0f}, {self.fit[1]:.0f} · size {100 / self.fit[2]:.1f}%")

    # -- the end ---------------------------------------------------------------
    def stop(self) -> None:
        """The service stopped: nothing to show the mirror with."""
        self._done = True
        self.close()

    def accept(self) -> None:
        if not self._done:
            self._done = True
            self.command(hold=False)
            if self.moved and self.fit is not None:
                self.saved.emit(self.fit)
        super().accept()

    def reject(self) -> None:
        if not self._done:
            self._done = True
            self.put_back(hold=False)
        super().reject()
