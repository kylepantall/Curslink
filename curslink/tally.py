"""A rate-limited log line for a stream of small input events (scrolls),
so a diagnostics bundle shows whether they arrived and with what values
without one line per event."""

from __future__ import annotations

import logging
import time
from typing import Callable

log = logging.getLogger("curslink.input")


class Tally:
    def __init__(self, label: str, every_s: float = 2.0, clock: Callable[[], float] = time.monotonic):
        self.label = label
        self.every_s = every_s
        self.clock = clock
        self._reset(None)

    def _reset(self, now: float | None) -> None:
        self.since = now
        self.count = 0
        self.dx = self.dy = 0.0
        self.last: tuple[float, float] | None = None

    def add(self, dx: float, dy: float) -> None:
        now = self.clock()
        if self.since is None:
            self.since = now
        self.count += 1
        self.dx += dx
        self.dy += dy
        self.last = (dx, dy)
        if self.count == 1 and self.since == now and self._first_ever:
            self._first_ever = False
            self._flush(now)  # the very first one straight away: proves the path works
            return
        if now - self.since >= self.every_s:
            self._flush(now)

    _first_ever = True

    def _flush(self, now: float) -> None:
        log.info("%s: %d in %.1fs, total dx %+.2f dy %+.2f, last %s", self.label, self.count,
                 now - (self.since or now), self.dx, self.dy,
                 "(%+.2f, %+.2f)" % self.last if self.last else "-")
        self._reset(None)
