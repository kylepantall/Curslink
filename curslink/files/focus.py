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


def from_helper(line: str) -> None:
    """The overlay helper's ``focus <key>``, run straight from its stdin
    reader (not its UI thread, which a drag's modal loop may hold).

    The helper is the drag's source, so it's the process Windows sees the
    drag's input going to, which is what lets a process move another window
    to the front; on a Mac it's another try at activating the app."""
    impl = _platform()
    token = line.split(None, 1)[1].strip() if " " in line.strip() else ""
    if impl is None or not token.lstrip("-").isdigit():
        return
    try:
        impl.bring_here(int(token))
    except Exception:  # pragma: no cover - platform specific
        log.debug("couldn't bring %s forward", token, exc_info=True)


class FocusFollower:
    """``tick(now)`` repeatedly while a drag is held: once the pointer has
    rested over another window for REST_S, bring that window forward, and
    keep at it (every RETRY_S, up to TRIES times) while it isn't in front:
    Windows and macOS can each refuse, or something can take it back.

    ``ready()`` (read from input threads): is the window under the pointer
    in front, so a drop now lands in it? ``bring_now()``: don't wait for the
    pointer to rest (the user let go)."""

    REST_S = 0.2
    RETRY_S = 0.3
    TRIES = 5

    def __init__(self, skip_pids=lambda: set(), impl=None, ask_helper=None):
        self.skip_pids = skip_pids
        self.impl = impl
        self.ask_helper = ask_helper  # (token) -> None: the helper's go at it
        self.reset()

    def reset(self) -> None:
        self._candidate = self._focused = None
        self._since = self._last = 0.0
        self._tries = 0
        self._front = True

    def ready(self) -> bool:
        return self._front

    def _impl(self):
        return self.impl or _platform()

    def _is_front(self, impl, key) -> bool:
        check = getattr(impl, "is_front", None)
        if check is None:  # can't tell: trust one go at it
            return key == self._focused
        return bool(check(key))

    def _bring(self, impl, key, name, x, y) -> None:
        self._focused = key
        log.info("drop: bringing %s forward under the pointer%s", name,
                 f" (try {self._tries})" if self._tries > 1 else "")
        impl.bring(key, x, y)
        front = self._is_front(impl, key)
        if self.ask_helper is not None and not front:
            token = getattr(impl, "token", lambda k: k)(key)
            self.ask_helper(token)
        from curslink.files.dragdebug import trace

        trace("focus", target=repr(name), key=key, tried=self._tries, front_after=front,
              helper_asked=self.ask_helper is not None and not front)

    def _probe(self, impl):
        return impl.probe({os.getpid(), *self.skip_pids()})

    def tick(self, now: float) -> None:
        impl = self._impl()
        if impl is None:
            return
        try:
            hit = self._probe(impl)
            if hit is None:  # the taskbar, the menu bar, or one of ours
                self._candidate, self._front = None, True
                return
            key, name, x, y = hit
            if key != self._candidate:
                self._candidate, self._since, self._tries = key, now, 0
                self._front = self._is_front(impl, key)
                from curslink.files.dragdebug import trace

                trace("hover", target=repr(name), key=key, in_front=self._front)
                return
            self._front = self._is_front(impl, key)
            if self._front or now - self._since < self.REST_S:
                return
            if self._tries >= self.TRIES or (self._tries and now - self._last < self.RETRY_S):
                return
            self._tries += 1
            self._last = now
            self._bring(impl, key, name, x, y)
            self._front = self._is_front(impl, key)
        except Exception:  # pragma: no cover - platform specific
            log.debug("couldn't follow the pointer's window", exc_info=True)

    def bring_now(self) -> bool:
        """Bring the window under the pointer forward now if it isn't.
        True if it had to (so give it a moment before dropping)."""
        impl = self._impl()
        if impl is None:
            return False
        try:
            hit = self._probe(impl)
            if hit is None:
                return False
            key, name, x, y = hit
            if self._is_front(impl, key):
                self._front = True
                return False
            if key != self._candidate:
                self._candidate, self._tries = key, 0
            self._tries += 1
            self._bring(impl, key, name, x, y)
            self._front = self._is_front(impl, key)
            return True
        except Exception:  # pragma: no cover - platform specific
            log.debug("couldn't bring the pointer's window forward", exc_info=True)
            return False
