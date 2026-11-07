"""Smooth pointer playback on an agent (a small jitter buffer).

The host sends one ``move`` per mouse report, stamped with the time the
report was captured (``ts``, the host's monotonic clock in ms). On a
perfect link they'd arrive just as evenly spaced; on a real one (Wi-Fi
especially) they arrive in clumps: nothing for 30 ms, then six at once.
Replayed as they land, the pointer stalls and then jumps.

So positions are played back on the host's timeline instead, a few
milliseconds behind it, and the pointer is moved between reports at a
steady rate. How far behind adapts to the link as it goes: the target is
just enough to cover how late nearly all recent reports were (the 98th
percentile of the last few seconds) plus one gap between reports (to have
the next one to move towards), never more than ``MAX_DELAY_S``. On a wired
link with a fast mouse that's a millisecond or two; on busy Wi-Fi it grows.

The delay never jumps to its target, because a jump in the delay is a
jump (or a freeze) of the pointer. It glides there instead: when the link
gets worse, playback runs a little slower than real time until it has
enough slack (``RISE_PER_S``); when the link recovers, a little faster
until it's caught up (``FALL_PER_S``). Both are too gentle to see.

Clicks, scrolls and keys don't wait: the session calls :meth:`snap`
first, which puts the pointer on the newest position at once, so a click
lands exactly where the host saw it.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Callable

log = logging.getLogger(__name__)

TICK_S = 0.004            # pointer updates while playing back: 250 a second
MAX_DELAY_S = 0.12        # never play back further behind than this (35 ms in 0.4.10-0.4.17,
                          # 60 ms in 0.4.18: busy Wi-Fi had moves later than both: stutter)
STATS_EVERY_S = 0.1       # re-measure the target delay this often
WINDOW_S = 3.0            # ... from this much recent history
LATE_PCT = 0.98           # cover this share of recent reports
RISE_PER_S = 0.4          # link got worse: add up to 0.4 s of delay a second (playback at 60% speed)
FALL_PER_S = 0.05         # link recovered: shed 50 ms of delay a second (playback at 105% speed)
OFFSET_WINDOW_S = 10.0    # the clock offset from this much (it drifts slowly)
REPORT_EVERY_S = 10.0     # log how the link is doing this often while active


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * q))]


class PointerSmoother:
    def __init__(self, move: Callable[[int, int], None], clock: Callable[[], float] = time.monotonic,
                 max_delay_s: float = MAX_DELAY_S, threaded: bool = True):
        self._move = move
        self._threaded = threaded  # False: the caller drives step() (tests)
        self._clock = clock
        self.max_delay_s = max_delay_s
        self._lock = threading.RLock()
        self._samples: deque[tuple[float, float, float]] = deque()   # (host ts s, x, y), oldest first
        self._history: deque[tuple[float, float]] = deque()          # (arrival s, host ts s)
        self._offset: float | None = None   # arrival - ts at its smallest: clock offset + least delay
        self.delay_s = 0.0          # how far behind playback is now
        self.target_s = 0.0         # ... and where it's heading (see _measure)
        self._stats_at = 0.0
        self._stepped_at: float | None = None
        self._at: tuple[int, int] | None = None   # where the pointer was last put
        self.active = False
        self._wake = threading.Event()
        self._stop = False
        self._thread: threading.Thread | None = None
        self._report = _LinkReport()

    # ------------------------------------------------------------ loop side
    def reset(self, x: int, y: int) -> None:
        """Input just arrived on this device: go to (x, y) now and start playing back."""
        with self._lock:
            self._samples.clear()
            self._put(x, y)
            self.active = True
        self._ensure_thread()
        self._wake.set()

    def add(self, x: float, y: float, ts_ms: float) -> None:
        now = self._clock()
        ts = ts_ms / 1000.0
        with self._lock:
            if self._samples and ts < self._samples[-1][0]:
                ts = self._samples[-1][0]  # never go back in time
            self._samples.append((ts, x, y))
            self._history.append((now, ts))
            lag = now - ts
            if self._offset is None or lag < self._offset:
                self._offset = lag
            self._report.arrived(now, lag - self._offset)
            if not self.active:
                self.active = True
                self._ensure_thread()
                self._wake.set()

    def snap(self) -> None:
        """Put the pointer on the newest position now (before a click or key)."""
        with self._lock:
            if self._samples:
                ts, x, y = self._samples[-1]
                self._samples.clear()
                self._samples.append((ts, x, y))
                self._put(x, y)

    def pause(self) -> None:
        """Input left this device."""
        with self._lock:
            self.snap()
            self.active = False
            self._samples.clear()
            self._report.flush(self._clock(), self.delay_s, force=True)

    def close(self) -> None:
        self._stop = True
        self._wake.set()

    # ---------------------------------------------------------- playback
    def step(self, now: float | None = None) -> None:
        """Move the pointer to where the host's timeline says it is now."""
        now = self._clock() if now is None else now
        with self._lock:
            if not self._samples or self._offset is None:
                return
            if now - self._stats_at >= STATS_EVERY_S:
                self._measure(now)
            self._glide(now)
            p = now - self._offset - self.delay_s   # "now", on the host's clock
            samples = self._samples
            while len(samples) > 1 and samples[1][0] <= p:
                samples.popleft()
            t0, x0, y0 = samples[0]
            if len(samples) == 1 or p <= t0:
                if p >= t0:
                    self._put(x0, y0)
                return  # nothing newer yet: stay put
            t1, x1, y1 = samples[1]
            f = (p - t0) / (t1 - t0) if t1 > t0 else 1.0
            self._put(x0 + (x1 - x0) * f, y0 + (y1 - y0) * f)
            self._report.flush(now, self.delay_s)

    def _measure(self, now: float) -> None:
        self._stats_at = now
        history = self._history
        while history and now - history[0][0] > OFFSET_WINDOW_S:
            history.popleft()
        if not history:
            return
        # Re-derive the offset from recent history only, so clock drift and a
        # route change (Wi-Fi to wired) are followed.
        self._offset = min(a - t for a, t in history)
        recent = [(a, t) for a, t in history if now - a <= WINDOW_S]
        late = [a - t - self._offset for a, t in recent]
        gaps = [t1 - t0 for (_, t0), (_, t1) in zip(recent, recent[1:]) if t1 > t0]
        jitter = _pct(late, LATE_PCT)
        gap = min(_pct(gaps, 0.5), 0.02) if gaps else 0.0
        self.target_s = min(self.max_delay_s, jitter + gap)

    def _glide(self, now: float) -> None:
        """Move the delay towards its target gently (see the module docs)."""
        dt = 0.0 if self._stepped_at is None else min(max(now - self._stepped_at, 0.0), 0.05)
        self._stepped_at = now
        if self.delay_s < self.target_s:
            self.delay_s = min(self.target_s, self.delay_s + RISE_PER_S * dt)
        elif self.delay_s > self.target_s:
            self.delay_s = max(self.target_s, self.delay_s - FALL_PER_S * dt)

    def _put(self, x: float, y: float) -> None:
        at = (round(x), round(y))
        if at != self._at:
            self._at = at
            started = time.perf_counter()
            self._move(*at)
            self._report.moved(time.perf_counter() - started)

    def _ensure_thread(self) -> None:
        if not self._threaded:
            return
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="curslink-pointer", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        next_at = self._clock()
        while not self._stop:
            if not self.active:
                self._wake.wait()
                self._wake.clear()
                next_at = self._clock()
                continue
            try:
                self.step()
            except Exception:  # never let a bad move stop playback
                log.debug("pointer playback step failed", exc_info=True)
            next_at += TICK_S
            spare = next_at - self._clock()
            if spare > 0:
                time.sleep(spare)
            else:
                next_at = self._clock()


class _LinkReport:
    """Every so often while active, logs how moves are arriving: the numbers
    that say whether a laggy pointer is the network, the sender, or this
    computer replaying them."""

    def __init__(self) -> None:
        self._reset(0.0)

    def _reset(self, now: float) -> None:
        self.since = now
        self.count = 0
        self.late: list[float] = []
        self.gaps: list[float] = []
        self.last_arrival: float | None = None
        self.move_s: list[float] = []

    def arrived(self, now: float, late: float) -> None:
        if self.count == 0 and self.since == 0.0:
            self.since = now
        if self.last_arrival is not None:
            self.gaps.append(now - self.last_arrival)
        self.last_arrival = now
        self.count += 1
        self.late.append(late)

    def moved(self, took: float) -> None:
        self.move_s.append(took)

    def flush(self, now: float, delay: float, force: bool = False) -> None:
        span = now - self.since
        if self.count == 0 or (span < REPORT_EVERY_S and not force):
            if force:
                self._reset(0.0)
            return
        ms = 1000.0
        log.info(
            "pointer: %d moves in %.1fs (%.0f/s); arriving late by p50 %.1f / p95 %.1f / max %.1f ms; "
            "longest gap %.0f ms; playing back %.1f ms behind (adapts to the link); moving the pointer took avg %.2f / max %.2f ms",
            self.count, span, self.count / max(span, 1e-6),
            _pct(self.late, 0.5) * ms, _pct(self.late, 0.95) * ms, max(self.late) * ms,
            max(self.gaps, default=0.0) * ms, delay * ms,
            (sum(self.move_s) / len(self.move_s) * ms) if self.move_s else 0.0,
            max(self.move_s, default=0.0) * ms,
        )
        self._reset(0.0)
