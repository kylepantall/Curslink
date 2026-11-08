"""Fine-tuning an overlap by eye, for mirror mode.

Lining up with clicks (curslink.gui.fitcalib) gets close, but what looks
right depends on where you sit: the screen in front is nearer to you than
the one behind. Here the mirror stays on while you nudge it with the arrow
keys and grow or shrink it with + and -, until the picture on the screen in
front carries on from the screen behind it. Each change goes to the running
service at once (the ``fit {...}`` command, see curslink.mirror.apply_fit);
Save keeps it in the config as a ``[[layout.fit]]``.
"""

from __future__ import annotations

import json

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QDialog, QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

STEP = 1.0  # screen behind's pixels per arrow press
BIG_STEP = 10.0  # with Shift
GROW = 1.005  # per + or - press
BIG_GROW = 1.05
MIN_SCALE, MAX_SCALE = 0.1, 10.0


def nudge(fit: tuple[float, float, float], dx: float, dy: float) -> tuple[float, float, float]:
    """Move the picture on the screen in front by (dx, dy) of the screen
    behind's pixels: the front screen sits that much the other way on it."""
    x, y, k = fit
    return (x - dx, y - dy, k)


def grow(fit: tuple[float, float, float], factor: float, behind: tuple[float, float],
         front: tuple[float, float]) -> tuple[float, float, float]:
    """Make the picture ``factor`` times bigger, keeping the middle of the
    covered part where it is on the screen in front."""
    x, y, k = fit
    k2 = min(MAX_SCALE, max(MIN_SCALE, k / factor))
    out = []
    for o, size_b, size_f in ((x, behind[0], front[0]), (y, behind[1], front[1])):
        lo, hi = max(0.0, o), min(size_b, o + k * size_f)
        c = (lo + hi) / 2 if hi > lo else o + k * size_f / 2
        u = (c - o) / k  # that point, on the screen in front
        out.append(c - k2 * u)
    return (out[0], out[1], k2)


class FitTuneDialog(QDialog):
    """Nudge the mirror until it lines up. ``behind`` and ``front`` are
    ``{"key": "device/screen", "name", "size": (w, h)}``; ``start`` is the
    current (x, y, scale); ``send`` a command for the running service.
    ``saved`` gives the new (x, y, scale)."""

    saved = Signal(object)

    def __init__(self, behind: dict, front: dict, start: tuple[float, float, float], send, parent=None):
        super().__init__(parent)
        self.behind, self.front, self.send = behind, front, send
        self.start_fit = tuple(start)
        self.fit = tuple(start)
        self._done = False
        self.setWindowTitle("Fine-tune overlap")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        lay = QVBoxLayout(self)
        intro = QLabel(f"{front['name']} now mirrors {behind['name']} behind it. Sit where you normally do and "
                       "move the picture until it carries on from the screen behind.\n\n"
                       "Arrow keys move it, + and - make it bigger or smaller. Hold Shift for bigger steps.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        grid = QGridLayout()
        for text, row, col, slot in (("↑", 0, 1, lambda: self.move_by(0, -1)), ("←", 1, 0, lambda: self.move_by(-1, 0)),
                                     ("→", 1, 2, lambda: self.move_by(1, 0)), ("↓", 2, 1, lambda: self.move_by(0, 1)),
                                     ("−", 1, 4, lambda: self.grow_by(1 / GROW)), ("+", 1, 5, lambda: self.grow_by(GROW))):
            b = QPushButton(text)
            b.setAutoRepeat(True)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.clicked.connect(slot)
            grid.addWidget(b, row, col)
        lay.addLayout(grid)
        self.readout = QLabel()
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
        self.apply(hold=True)

    # -- changes ---------------------------------------------------------------
    def apply(self, hold: bool | None = None) -> None:
        x, y, k = self.fit
        data = {"behind": self.behind["key"], "front": self.front["key"], "x": x, "y": y, "scale": k}
        if hold is not None:
            data["hold"] = hold
        self.send("fit " + json.dumps(data, separators=(",", ":")))
        self.readout.setText(f"Position {x:.0f}, {y:.0f} · size {100 / k:.1f}%")

    def move_by(self, dx: float, dy: float, big: bool = False) -> None:
        step = BIG_STEP if big else STEP
        self.fit = nudge(self.fit, dx * step, dy * step)
        self.apply()

    def grow_by(self, factor: float) -> None:
        self.fit = grow(self.fit, factor, self.behind["size"], self.front["size"])
        self.apply()

    def start_over(self) -> None:
        self.fit = self.start_fit
        self.apply()

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

    # -- the end ---------------------------------------------------------------
    def on_event(self, _event: dict) -> None:
        """Service events (the Layout page passes them on while calibrating): none are for us."""

    def stop(self) -> None:
        """The service stopped: nothing to show the mirror with."""
        self._done = True
        self.close()

    def accept(self) -> None:
        self._finish(keep=True)
        super().accept()

    def reject(self) -> None:
        self._finish(keep=False)
        super().reject()

    def _finish(self, keep: bool) -> None:
        if self._done:
            return
        self._done = True
        if not keep:
            self.fit = self.start_fit
        self.apply(hold=False)
        if keep:
            self.saved.emit(self.fit)
