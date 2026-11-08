"""Bringing the window under a file drag forward while it's held.

A drag carried here from another computer isn't one this computer's own
mouse started, and many apps only take the drop once their window is in
front. So while such a drag is held, the window the pointer rests over for
a moment is brought forward, as if you had clicked it: on macOS through
Accessibility (:mod:`curslink.files.macfocus`), on Windows with
SetForegroundWindow (:mod:`curslink.files.winfocus`).
"""

from __future__ import annotations

import logging
import os
import sys

log = logging.getLogger(__name__)


def _platform():
    if sys.platform == "darwin":
        from curslink.files import macfocus as impl
    elif sys.platform == "win32":
        from curslink.files import winfocus as impl
    else:
        return None
    return impl


class FocusFollower:
    """``tick(now)`` repeatedly while a drag is held: once the pointer has
    rested over another window for REST_S, bring that window forward."""

    REST_S = 0.25

    def __init__(self, skip_pids=lambda: set(), impl=None):
        self.skip_pids = skip_pids
        self.impl = impl
        self._candidate = None
        self._since = 0.0
        self._focused = None

    def reset(self) -> None:
        self._candidate = self._focused = None

    def tick(self, now: float) -> None:
        impl = self.impl or _platform()
        if impl is None:
            return
        try:
            hit = impl.probe({os.getpid(), *self.skip_pids()})
            if hit is None:
                return
            key, name, x, y = hit
            if key != self._candidate:
                self._candidate, self._since = key, now
                return
            if key == self._focused or now - self._since < self.REST_S:
                return
            self._focused = key
            log.info("drop: bringing %s forward under the pointer", name)
            impl.bring(key, x, y)
        except Exception:  # pragma: no cover - platform specific
            log.debug("couldn't follow the pointer's window", exc_info=True)
