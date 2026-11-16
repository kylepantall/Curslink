"""Calibration data and the "which screen am I looking at" classifier."""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Target:
    device_id: str
    screen_id: str
    # True when only the device is known, not which of its monitors you
    # look at (a device's own camera, or a device that wasn't calibrated).
    # Not part of equality: it's the same target either way.
    guess: bool = field(default=False, compare=False)

    @property
    def key(self) -> str:
        return f"{self.device_id}/{self.screen_id}"

    @classmethod
    def parse(cls, key: str) -> "Target":
        device_id, _, screen_id = key.partition("/")
        if not device_id or not screen_id:
            raise ValueError(f"bad target key {key!r}")
        return cls(device_id, screen_id)


@dataclass
class Calibration:
    """Head angles recorded while the user looked at each screen."""

    samples: dict[Target, list[tuple[float, float]]] = field(default_factory=dict)
    # The same looks as other devices' cameras saw them, each from its own
    # angle: device id -> what ``samples`` is for this computer's camera.
    views: dict[str, dict[Target, list[tuple[float, float]]]] = field(default_factory=dict)

    def add(self, target: Target, yaw: float, pitch: float, camera: str | None = None) -> None:
        """One look at ``target``, as this computer's camera saw it, or as
        ``camera`` (another device's own) did."""
        samples = self.samples if camera is None else self.views.setdefault(camera, {})
        samples.setdefault(target, []).append((yaw, pitch))

    def centroids(self, camera: str | None = None) -> dict[Target, tuple[float, float]]:
        out = {}
        for target, pts in (self.samples if camera is None else self.views.get(camera, {})).items():
            if pts:
                out[target] = (
                    statistics.median(p[0] for p in pts),
                    statistics.median(p[1] for p in pts),
                )
        return out

    def save(self, path: str | Path) -> None:
        data = {
            "version": 1,
            "targets": {t.key: [list(p) for p in pts] for t, pts in self.samples.items()},
        }
        if self.views:
            data["cameras"] = {camera: {t.key: [list(p) for p in pts] for t, pts in seen.items()}
                               for camera, seen in self.views.items()}
        Path(path).write_text(json.dumps(data, indent=2))

    @classmethod
    def load(cls, path: str | Path) -> "Calibration":
        data = json.loads(Path(path).read_text())
        if data.get("version") != 1:
            raise ValueError("unsupported calibration file version")
        cal = cls()
        for key, pts in data["targets"].items():
            target = Target.parse(key)
            for yaw, pitch in pts:
                cal.add(target, float(yaw), float(pitch))
        cameras = data.get("cameras")
        for camera, seen in (cameras.items() if isinstance(cameras, dict) else ()):
            for key, pts in seen.items():
                target = Target.parse(key)
                for yaw, pitch in pts:
                    cal.add(target, float(yaw), float(pitch), camera=str(camera))
        return cal


@dataclass
class GazeSettings:
    # Further than this (degrees) from every calibrated screen = looking
    # somewhere else (desk, phone, window): keep the current target.
    max_distance_deg: float = 12.0
    # A new screen must be at least this much closer than the current one.
    switch_margin_deg: float = 3.0
    # ...and stay the best match this long before we switch.
    dwell_s: float = 0.25
    # Pitch is noisier than yaw on most webcams; weight it down a little.
    pitch_weight: float = 0.8


class GazeClassifier:
    """Turns a stream of head angles into deliberate screen changes.

    Combines nearest-centroid matching with hysteresis (margin) and a dwell
    time so that glancing across the desk doesn't throw your cursor around.
    """

    def __init__(self, calibration: Calibration, settings: GazeSettings | None = None):
        self.settings = settings or GazeSettings()
        self._centroids = calibration.centroids()
        if not self._centroids:
            raise ValueError("calibration has no targets; run `curslink calibrate` first")
        self.current: Target | None = None
        self._candidate: Target | None = None
        self._candidate_since = 0.0

    @property
    def targets(self) -> list[Target]:
        """The calibrated screens."""
        return list(self._centroids)

    def distance(self, target: Target, yaw: float, pitch: float) -> float:
        cy, cp = self._centroids[target]
        return math.hypot(yaw - cy, (pitch - cp) * self.settings.pitch_weight)

    def nearest(self, yaw: float, pitch: float) -> tuple[Target, float]:
        return min(
            ((t, self.distance(t, yaw, pitch)) for t in self._centroids),
            key=lambda item: item[1],
        )

    def force(self, target: Target | None) -> None:
        """Sync with a switch that happened another way (edge crossing, hotkey)."""
        self.current = target
        self._candidate = None

    def update(self, t: float, yaw: float, pitch: float) -> Target | None:
        """Feed one sample; returns the new target when the gaze switches."""
        s = self.settings
        best, best_d = self.nearest(yaw, pitch)
        if best_d > s.max_distance_deg or best == self.current:
            self._candidate = None
            return None
        if self.current is not None and self.current in self._centroids:
            if self.distance(self.current, yaw, pitch) - best_d < s.switch_margin_deg:
                self._candidate = None
                return None
        if best != self._candidate:
            self._candidate = best
            self._candidate_since = t
        if t - self._candidate_since >= s.dwell_s:
            self.current = best
            self._candidate = None
            return best
        return None


class JointClassifier(GazeClassifier):
    """Several cameras deciding together which screen you look at.

    Each camera was calibrated from where it stands (``Calibration.views``):
    the same look is a different head angle to the laptop in front of you
    and to the one off to the side, so each has its own centre for every
    screen and is measured against its own. How far you are from a screen
    is then how far every camera that sees you now says you are, taken
    together (root mean square): a screen wins only when the cameras agree
    on it, and one camera losing your face (a bright window behind you, the
    head turned too far) leaves the others to carry on.

    ``local`` is this computer's own camera, fed through :meth:`update` as
    before; the others come in through :meth:`sample`.
    """

    def __init__(self, calibration: Calibration, settings: GazeSettings | None = None, local: str = "",
                 fresh_s: float = 0.6):
        self.settings = settings or GazeSettings()
        self.local = local
        self.fresh_s = fresh_s
        views = {local: calibration.centroids(), **{c: calibration.centroids(c) for c in calibration.views}}
        self._views = {camera: cents for camera, cents in views.items() if cents}
        if not self._views:
            raise ValueError("calibration has no targets; run `curslink calibrate` first")
        self._centroids = {t: c for cents in self._views.values() for t, c in cents.items()}  # (for its targets)
        self._latest: dict[str, tuple[float, float, float]] = {}
        self._now = 0.0
        self.current = None
        self._candidate = None
        self._candidate_since = 0.0

    def has(self, camera: str) -> bool:
        """Was ``camera`` calibrated, so that its view counts?"""
        return camera in self._views

    def live(self, camera: str, now: float) -> bool:
        seen = self._latest.get(camera)
        return camera in self._views and seen is not None and now - seen[0] <= self.fresh_s

    def cameras(self, now: float) -> list[str]:
        """The calibrated cameras that see you now."""
        return [c for c in self._views if self.live(c, now)]

    def see(self, camera: str, t: float, yaw: float, pitch: float) -> None:
        if camera in self._views:
            self._latest[camera] = (t, yaw, pitch)
        self._now = max(self._now, t)

    def distance(self, target: Target, yaw: float | None = None, pitch: float | None = None) -> float:
        """How far from ``target`` the cameras that see you now put you
        (the angles given are not used: each camera has its own)."""
        squares = []
        for camera, cents in self._views.items():
            if target not in cents or not self.live(camera, self._now):
                continue
            _t, y, p = self._latest[camera]
            cy, cp = cents[target]
            squares.append((y - cy) ** 2 + ((p - cp) * self.settings.pitch_weight) ** 2)
        return math.sqrt(sum(squares) / len(squares)) if squares else math.inf

    def update(self, t: float, yaw: float, pitch: float) -> Target | None:
        return self.sample(self.local, t, yaw, pitch)

    def sample(self, camera: str, t: float, yaw: float, pitch: float) -> Target | None:
        """One sample from ``camera``; returns the new target when the gaze switches."""
        self.see(camera, t, yaw, pitch)
        return super().update(t, yaw, pitch)
