"""Interfaces between the platform input layer and the rest of Curslink."""

from __future__ import annotations

from typing import Protocol


class CaptureHandler(Protocol):
    def on_pointer(self, x: float, y: float) -> None:
        """Absolute OS position, reported while input stays on this device."""

    def on_motion(self, dx: float, dy: float) -> None:
        """Relative motion, reported while input is grabbed for another device."""

    def on_button(self, button: str, pressed: bool) -> None: ...

    def on_scroll(self, dx: float, dy: float) -> None: ...

    def on_key(self, key: str, pressed: bool) -> None: ...


class InputCapture(Protocol):
    def start(self, handler: CaptureHandler) -> None: ...

    def set_grab(self, grabbed: bool) -> None:
        """Grabbed: local OS stops seeing the input and we get relative motion."""

    def stop(self) -> None: ...


class InputSink(Protocol):
    """Injects input into the OS of the machine it runs on."""

    def move_to(self, x: int, y: int) -> None: ...

    def button(self, button: str, pressed: bool) -> None: ...

    def scroll(self, dx: float, dy: float) -> None: ...

    def key(self, key: str, pressed: bool) -> None: ...
