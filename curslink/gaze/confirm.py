"""Gaze from any device's camera: the host's, an agent's, or both together.

In a setup like two laptops and one monitor, the host's camera alone can
only tell that your head turned *away* from its screens; it can't be sure
you are now looking at the other laptop rather than out of the window.
An agent with its own camera can: if it sees a face looking straight at
it, you are facing that device.

Agent side, :class:`FacingDetector` turns the agent's head-pose samples
into a debounced "the user is facing me" flag, which the agent sends to
the host in ``facing`` frames (see :mod:`curslink.protocol`).

Host side, :class:`GazeArbiter` sits between the host's
:class:`~curslink.gaze.zones.GazeClassifier` and the router and only lets
a gaze switch happen when both cameras agree:

* a switch to a device that has a camera needs that device to report the
  user facing it;
* a device reporting the user facing it gets the pointer when the host's
  camera confirms the head has turned away from the host's own screens
  (or towards that device, if it was calibrated).

When the host's camera isn't calibrated, is switched off, or has stopped
seeing anyone, the cameras that are left decide on their own ("facing
only"): a device whose camera sees you facing it gets the pointer, and
when the device you're on stops seeing you the pointer goes to another
device that does, or home to the host if the host has no camera of its
own. The host's camera, when it runs uncalibrated, reports for the host
the same way an agent's does.

Devices without a camera keep the old behaviour: host gaze alone decides.
Everything here is pure logic with explicit timestamps, so it is easy to
test without a camera.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Callable

from curslink.gaze.zones import GazeClassifier, Target

log = logging.getLogger(__name__)


# ------------------------------------------------------------------- agent
@dataclass
class FacingSettings:
    # Head within this many degrees of the camera's "straight on" = facing.
    facing_deg: float = 18.0
    # Must drift this much further out before we say "not facing" again.
    hysteresis_deg: float = 5.0
    # Where "straight on" is. A laptop camera sits above the screen, so
    # looking at the middle of the screen is a few degrees of pitch down.
    center_yaw_deg: float = 0.0
    center_pitch_deg: float = 0.0
    # Pitch is noisier than yaw on most webcams; weight it down a little.
    pitch_weight: float = 0.8
    # How long the new state must hold before it is reported.
    dwell_s: float = 0.15
    # No face for this long = not facing.
    lost_after_s: float = 0.4


class FacingDetector:
    """Agent side: head angles from the agent's own camera in, facing flag out."""

    def __init__(self, settings: FacingSettings | None = None):
        self.settings = settings or FacingSettings()
        self.facing = False
        self.last_sample = -math.inf
        self._candidate: bool | None = None
        self._candidate_since = 0.0

    def offset(self, yaw: float, pitch: float) -> float:
        s = self.settings
        return math.hypot(yaw - s.center_yaw_deg, (pitch - s.center_pitch_deg) * s.pitch_weight)

    def update(self, t: float, yaw: float, pitch: float) -> bool | None:
        """Feed one sample; returns the new state when it changes."""
        self.last_sample = t
        s = self.settings
        limit = s.facing_deg + (s.hysteresis_deg if self.facing else 0.0)
        return self._propose(t, self.offset(yaw, pitch) <= limit)

    def tick(self, t: float) -> bool | None:
        """Call regularly: notices the face going away (no samples arrive then)."""
        if self.facing and t - self.last_sample >= self.settings.lost_after_s:
            self.facing = False
            self._candidate = None
            return False
        return None

    def _propose(self, t: float, facing: bool) -> bool | None:
        if facing == self.facing:
            self._candidate = None
            return None
        if facing != self._candidate:
            self._candidate = facing
            self._candidate_since = t
        if t - self._candidate_since >= self.settings.dwell_s:
            self.facing = facing
            self._candidate = None
            return facing
        return None


# -------------------------------------------------------------------- host
@dataclass
class ConfirmSettings:
    enabled: bool = True
    # A "facing" report older than this is not trusted (agents refresh
    # theirs every second while connected).
    report_fresh_s: float = 3.0
    # The host's own reading must be this recent to confirm anything...
    host_fresh_s: float = 0.5
    # ...except "no face at all" on the host camera, which counts as turned
    # away as long as the host camera produced a sample within this long.
    host_alive_s: float = 10.0


@dataclass
class _Report:
    facing: bool
    at: float


class GazeArbiter:
    """Host side: combines host gaze with agents' own cameras.

    ``classifier`` is the host camera's calibrated classifier, or ``None``
    when the host has no calibrated camera (agents' cameras, and the host's
    own uncalibrated one reporting through :meth:`on_facing`, decide then).
    ``switch(target)`` asks the router to switch; ``current()`` says where
    the pointer is now. ``screens_of(device_id)`` lists a device's screen
    ids (the first is used when a device wasn't calibrated on the host).
    """

    def __init__(
        self,
        classifier: GazeClassifier | None,
        local_device_id: str,
        switch: Callable[[Target], None],
        current: Callable[[], Target],
        screens_of: Callable[[str], list[str]],
        settings: ConfirmSettings | None = None,
    ):
        self.classifier = classifier
        self.local_id = local_device_id
        self._switch = switch
        self._current = current
        self._screens_of = screens_of
        self.settings = settings or ConfirmSettings()
        self.cameras: set[str] = set()
        self.reports: dict[str, _Report] = {}
        self._last_sample: tuple[float, float, float] | None = None  # (t, yaw, pitch)
        # Told when a switch was held back for want of confirmation, for logs and the app.
        self.on_blocked: Callable[[Target], None] = lambda target: None
        # While True nothing switches (the app is calibrating the host camera).
        self.suspended = False

    # ------------------------------------------------------------ agents
    def set_camera(self, device_id: str, has_camera: bool) -> None:
        if has_camera:
            self.cameras.add(device_id)
        else:
            self.forget(device_id)

    def forget(self, device_id: str) -> None:
        self.cameras.discard(device_id)
        self.reports.pop(device_id, None)

    def confirms(self, device_id: str, now: float) -> bool:
        """Does ``device_id`` agree that the user is facing it?"""
        if not self.settings.enabled or device_id == self.local_id or device_id not in self.cameras:
            return True  # nothing to ask: host gaze alone decides
        return self._facing(device_id, now)

    def _facing(self, device_id: str, now: float) -> bool:
        report = self.reports.get(device_id)
        return report is not None and report.facing and now - report.at <= self.settings.report_fresh_s

    def host_live(self, now: float) -> bool:
        """Is the host's calibrated camera seeing anyone, so it has a say?"""
        if self.classifier is None or self._last_sample is None:
            return False
        return now - self._last_sample[0] <= self.settings.host_alive_s

    def on_facing(self, t: float, device_id: str, facing: bool) -> None:
        """A device's own camera says whether the user is facing it."""
        was_facing = self._facing(device_id, t)
        self.reports[device_id] = _Report(facing, t)
        if self.suspended or device_id not in self.cameras:
            return
        if not self.settings.enabled and device_id != self.local_id:
            return  # agents' cameras were turned off on the host
        current = self._current().device_id
        if not self.host_live(t):
            self._facing_only(t, device_id, facing and not was_facing, facing, current)
            return
        if not facing or current == device_id:
            return
        target = self._host_agrees(t, device_id)
        if target is not None:
            log.info("gaze: %s sees you facing it and the host sees you turned away", device_id)
            self._do_switch(target)

    def _facing_only(self, t: float, device_id: str, turned_to: bool, facing: bool, current: str) -> None:
        """No calibrated host camera to ask: the devices' own cameras decide.

        Only *turning to* a device moves the pointer there, not the
        once-a-second "still facing" refresh, so two cameras that both see
        you don't pass the pointer back and forth, and "bring input home"
        sticks until you look away and back."""
        if facing:
            if turned_to and current != device_id:
                log.info("gaze: %s's camera sees you facing it", device_id)
                self._do_switch(self._default_target(device_id))
            return
        if current != device_id:
            return  # you weren't on it anyway
        others = [d for d in self.cameras if d != device_id and self._facing(d, t)
                  and (self.settings.enabled or d == self.local_id)]
        if others:
            target_id = max(others, key=lambda d: self.reports[d].at)
        elif self.local_id not in self.cameras and device_id != self.local_id:
            target_id = self.local_id  # the host has no camera to ask: you looked back at it
        else:
            return  # nobody sees you: looking away from everything, stay put
        log.info("gaze: you turned away from %s, to %s", device_id, target_id)
        self._do_switch(self._default_target(target_id))

    # -------------------------------------------------------------- host
    def on_sample(self, t: float, yaw: float, pitch: float) -> None:
        """One head-pose sample from the host's calibrated camera."""
        if self.classifier is None:
            return
        self._last_sample = (t, yaw, pitch)
        target = self.classifier.update(t, yaw, pitch)
        if self.suspended:
            return
        if target is None:
            return
        if self.confirms(target.device_id, t):
            self._switch(target)
            return
        # Not confirmed yet: undo the classifier's switch so it proposes it
        # again after another dwell, by when the agent may have caught up.
        self.classifier.force(self._current())
        self.on_blocked(target)

    def _host_agrees(self, now: float, device_id: str) -> Target | None:
        """The target on ``device_id`` to switch to, if the host's camera
        backs up the agent's: the head has turned away from the host's
        screens, or towards a calibrated screen of that device."""
        sample = self._last_sample
        assert sample is not None and self.classifier is not None  # see host_live()
        t, yaw, pitch = sample
        age = now - t
        fallback = self._default_target(device_id)
        if age > self.settings.host_fresh_s:
            return fallback  # face gone from the host camera: turned well away
        clf = self.classifier
        limit = clf.settings.max_distance_deg
        dists = {target: clf.distance(target, yaw, pitch) for target in clf.targets}
        best = min(dists, key=dists.get)
        if best.device_id == device_id and dists[best] <= limit:
            return best  # calibrated, and the host sees you looking at it
        near_host = any(d <= limit for target, d in dists.items() if target.device_id != device_id)
        return None if near_host else fallback

    def _default_target(self, device_id: str) -> Target | None:
        targets = self.classifier.targets if self.classifier is not None else []
        calibrated = [t for t in targets if t.device_id == device_id]
        if calibrated:
            return calibrated[0]
        screens = self._screens_of(device_id)
        return Target(device_id, screens[0]) if screens else None

    def _do_switch(self, target: Target | None) -> None:
        if target is None:
            return
        self._switch(target)
        if self.classifier is not None:
            self.classifier.force(self._current())
