"""Keep this computer's screen on while Curslink is linked ("Keep my screen on").

A screen that's part of the layout but not being used directly (the pointer
is on another device, or you're only reading it) dims and sleeps on its own
timer. With ``[host] keep_screen_on`` or ``[agent] keep_screen_on`` set, the
service holds it on while it's linked: a host while at least one device is
connected, an agent while it's connected to its host. It lets go as soon as
the link drops or Curslink stops, so the usual sleep timers apply again.

How, per platform:

* macOS: ``caffeinate -d -i -w <our pid>``, which ends on its own when this
  process does.
* Windows: ``SetThreadExecutionState(ES_CONTINUOUS | ES_DISPLAY_REQUIRED |
  ES_SYSTEM_REQUIRED)`` on the service's event-loop thread; Windows drops it
  when the thread or process ends.
* Linux: ``gnome-session-inhibit --inhibit idle`` (GNOME) or ``systemd-inhibit
  --what=idle`` (KDE and others) around a small watcher that exits when this
  process does. Where ``xdg-screensaver`` is installed it's also told the user
  is active every 50 s, for X11 screensavers that don't follow either.

The phone has its own switch (the Android app's "Keep the screen on").
"""

from __future__ import annotations

import ctypes
import logging
import os
import shutil
import subprocess
import sys
import threading
from typing import Callable

log = logging.getLogger(__name__)

WHY = "Curslink is linked to another device"
# How often (s) to tell xdg-screensaver the user is still here.
POKE_S = 50.0

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002


class Backend:
    """Holds the screen on and lets it go; ``ok`` False where it can't."""

    ok = False
    name = "none"

    def hold(self) -> bool:
        return False

    def release(self) -> None:
        pass


class _Process(Backend):
    """A helper process that holds the screen on until it's stopped."""

    ok = True

    def __init__(self, name: str, argv: list[str]):
        self.name = name
        self.argv = argv
        self.proc: subprocess.Popen | None = None

    def hold(self) -> bool:
        if self.proc is not None and self.proc.poll() is None:
            return True
        try:
            self.proc = subprocess.Popen(
                self.argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as e:
            log.warning("couldn't keep the screen on (%s): %s", self.name, e)
            self.proc = None
            return False
        return True

    def release(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


class _Windows(Backend):
    ok = True
    name = "SetThreadExecutionState"

    def _set(self, flags: int) -> bool:
        try:
            return bool(ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(flags)))  # type: ignore[attr-defined]
        except (AttributeError, OSError) as e:
            log.warning("couldn't keep the screen on: %s", e)
            return False

    def hold(self) -> bool:
        return self._set(ES_CONTINUOUS | ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED)

    def release(self) -> None:
        self._set(ES_CONTINUOUS)


class _Linux(_Process):
    """An idle inhibitor, plus xdg-screensaver pokes where that's installed."""

    def __init__(self, name: str, argv: list[str], poke: list[str] | None):
        super().__init__(name, argv)
        self.poke = poke
        self._stop: threading.Event | None = None

    def hold(self) -> bool:
        ok = super().hold() if self.argv else False
        if self.poke and self._stop is None:
            stop = self._stop = threading.Event()

            def poke() -> None:
                while True:
                    try:
                        subprocess.run(self.poke, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, timeout=10)
                    except (OSError, subprocess.SubprocessError):
                        pass
                    if stop.wait(POKE_S):
                        return

            threading.Thread(target=poke, name="curslink-keepawake", daemon=True).start()
            ok = True
        return ok

    def release(self) -> None:
        super().release()
        if self._stop is not None:
            self._stop.set()
            self._stop = None


def _watch_parent(pid: int) -> list[str]:
    """A command that runs until process ``pid`` has gone."""
    return ["sh", "-c", f"while kill -0 {pid} 2>/dev/null; do sleep 5; done"]


def backend(platform: str = sys.platform, which: Callable[[str], str | None] = shutil.which,
            pid: int | None = None) -> Backend:
    pid = os.getpid() if pid is None else pid
    if platform == "darwin":
        tool = which("caffeinate") or "/usr/bin/caffeinate"
        return _Process("caffeinate", [tool, "-d", "-i", "-w", str(pid)])
    if platform == "win32":
        return _Windows()
    if platform.startswith("linux"):
        poke = [p, "reset"] if (p := which("xdg-screensaver")) else None
        if gnome := which("gnome-session-inhibit"):
            return _Linux("gnome-session-inhibit", [gnome, "--inhibit", "idle", "--app-id", "curslink",
                                                    "--reason", WHY, *_watch_parent(pid)], poke)
        if systemd := which("systemd-inhibit"):
            return _Linux("systemd-inhibit", [systemd, "--what=idle", "--who=Curslink", f"--why={WHY}",
                                              "--mode=block", *_watch_parent(pid)], poke)
        if poke:
            return _Linux("xdg-screensaver", [], poke)
    return Backend()


class ScreenKeeper:
    """Holds the screen on while :meth:`linked` says so (switched on in config).

    Call it from one thread (the service's event loop): on Windows the hold
    belongs to the thread that took it.
    """

    def __init__(self, enabled: bool, impl: Backend | None = None):
        self.enabled = False
        self.impl = impl
        self.held = False
        self.is_linked = False
        self.set_enabled(enabled)

    def set_enabled(self, on: bool) -> None:
        """Switched on or off from Settings or the tray, without a restart."""
        if on and self.impl is None:
            self.impl = backend()
            if not self.impl.ok:
                log.warning("keep my screen on: not supported on this computer")
        self.enabled = on
        self.linked(self.is_linked)

    def command(self, line: str) -> bool:
        """``keep_screen_on on|off`` from the app; True if it was one."""
        if line in ("keep_screen_on on", "keep_screen_on off"):
            self.set_enabled(line.endswith(" on"))
            return True
        return False

    def linked(self, on: bool) -> None:
        self.is_linked = on
        want = self.enabled and on and self.impl is not None
        if want == self.held:
            return
        if want:
            if self.impl.hold():
                self.held = True
                log.info("keeping the screen on while linked (%s)", self.impl.name)
        else:
            assert self.impl is not None
            self.impl.release()
            self.held = False
            log.info("letting the screen sleep again")

    def close(self) -> None:
        self.linked(False)
