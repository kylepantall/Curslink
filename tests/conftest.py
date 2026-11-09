from __future__ import annotations

import pytest

from curslink.layout import Device, Layout, Screen


class FakeSink:
    def __init__(self):
        self.calls = []

    def move_to(self, x, y):
        self.calls.append(("move", x, y))

    def button(self, button, pressed):
        self.calls.append(("button", button, pressed))

    def scroll(self, dx, dy):
        self.calls.append(("scroll", dx, dy))

    def key(self, key, pressed):
        self.calls.append(("key", key, pressed))


class FakeCapture:
    def __init__(self):
        self.grabbed = False
        self.history = []

    def start(self, handler):
        pass

    def set_grab(self, grabbed):
        self.grabbed = grabbed
        self.history.append(grabbed)

    def stop(self):
        pass


class FakeTransport:
    def __init__(self, connected=()):
        self.connected = set(connected)
        self.sent = []

    def is_connected(self, device_id):
        return device_id in self.connected

    def send(self, device_id, t, **data):
        self.sent.append((device_id, t, data))

    def of_type(self, t):
        return [s for s in self.sent if s[1] == t]


class FakeOverlay:
    def __init__(self):
        self.shown = []

    def show(self, effect, x, y, box=None):
        self.shown.append((effect, x, y))
        self.boxes = getattr(self, "boxes", []) + [box]


@pytest.fixture
def layout():
    """desktop: two monitors side by side; laptop to the right; tablet above-left."""
    return Layout.from_devices(
        [
            Device(
                "desktop",
                "Desktop",
                (
                    Screen("left", "desktop", 0, 0, 1920, 1080),
                    Screen("right", "desktop", 1920, 0, 1920, 1080, os_x=1920),
                ),
            ),
            Device("laptop", "Laptop", (Screen("main", "laptop", 3840, 200, 1280, 800),)),
            Device("tablet", "Tablet", (Screen("main", "tablet", 0, -900, 1200, 900),)),
        ]
    )
