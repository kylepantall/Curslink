"""How fast the pointer moves on other devices ([pointer] in the host's config).

The host reads the mouse's motion after its own operating system has
applied its pointer speed and acceleration, and by default the pointer on
another device moves exactly that far: it feels like the host's mouse.

Two settings change that, per device (``[pointer.device."<id>"]``) or for
every device (``[pointer]``):

``speed``
    A multiplier, 0.25 to 4 (1 = unchanged).

``feel = "native"``
    Move the way that device's own mouse settings would. Each agent tells
    the host its pointer settings in ``hello.pointer`` (see
    :func:`local_profile`): Windows' pointer speed and "Enhance pointer
    precision", macOS's tracking speed, GNOME's speed and acceleration
    profile. The host undoes its own curve to estimate how far the mouse
    itself moved (in counts), then applies the device's curve to that.
    ``speed`` still multiplies the result, to trim it.

The curves are models of what each system does, close enough that a slow,
careful move stays slow and a flick still crosses the screen.
"""

from __future__ import annotations

import logging
import math
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)

FEELS = ("here", "native")
SPEED_MIN, SPEED_MAX = 0.25, 4.0

# Mouse counts per inch the curves assume (most mice are 400 to 1600).
COUNTS_PER_INCH = 800.0
MM_PER_COUNT = 25.4 / COUNTS_PER_INCH

# Windows' pointer speed slider (1-20, 10 is the default): its multiplier.
WIN_SPEEDS = (0.03125, 0.0625, 0.125, 0.25, 0.375, 0.5, 0.625, 0.75, 0.875, 1.0,
              1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0, 3.25, 3.5)
# "Enhance pointer precision": Windows' default SmoothMouseXCurve and
# SmoothMouseYCurve (mouse inches a second -> screen inches a second).
WIN_CURVE_X = (0.0, 0.43, 1.25, 3.86, 40.0)
WIN_CURVE_Y = (0.0, 1.37, 5.30, 24.30, 568.0)
WIN_MICKEYS_PER_INCH = 400.0   # what Windows assumes of the mouse
WIN_SCREEN_DPI = 96.0

# A gap this long between moves starts a new movement.
GAP_S = 0.1
# Moves this close together are timed as this far apart (reports batch up).
MIN_DT_S = 0.002
# Smooths the measured speed so the gain doesn't flicker from one report to the next.
SPEED_SMOOTHING = 0.5


def _interp(xs: tuple, ys: tuple, x: float) -> float:
    if x <= xs[0]:
        return ys[0]
    for i in range(1, len(xs)):
        if x <= xs[i]:
            t = (x - xs[i - 1]) / (xs[i] - xs[i - 1])
            return ys[i - 1] + t * (ys[i] - ys[i - 1])
    slope = (ys[-1] - ys[-2]) / (xs[-1] - xs[-2])
    return ys[-1] + (x - xs[-1]) * slope


@dataclass(frozen=True)
class Profile:
    """A device's pointer settings, as its operating system keeps them.

    ``os``: windows | mac | linux. ``speed``: the system's own value (Windows
    1-20; macOS tracking speed 0-3, or -1 for acceleration off; GNOME -1..1).
    ``accel``: acceleration on (Windows "Enhance pointer precision", GNOME's
    adaptive profile; macOS always accelerates unless speed is -1).
    """

    os: str
    speed: float
    accel: bool = True

    def gain(self, counts_per_s: float) -> float:
        """Pixels the pointer moves per mouse count at this mouse speed."""
        v = max(0.0, counts_per_s)
        if self.os == "windows":
            mult = WIN_SPEEDS[min(19, max(0, round(self.speed) - 1))]
            if not self.accel:
                return mult
            inches = max(v / WIN_MICKEYS_PER_INCH, 1e-6)
            if inches <= WIN_CURVE_X[1]:
                ratio = WIN_CURVE_Y[1] / WIN_CURVE_X[1]
            else:
                ratio = _interp(WIN_CURVE_X, WIN_CURVE_Y, inches) / inches
            return mult * ratio * WIN_SCREEN_DPI / WIN_MICKEYS_PER_INCH
        mm_s = v * MM_PER_COUNT
        if self.os == "mac":
            if self.speed < 0:
                return 1.0
            s = min(3.0, self.speed)
            return 0.5 + s * 0.6 * min((mm_s / 100.0) ** 0.7, 4.0)
        if self.os == "linux":
            base = max(0.05, 1.0 + max(-1.0, min(1.0, self.speed)))
            if not self.accel:
                return base
            return base * (0.6 + 0.4 * min(mm_s / 150.0, 3.0))
        return 1.0

    def out_speed(self, counts_per_s: float) -> float:
        """Pixels a second the pointer moves at this mouse speed."""
        return counts_per_s * self.gain(counts_per_s)

    def counts_for(self, px_per_s: float) -> float:
        """The mouse speed (counts a second) that moves the pointer this fast."""
        if px_per_s <= 0:
            return 0.0
        lo, hi = 0.0, 1.0
        while self.out_speed(hi) < px_per_s and hi < 1e7:
            hi *= 2.0
        for _ in range(40):
            mid = (lo + hi) / 2.0
            if self.out_speed(mid) < px_per_s:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2.0

    def to_wire(self) -> dict:
        return {"os": self.os, "speed": self.speed, "accel": self.accel}

    def describe(self) -> str:
        if self.os == "windows":
            return f"Windows speed {round(self.speed)}/20, precision {'on' if self.accel else 'off'}"
        if self.os == "mac":
            return "macOS, acceleration off" if self.speed < 0 else f"macOS tracking speed {self.speed:g}"
        return f"Linux speed {self.speed:g}, {'adaptive' if self.accel else 'flat'}"


def profile_from_wire(data: Any) -> Profile | None:
    """``hello.pointer`` from an agent, or None if it sent none (or nonsense)."""
    if not isinstance(data, dict):
        return None
    os_name = data.get("os")
    speed = data.get("speed")
    if os_name not in ("windows", "mac", "linux") or not isinstance(speed, (int, float)) or isinstance(speed, bool):
        return None
    if not math.isfinite(speed):
        return None
    return Profile(str(os_name), float(speed), bool(data.get("accel", True)))


# -- this computer's own settings ------------------------------------------------
def _run(args: list[str]) -> str:
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _windows_profile() -> Profile | None:
    import ctypes

    spi = ctypes.windll.user32.SystemParametersInfoW  # type: ignore[attr-defined]
    speed = ctypes.c_int(10)
    mouse = (ctypes.c_int * 3)(6, 10, 1)
    if not spi(0x0070, 0, ctypes.byref(speed), 0):  # SPI_GETMOUSESPEED
        speed.value = 10
    if not spi(0x0003, 0, mouse, 0):  # SPI_GETMOUSE: thresholds, acceleration
        mouse[2] = 1
    return Profile("windows", float(max(1, min(20, speed.value))), mouse[2] != 0)


def _mac_value(key: str) -> float | None:
    try:
        from Foundation import CFPreferencesCopyAppValue  # type: ignore[import-not-found]

        value = CFPreferencesCopyAppValue(key, "NSGlobalDomain")
    except Exception:
        value = _run(["defaults", "read", "-g", key]) or None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _mac_profile() -> Profile | None:
    # A mouse's tracking speed if one has been set up, else the trackpad's.
    for key, default in (("com.apple.mouse.scaling", None), ("com.apple.trackpad.scaling", 1.0)):
        value = _mac_value(key)
        if value is None:
            value = default
        if value is not None:
            return Profile("mac", value, value >= 0)
    return None


def _linux_profile() -> Profile | None:
    speed = _run(["gsettings", "get", "org.gnome.desktop.peripherals.mouse", "speed"])
    accel = _run(["gsettings", "get", "org.gnome.desktop.peripherals.mouse", "accel-profile"])
    try:
        value = float(speed) if speed else 0.0
    except ValueError:
        value = 0.0
    return Profile("linux", value, "flat" not in accel)


def local_profile() -> Profile | None:
    """This computer's pointer settings, or None where they can't be read."""
    try:
        if sys.platform == "win32":
            return _windows_profile()
        if sys.platform == "darwin":
            return _mac_profile()
        if sys.platform.startswith("linux"):
            return _linux_profile()
    except Exception:
        log.debug("pointer: couldn't read this computer's pointer settings", exc_info=True)
    return None


# -- the host's settings -----------------------------------------------------------
@dataclass(frozen=True)
class DeviceFeel:
    feel: str = "here"
    speed: float = 1.0


def _feel(table: Any, where: str, base: DeviceFeel) -> DeviceFeel:
    from curslink.config import ConfigError

    if not isinstance(table, dict):
        raise ConfigError(f"{where} must be a table")
    feel = table.get("feel", base.feel)
    if feel not in FEELS:
        raise ConfigError(f'{where}.feel must be "here" or "native"')
    speed = table.get("speed", base.speed)
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not math.isfinite(speed):
        raise ConfigError(f"{where}.speed must be a number")
    if not SPEED_MIN <= speed <= SPEED_MAX:
        raise ConfigError(f"{where}.speed must be between {SPEED_MIN:g} and {SPEED_MAX:g}")
    return DeviceFeel(str(feel), float(speed))


@dataclass
class PointerSettings:
    default: DeviceFeel = field(default_factory=DeviceFeel)
    devices: dict[str, DeviceFeel] = field(default_factory=dict)

    def for_device(self, device_id: str) -> DeviceFeel:
        return self.devices.get(device_id, self.default)


def pointer_settings(raw: dict[str, Any]) -> PointerSettings:
    """``[pointer]`` (feel, speed for every device) and ``[pointer.device."<id>"]``."""
    table = raw.get("pointer", {})
    default = _feel(table, "[pointer]", DeviceFeel())
    devices = {}
    per = table.get("device", {}) if isinstance(table, dict) else {}
    if not isinstance(per, dict):
        from curslink.config import ConfigError

        raise ConfigError("[pointer.device] must be a table of devices")
    for device_id, t in per.items():
        devices[str(device_id)] = _feel(t, f'[pointer.device."{device_id}"]', default)
    return PointerSettings(default, devices)


class PointerFeel:
    """Scales the host's mouse motion for the device the pointer is on (see
    :class:`curslink.router.Router`)."""

    def __init__(self, settings: PointerSettings | None = None, here: Profile | None = None,
                 profile_of: Callable[[str], Profile | None] = lambda device_id: None):
        self.settings = settings or PointerSettings()
        self.here = here
        self.profile_of = profile_of
        self._device = ""
        self._at = -1e9
        self._speed = 0.0       # the host pointer's speed, smoothed (px/s)
        self._said: set[tuple[str, str]] = set()

    def ratio(self, device_id: str, px_per_s: float) -> float:
        """How much further than the host's own pointer to move on that device."""
        feel = self.settings.for_device(device_id)
        ratio = feel.speed
        if feel.feel == "native":
            there = self.profile_of(device_id)
            if there is not None and self.here is not None:
                counts = self.here.counts_for(px_per_s)
                ratio *= there.gain(counts) / max(self.here.gain(counts), 1e-6)
                self._say(device_id, f"moves like its own mouse ({there.describe()}; here: {self.here.describe()})")
            else:
                self._say(device_id, "can't move like its own mouse: "
                          + ("it didn't say how its mouse is set up" if there is None
                             else "this computer's mouse settings couldn't be read"))
        return ratio

    def _say(self, device_id: str, what: str) -> None:
        if (device_id, what) not in self._said:
            self._said.add((device_id, what))
            log.info("pointer: %s %s", device_id, what)

    def scale(self, device_id: str, dx: float, dy: float, ts: float) -> tuple[float, float]:
        feel = self.settings.for_device(device_id)
        if feel.feel == "here" and feel.speed == 1.0:
            return dx, dy
        dist = math.hypot(dx, dy)
        dt = ts - self._at
        if device_id != self._device or dt > GAP_S or dt < 0:
            dt = 1 / 125  # a typical mouse's report rate
            self._speed = 0.0
        self._device, self._at = device_id, ts
        speed = dist / max(dt, MIN_DT_S)
        self._speed = speed if self._speed <= 0 else (
            SPEED_SMOOTHING * self._speed + (1 - SPEED_SMOOTHING) * speed)
        r = self.ratio(device_id, self._speed)
        return dx * r, dy * r
