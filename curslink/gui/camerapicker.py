"""A camera menu that lists cameras by name (Settings and Calibrate share it).

The config keeps both ``gaze.camera`` (the index OpenCV opens) and
``gaze.camera_name``, so the right camera is found again when another one
is plugged in and the numbers shift.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox

from curslink.gui.common import in_background

log = logging.getLogger(__name__)


class CameraPicker(QComboBox):
    picked = Signal(int, str)  # index, name ("" when the name isn't known)

    def __init__(self):
        super().__init__()
        self.setAccessibleName("Camera")
        self.setMinimumWidth(240)
        self._want: tuple[int, str] = (0, "")
        self._filling = False
        self.loaded = False
        self.addItem("Looking for cameras…", (0, ""))
        self.currentIndexChanged.connect(self._changed)

    def set_current(self, index: int, name: str = "") -> None:
        self._want = (int(index or 0), name or "")
        if self.loaded:
            self._select()

    def current(self) -> tuple[int, str]:
        data = self.currentData()
        return tuple(data) if data else self._want  # type: ignore[return-value]

    def refresh(self) -> None:
        from curslink.gaze.cameras import list_cameras

        in_background(list_cameras, self._fill, lambda exc: self._fill([]), name="curslink-cameras")

    def _fill(self, cameras) -> None:
        self._filling = True
        self.clear()
        for c in cameras:
            self.addItem(c.label, (c.index, c.name))
        if not cameras:
            for i in range(3):
                self.addItem(f"Camera {i + 1}", (i, ""))
        index, name = self._want
        known = {d[0] for d in (self.itemData(i) for i in range(self.count()))}
        if index not in known and not (name and any(self.itemData(i)[1] == name for i in range(self.count()))):
            self.addItem(f"{name or f'Camera {index + 1}'} (not connected)", (index, name))
        self.loaded = True
        self._select()
        self._filling = False

    def _select(self) -> None:
        index, name = self._want
        self._filling = True
        pick = -1
        if name:
            pick = next((i for i in range(self.count()) if self.itemData(i)[1] == name), -1)
        if pick < 0:
            pick = next((i for i in range(self.count()) if self.itemData(i)[0] == index), 0)
        self.setCurrentIndex(pick)
        self._filling = False

    def _changed(self, _i: int) -> None:
        if self._filling or not self.loaded:
            return
        index, name = self.currentData()
        self._want = (index, name)
        log.info("camera picked: %d %r", index, name)
        self.picked.emit(index, name)
