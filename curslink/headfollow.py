"""Experimental: keep an overlap lined up as your head moves.

A screen in front of another (a laptop before a monitor) is seen over a
different part of the one behind from every place you look from: with the
laptop 25 cm before the monitor, a centimetre of head is 3 or 4 mm of the
monitor at the laptop's edge. A line-up fits the overlap for where the eye
was then (curslink.layout.Fit: ``quad``, ``eye``, ``depth``); lean over and
the mirror strip slides off what it carries on from, the way near things
slide across far ones out of a train window.

With ``[layout] follow_head = true`` this computer's camera (the one gaze
switching uses, ``[gaze] camera``) watches where your head is, and each
overlap that a live line-up measured is worked out again for the eye where
it now is (:meth:`curslink.layout.Fit.seen_from`), many times a second:
the mirror strip and where the pointer crosses follow your head.

What it goes by, and where it's rough:

* MediaPipe's face model gives the head's place before the camera in
  centimetres, from the size of the face in the picture and a guess at the
  lens. Good for how far the head has *moved* (which is all that's used: the
  move from "home", where the head was first seen sitting still), less so
  for how far away it is.
* The camera is taken to look straight out of this computer's own screen
  when that screen is the one in front (a laptop's camera, above its
  screen: how that screen leans is known from the fit); otherwise straight
  out of the screen behind.
* Home is where your head was when it was switched on (or first seen after
  a new line-up), and is kept between runs. If you sat differently then,
  "Sit here" in Settings > Overlapping screens takes where you are now.

``[layout] follow_seat`` ("Keep overlaps aimed at where I sit") is the slow,
dependable cousin (``slow=True``): nothing moves as you lean about, but once
you've settled somewhere new for half a minute the overlaps are aimed from
there, once, and ``moved(was, now)`` says so (the app shows it, with a way
to put it back: :meth:`HeadFollow.put_back`). Home is then where the camera
first saw you sitting still after the line-up: its last step has just found
your eyes (curslink.seat), so that's the face at a known place.

No camera or ML here: samples come in through :meth:`HeadFollow.sample`.
"""

from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path
from typing import Callable

from curslink.gaze.headpose import OneEuroFilter
from curslink.layout import Fit, Layout

log = logging.getLogger(__name__)

GRID = 64  # the covered part is captured out to multiples of this while following (Layout.part_grid)
HOME_S = 1.5  # home: the middle of where the head is over this long...
HOME_SAMPLES = 10  # ...and at least this many sightings
STILL_MM = 25.0  # ...none of them further than this from that middle
EVERY_S = 1 / 20  # the fits are moved at most this often
MIN_PX = 1.0  # ...and only when a corner would move this far (the screen behind's pixels)
MAX_MM = (300.0, 250.0, 350.0)  # further from home than this (across, down, away) is held at it
LOST_S = 3.0  # no face for this long: back to the fit as measured

# Slow ("Keep overlaps aimed at where I sit"):
SEAT_S = 30.0  # settled: the head has been about one place this long...
SEAT_SAMPLES = 40  # ...seen at least this often...
SEAT_NEAR_MM = (60.0, 60.0, 120.0)  # ...four sightings in five within this of the middle of them
SEAT_MOVED_MM = (30.0, 30.0, 50.0)  # ...and that's this far from where the overlaps are aimed now
SEAT_HOME_S = 4.0  # home, slow: still for longer than a glance (the phone has just been put down)


def mediapipe_head(matrix) -> tuple[float, float, float]:
    """Where the head is before the camera from MediaPipe's face matrix, as
    (to the right as you look at the screen, down, away from the camera) in
    mm. MediaPipe's camera looks along -z with x to its own right (your
    left, facing it) and y up, in centimetres."""
    return (-10.0 * float(matrix[0][3]), -10.0 * float(matrix[1][3]), -10.0 * float(matrix[2][3]))


def _unit(v: tuple[float, float, float]) -> tuple[float, float, float] | None:
    n = math.sqrt(sum(c * c for c in v))
    return None if n < 1e-9 else (v[0] / n, v[1] / n, v[2] / n)


def screen_axes(fit: Fit) -> tuple[tuple[float, float, float], ...]:
    """Which way the front screen of ``fit`` faces, in the screen behind's
    terms (across, down, toward you): its own across, down and straight-out
    directions, from where its corners are in the room. The screen behind's
    own if it can't be told."""
    square = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    if not fit.follows:
        return square
    ex, ey, ez = fit.eye
    pts = []
    for i, d in enumerate(fit.depth):
        qx, qy = fit.quad[2 * i], fit.quad[2 * i + 1]
        pts.append(((qx * (ez - d) + ex * d) / ez, (qy * (ez - d) + ey * d) / ez, d))
    across = _unit(tuple(pts[1][k] - pts[0][k] for k in range(3)))
    down = _unit(tuple(pts[3][k] - pts[0][k] for k in range(3)))
    if across is None or down is None:
        return square
    out = _unit((across[1] * down[2] - across[2] * down[1], across[2] * down[0] - across[0] * down[2],
                 across[0] * down[1] - across[1] * down[0]))
    if out is None or out[2] <= 0:
        return square
    return (across, down, out)


def signature(fits) -> str:
    """Changes when the overlaps are lined up afresh (home goes with a line-up)."""
    return json.dumps(sorted([list(f.behind), list(f.front), [round(v) for v in f.quad], [round(v) for v in f.eye]]
                             for f in fits if f.follows))


class HeadFollow:
    """``layout``: the running layout, whose fits are moved. ``local_id``:
    this computer (whose camera it is). ``changed()``: called after the fits
    move (mirror mode plans again). ``busy()``: True while an overlap is
    being adjusted by hand (nothing moves meanwhile). ``home_path``: where
    home is kept."""

    def __init__(self, layout: Layout, local_id: str, changed: Callable[[], None],
                 busy: Callable[[], bool] = lambda: False, home_path: Path | None = None,
                 clock: Callable[[], float] = time.monotonic, slow: bool = False,
                 moved: Callable[[tuple, tuple], None] | None = None,
                 home_set: Callable[[tuple], None] | None = None):
        self.layout = layout
        self.local_id = local_id
        self.changed = changed
        self.busy = busy
        self.home_path = home_path
        self.clock = clock
        self.slow = slow
        self.moved = moved
        self.home_set = home_set  # told where the head is before the camera when home is taken
        self._seat: list[tuple[float, tuple[float, float, float]]] = []  # slow: where the head's been lately
        self._refused: tuple[float, float, float] | None = None  # slow: a place it was told not to aim from
        self.base: dict[tuple, Fit] = {}
        self._mine: tuple | None = None  # layout.fits as this last left them
        self._grid = layout.part_grid
        self.home: tuple[float, float, float] | None = None
        self._home_for = ""  # the overlaps (their signature) home is for
        self._settling: list[tuple[float, tuple[float, float, float]]] = []
        self._filters = (OneEuroFilter(1.0, 0.01), OneEuroFilter(1.0, 0.01), OneEuroFilter(0.3, 0.003))
        self._applied_at = 0.0
        self._seen_at: float | None = None
        self.offset = (0.0, 0.0, 0.0)  # the head's move from home last gone by, mm
        self.moves = 0
        self._logged = 0.0
        self._take()
        self._load_home()
        if not slow:
            layout.part_grid = GRID
        log.info("%s: following %d overlap(s)%s", "keeping overlaps aimed at where you sit" if slow else
                 "head tracking (experimental)", len(self.base),
                 "" if self.base else " (none has been through a live line-up yet)")

    # ---- the fits as measured ----
    def _take(self) -> None:
        """The layout's fits are the measured ones (at the start, or after
        something else changed them: a line-up saved, one adjusted by hand)."""
        self.base = {(f.behind, f.front): f for f in self.layout.fits if f.follows and self._mm(f)}
        self._mine = self.layout.fits

    def wanted(self) -> bool:
        """Is there an overlap to follow (one through a live line-up)? Looks
        again if the layout's fits changed since."""
        if self.layout.fits is not self._mine:
            self._take()
            self._load_home()
            if self.home is None:
                self._settling = []
        return bool(self.base)

    def _mm(self, fit: Fit) -> float | None:
        """The size of one of the screen behind's pixels."""
        if fit.mm:
            return fit.mm
        size = self.layout.physical.get(fit.behind)
        try:
            return size[0] / self.layout.screen(*fit.behind).width if size else None
        except (KeyError, ZeroDivisionError):
            return None

    # ---- home ----
    def _load_home(self) -> None:
        """Home as it was kept, if it was for these overlaps as they're now lined up."""
        now = signature(self.base.values())
        if self.home is not None and self._home_for != now:
            self.home = None
        if self.home is not None or self.home_path is None:
            return
        self._home_for = now
        try:
            data = json.loads(self.home_path.read_text())
            if data.get("fits") == signature(self.base.values()):
                self.home = tuple(float(v) for v in data["home"][:3])
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            self.home = None
        if self.home is not None and len(self.home) != 3:
            self.home = None

    def _save_home(self) -> None:
        if self.home_path is None or self.home is None:
            return
        try:
            self.home_path.parent.mkdir(parents=True, exist_ok=True)
            self.home_path.write_text(json.dumps({"home": list(self.home), "fits": signature(self.base.values())}))
        except OSError:
            log.debug("couldn't save where the head's home is", exc_info=True)

    def rehome(self) -> None:
        """Where the head is now (once it's still) is where the overlaps are right from."""
        self.home = None
        self._settling = []
        for f in self._filters:
            f.reset()
        self._apply((0.0, 0.0, 0.0), force=True)
        log.info("head tracking: taking where you sit now as home")

    def _settle(self, t: float, head: tuple[float, float, float]) -> None:
        over = SEAT_HOME_S if self.slow else HOME_S
        self._settling = [(at, h) for at, h in self._settling if t - at <= over] + [(t, head)]
        if len(self._settling) < HOME_SAMPLES or t - self._settling[0][0] < 0.8 * over:
            return
        middle = tuple(sorted(h[k] for _at, h in self._settling)[len(self._settling) // 2] for k in range(3))
        if any(abs(h[k] - middle[k]) > STILL_MM for _at, h in self._settling for k in range(2)):
            return  # still moving about
        self.home, self._settling = middle, []
        self._seat, self._refused = [], None
        self._home_for = signature(self.base.values())
        for f in self._filters:
            f.reset()
        self._save_home()
        log.info("head tracking: home is %.0f mm across, %.0f down, %.0f mm from the camera", *middle)
        if self.home_set is not None:
            try:
                self.home_set(middle)
            except Exception:  # (noting it is a nicety)
                log.debug("couldn't note where home is", exc_info=True)

    # ---- samples ----
    def sample(self, t: float, head: tuple[float, float, float]) -> None:
        """The head is at ``head`` before the camera: (to the right as you
        look at the screen, down, away from the camera), in mm."""
        if not all(math.isfinite(v) for v in head) or not 100 < head[2] < 3000:
            return
        self._seen_at = t
        if self.layout.fits is not self._mine:
            self._take()
            self._load_home()
            if self.home is None:
                self._settling = []
        if self.home is None:
            self._settle(t, head)
            return
        if self.slow:
            self._sat(t, head)
            return
        smooth = tuple(f(t, v) for f, v in zip(self._filters, head))
        self._apply(tuple(max(-m, min(m, s - h)) for s, h, m in zip(smooth, self.home, MAX_MM)), t=t)

    # ---- slow: where you've settled ----
    def _sat(self, t: float, head: tuple[float, float, float]) -> None:
        """Slow: once the head has been about one place for a while, and
        that's not where the overlaps are aimed from, they're aimed from there."""
        self._seat = [(at, h) for at, h in self._seat if t - at <= SEAT_S] + [(t, head)]
        if len(self._seat) < SEAT_SAMPLES or t - self._seat[0][0] < 0.9 * SEAT_S:
            return
        middle = tuple(sorted(h[k] for _at, h in self._seat)[len(self._seat) // 2] for k in range(3))
        near = sum(1 for _at, h in self._seat if all(abs(h[k] - middle[k]) <= SEAT_NEAR_MM[k] for k in range(3)))
        if near < 0.8 * len(self._seat):
            return  # moving about
        offset = tuple(max(-m, min(m, v - h)) for v, h, m in zip(middle, self.home, MAX_MM))
        if not any(abs(a - b) >= m for a, b, m in zip(offset, self.offset, SEAT_MOVED_MM)):
            return
        if self._refused is not None and not any(abs(a - b) >= m for a, b, m in zip(offset, self._refused, SEAT_MOVED_MM)):
            return  # put back from here once already
        was = self.offset
        self._apply(offset, t=t, force=True)
        if self.offset == offset:
            self._refused = None
            self._seat = []
            log.info("where you sit: settled %.0f mm across, %.0f down, %.0f further back than where the overlaps were "
                     "lined up from; they're aimed from there now", *offset)
            if self.moved is not None:
                self.moved(was, offset)

    def put_back(self) -> None:
        """Slow: the overlaps as they were before the last move (and not
        moved for sitting there again)."""
        if not self.slow or self.offset == (0.0, 0.0, 0.0):
            return
        self._refused, self._seat = self.offset, []
        self._apply((0.0, 0.0, 0.0), force=True)
        log.info("where you sit: put back to the overlaps as lined up")

    def tick(self, t: float | None = None) -> None:
        """Now and then: with no face for a while, back to the fits as measured
        (slow: they stay; you've only left the desk)."""
        if self.slow:
            return
        t = self.clock() if t is None else t
        if self._seen_at is not None and t - self._seen_at > LOST_S and self.offset != (0.0, 0.0, 0.0):
            log.info("head tracking: no face for %.0f s, back to the overlaps as lined up", t - self._seen_at)
            for f in self._filters:
                f.reset()
            self._apply((0.0, 0.0, 0.0), force=True)

    def fits_for(self, offset: tuple[float, float, float]) -> dict[tuple, Fit]:
        """Each followed fit for a head ``offset`` mm from home (across, down, away from the camera)."""
        out = {}
        for key, fit in self.base.items():
            axes = screen_axes(fit) if fit.front[0] == self.local_id else ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
            mm = self._mm(fit)
            move = [sum(axes[a][k] * offset[a] for a in range(3)) / mm for k in range(3)]
            out[key] = fit.seen_from((fit.eye[0] + move[0], fit.eye[1] + move[1], fit.eye[2] + move[2]))
        return out

    def _apply(self, offset: tuple[float, float, float], t: float | None = None, force: bool = False) -> None:
        if not self.base or self.busy():
            return
        if self.layout.fits is not self._mine and not force:
            return
        now = self.clock() if t is None else t
        if not force and now - self._applied_at < EVERY_S:
            return
        new = self.fits_for(offset)
        current = {(f.behind, f.front): f for f in self.layout.fits}
        step = max((abs(a - b) for key, f in new.items() if key in current and current[key].quad is not None
                    for a, b in zip(f.quad, current[key].quad)), default=0.0)
        if step < MIN_PX and not (force and step > 0):
            return
        self.layout.fits = tuple(new.get((f.behind, f.front), f) for f in self.layout.fits)
        self._mine = self.layout.fits
        self._applied_at = now
        self.offset = offset
        self.moves += 1
        if now - self._logged > 10.0:
            self._logged = now
            log.debug("head tracking: head %.0f mm across, %.0f down, %.0f nearer/further from home; overlap moved %.1f px",
                      offset[0], offset[1], offset[2], step)
        self.changed()

    def stop(self) -> None:
        """Back to the overlaps as lined up."""
        if self.layout.fits is self._mine and self.base:
            self.layout.fits = tuple(self.base.get((f.behind, f.front), f) for f in self.layout.fits)
        self._mine = None
        self.layout.part_grid = self._grid
        self.changed()
