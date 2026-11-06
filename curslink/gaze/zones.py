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

    def add(self, target: Target, yaw: float, pitch: float) -> None:
        self.samples.setdefault(target, []).append((yaw, pitch))

    def centroids(self) -> dict[Target, tuple[float, float]]:
        out = {}
        for target, pts in self.samples.items():
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
        return cal


@dataclass
class GazeSettings:
    # Further than this (degrees) from every calibrated screen = looking
    # somewhere else (desk, phone, window): keep the current target.
    max_distance_deg: float = 12.0
    # A new screen must be at least this much closer than the current one.
    switch_margin_deg: float = 3.0
    # ...and stay the best match this long before we switch.
    dwell_s: float = 0.35
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
