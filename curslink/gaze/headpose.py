"""Pure maths for turning a face pose into smoothed head angles.

No camera or ML dependencies live here so that it is easy to test.
"""

from __future__ import annotations

import math
from typing import Sequence


def rotation_to_euler(r: Sequence[Sequence[float]]) -> tuple[float, float, float]:
    """Decompose a 3x3 rotation matrix ``R = Rz(roll) @ Ry(yaw) @ Rx(pitch)``.

    Returns ``(yaw, pitch, roll)`` in degrees. Sign conventions do not
    matter much for Curslink because screens are recognised from per-user
    calibration samples, not from absolute angles.
    """
    sy = -r[2][0]
    sy = max(-1.0, min(1.0, sy))
    yaw = math.asin(sy)
    if abs(sy) < 0.999999:
        pitch = math.atan2(r[2][1], r[2][2])
        roll = math.atan2(r[1][0], r[0][0])
    else:  # gimbal lock, not reachable by a human neck but keep it defined
        pitch = math.atan2(-r[1][2], r[1][1])
        roll = 0.0
    return math.degrees(yaw), math.degrees(pitch), math.degrees(roll)


class OneEuroFilter:
    """The 1-euro filter (Casiez et al., 2012).

    Strong smoothing when the head is still (kills camera jitter), little
    lag when it moves quickly (so switching still feels instant).
    """

    def __init__(self, min_cutoff: float = 1.0, beta: float = 0.05, d_cutoff: float = 1.0):
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self._x: float | None = None
        self._dx = 0.0
        self._t: float | None = None

    @staticmethod
    def _alpha(cutoff: float, dt: float) -> float:
        tau = 1.0 / (2 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def reset(self) -> None:
        self._x = None
        self._t = None
        self._dx = 0.0

    def __call__(self, t: float, x: float) -> float:
        if self._x is None or self._t is None or t <= self._t:
            if self._x is None:
                self._x, self._t = x, t
            return self._x
        dt = t - self._t
        dx = (x - self._x) / dt
        a_d = self._alpha(self.d_cutoff, dt)
        self._dx = a_d * dx + (1 - a_d) * self._dx
        cutoff = self.min_cutoff + self.beta * abs(self._dx)
        a = self._alpha(cutoff, dt)
        self._x = a * x + (1 - a) * self._x
        self._t = t
        return self._x


class AngleSmoother:
    """Smooths a (yaw, pitch) stream and resets after the face is lost."""

    def __init__(self, min_cutoff: float = 1.0, beta: float = 0.05, reset_after_s: float = 0.5):
        self._yaw = OneEuroFilter(min_cutoff, beta)
        self._pitch = OneEuroFilter(min_cutoff, beta)
        self._last_t: float | None = None
        self.reset_after_s = reset_after_s

    def __call__(self, t: float, yaw: float, pitch: float) -> tuple[float, float]:
        if self._last_t is not None and t - self._last_t > self.reset_after_s:
            self._yaw.reset()
            self._pitch.reset()
        self._last_t = t
        return self._yaw(t, yaw), self._pitch(t, pitch)
