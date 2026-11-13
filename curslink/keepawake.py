"""Keep this computer's screen on while Curslink is linked ("Keep my screen on").

A screen that's part of the layout but not being used directly (the pointer
is on another device, or you're only reading it) dims and sleeps on its own
timer. With ``[host] keep_screen_on`` or ``[agent] keep_screen_on`` set, the
service holds it on while it's linked: a host while at least one device is
connected, an agent while it's connected to its host. It lets go as soon as
the link drops or Curslink stops, so the usual sleep timers apply again.

How, per platform:

* macOS: ``caffeinate -d -i -w <our pid>``, which ends on its own when this
  process does, and ``caffeinate -u`` every 50 s (user activity) so the screen
  saver and its lock stay away too.
* Windows: ``SetThreadExecutionState(ES_CONTINUOUS | ES_DISPLAY_REQUIRED |
  ES_SYSTEM_REQUIRED)`` on the service's event-loop thread (Windows drops it
  when the thread or process ends), and every 50 s a timer reset plus a
  zero-length mouse move, because the screen saver and "lock after idle"
  policies go by the last input, not by that.
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
# How often (s) to tell the system the user is still here (and check the helper).
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


class _Poker:
    """Calls ``poke`` every POKE_S seconds on its own thread until stopped.

    Holding the display awake isn't always enough: a screen saver or a
    "lock after N minutes idle" policy goes by the last input, so the
    backends also tell the system the user is still here."""

    def __init__(self, poke: Callable[[], None] | None):
        self.poke = poke
        self._stop: threading.Event | None = None

    def start(self) -> None:
        if self.poke is None or self._stop is not None:
            return
        stop = self._stop = threading.Event()
        poke = self.poke

        def run() -> None:
            while not stop.is_set():
                try:
                    poke()
                except Exception as e:  # noqa: BLE001 - keep poking whatever one try did
                    log.debug("keep-awake poke failed: %s", e)
                if stop.wait(POKE_S):
                    return

        threading.Thread(target=run, name="curslink-keepawake", daemon=True).start()

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
            self._stop = None


def _run(argv: list[str]) -> None:
    subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=10, check=False)


class _Process(Backend):
    """A helper process that holds the screen on until it's stopped, plus
    optional pokes (which also start the helper again if it ended)."""

    ok = True

    def __init__(self, name: str, argv: list[str], poke: list[str] | None = None):
        self.name = name
        self.argv = argv
        self.poke = poke
        self.proc: subprocess.Popen | None = None
        self._poker = _Poker(self._poke if (poke or argv) else None)

    def _spawn(self) -> bool:
        if not self.argv:
            return False
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

    def _poke(self) -> None:
        if self.argv and self.proc is not None and self.proc.poll() is not None:
            log.info("the screen keep-awake helper (%s) ended (code %s); starting it again",
                     self.name, self.proc.returncode)
            self.proc = None
            self._spawn()
        if self.poke:
            _run(self.poke)

    def hold(self) -> bool:
        ok = self._spawn()
        if self.poke:
            ok = True
        if ok:
            self._poker.start()
        return ok

    def release(self) -> None:
        self._poker.stop()
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


class _Windows(Backend):
    """``SetThreadExecutionState`` keeps the display on, but a screen saver,
    or a policy that locks the PC after a while without input, goes by the
    last input. So every POKE_S the timers are reset and a zero-length mouse
    move (it doesn't move the pointer) counts as input, as mouse jigglers do."""

    ok = True
    name = "SetThreadExecutionState"

    def __init__(self):
        self._poker = _Poker(self._poke)

    def _set(self, flags: int) -> bool:
        try:
            return bool(ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(flags)))  # type: ignore[attr-defined]
        except (AttributeError, OSError) as e:
            log.warning("couldn't keep the screen on: %s", e)
            return False

    def _poke(self) -> None:
        self._set(ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED)  # resets the idle timers once
        _zero_mouse_move()

    def hold(self) -> bool:
        ok = self._set(ES_CONTINUOUS | ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED)
        self._poker.start()
        return ok

    def release(self) -> None:
        self._poker.stop()
        self._set(ES_CONTINUOUS)


def _zero_mouse_move() -> None:
    """Windows: input that doesn't move the pointer, so the PC counts as in use."""
    from ctypes import wintypes

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    MOUSEEVENTF_MOVE = 0x0001
    event = INPUT(type=0, u=_U(mi=MOUSEINPUT(0, 0, 0, MOUSEEVENTF_MOVE, 0, 0)))
    ctypes.windll.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))  # type: ignore[attr-defined]


def _watch_parent(pid: int) -> list[str]:
    """A command that runs until process ``pid`` has gone."""
    return ["sh", "-c", f"while kill -0 {pid} 2>/dev/null; do sleep 5; done"]


def backend(platform: str = sys.platform, which: Callable[[str], str | None] = shutil.which,
            pid: int | None = None) -> Backend:
    pid = os.getpid() if pid is None else pid
    if platform == "darwin":
        tool = which("caffeinate") or "/usr/bin/caffeinate"
        # -d/-i hold the display and the Mac awake; "-u" every POKE_S says the
        # user is active, which keeps the screen saver (and its lock) away too.
        return _Process("caffeinate", [tool, "-d", "-i", "-w", str(pid)], [tool, "-u", "-t", "2"])
    if platform == "win32":
        return _Windows()
    if platform.startswith("linux"):
        poke = [p, "reset"] if (p := which("xdg-screensaver")) else None
        if gnome := which("gnome-session-inhibit"):
            return _Process("gnome-session-inhibit", [gnome, "--inhibit", "idle", "--app-id", "curslink",
                                                    "--reason", WHY, *_watch_parent(pid)], poke)
        if systemd := which("systemd-inhibit"):
            return _Process("systemd-inhibit", [systemd, "--what=idle", "--who=Curslink", f"--why={WHY}",
                                              "--mode=block", *_watch_parent(pid)], poke)
        if poke:
            return _Process("xdg-screensaver", [], poke)
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
