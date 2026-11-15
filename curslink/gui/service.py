"""Running ``curslink host`` or ``curslink agent`` behind the desktop app.

The app doesn't run the host or agent in its own process: Qt wants the main
thread, the host wants an asyncio loop, input capture and the camera, and a
crash in one shouldn't take the other down. So it starts the same command
the CLI runs, with ``--events`` (see :mod:`curslink.events`), and reads what
happens from its stdout.
"""

from __future__ import annotations

import collections
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from curslink import events
from curslink.update import SKIP_ENV

log = logging.getLogger(__name__)

STOPPED, STARTING, RUNNING, FAILED = "stopped", "starting", "running", "failed"
# A child that ran this long before dying gets restarted rather than reported.
RESTART_IF_UP_FOR_S = 30.0
# A child that sends "beat" events (curslink.app.beat_to_app) and then goes
# quiet this long is stuck: the cursor is put back (Windows' blank cursors
# outlive it, see curslink.cursorhide) ...
STUCK_SHOW_CURSOR_S = 4.0
# ... and quiet this long, it's stopped and started again (its hooks go with it).
STUCK_RESTART_S = 12.0
WATCH_EVERY_MS = 1000
# Ctrl+Alt+Delete or Task Manager with the child this quiet: restarted at once.
STUCK_WAY_OUT_S = 2.0


def service_command(role: str, config: Path) -> list[str]:
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        if exe.stem.lower() == "curslinkw":
            # The windowed launcher; the console build runs the service.
            exe = exe.with_name("curslink.exe")
        cmd = [str(exe)]
    else:
        python = Path(sys.executable)
        if python.stem.lower() == "pythonw":
            python = python.with_name("python.exe")
        cmd = [str(python), "-m", "curslink"]
    return [*cmd, role, "-c", str(config), "--events"]


class Service(QObject):
    """One host or agent child process. Signals arrive on the Qt thread."""

    event = Signal(dict)
    state_changed = Signal(str)
    output = Signal(str)  # anything else it printed, for the activity log

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.state = STOPPED
        self.role: str | None = None
        self.config: Path | None = None
        self.error = ""
        self.tail: collections.deque[str] = collections.deque(maxlen=40)
        self._proc: subprocess.Popen | None = None
        self._started_at = 0.0
        self._stopping = False
        self._lock = threading.Lock()
        # Child threads can't touch Qt objects, so they hand over through these.
        self._line_seen.connect(self._on_line)
        self._exited.connect(self._on_exit)
        # The watchdog: this app is a process of its own, so it still runs
        # when the child is wedged (0.4.99: the host stuck mid-switch with
        # the cursor blank and no hook left to hear Escape).
        self._beat_at: float | None = None
        self._watched_at = time.monotonic()
        self._cursor_put_back = False
        self._killed_stuck: subprocess.Popen | None = None
        self._stuck_restarts: list[float] = []
        # Windows: Ctrl+Alt+Delete or Task Manager coming up frees the cursor
        # (curslink.escapehatch), from here too in case the child is stuck.
        from curslink.escapehatch import escape_wanted

        self.escape_wanted = escape_wanted
        self._way_out: str | None = None
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(WATCH_EVERY_MS)
        self._watchdog.timeout.connect(self._watch)
        self._watchdog.start()

    _line_seen = Signal(str)
    _exited = Signal(object, int)  # (the process, its exit code)

    # -- control ------------------------------------------------------------
    def start(self, role: str, config: Path) -> None:
        if self.is_alive():
            return
        self.role, self.config = role, Path(config)
        self.error = ""
        self.tail.clear()
        self._stopping = False
        env = dict(os.environ)
        # The app installs updates itself; a child that replaced itself would
        # leave the app holding a dead process.
        env[SKIP_ENV] = "gui"
        env["PYTHONUNBUFFERED"] = "1"
        env.setdefault("PYTHONIOENCODING", "utf-8")
        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            self._proc = subprocess.Popen(
                service_command(role, self.config),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                cwd=str(self.config.parent), env=env, **kwargs,
            )
        except OSError as exc:
            self.error = f"Couldn't start Curslink: {exc}"
            log.error("couldn't start %s: %s", role, exc)
            self._set_state(FAILED)
            return
        self._started_at = time.monotonic()
        self._beat_at = None  # watched from its first beat on (older versions send none)
        self._cursor_put_back = False
        log.info("started %s (pid %s): %s", role, self._proc.pid, " ".join(service_command(role, self.config)))
        self._set_state(STARTING)
        proc = self._proc
        threading.Thread(target=self._pump, args=(proc,), name="curslink-service-out", daemon=True).start()

    def stop(self, wait: float = 3.0) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            self._set_state(STOPPED)
            return
        self._stopping = True
        self.send("stop")
        try:
            proc.wait(wait)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(2)
            except subprocess.TimeoutExpired:
                proc.kill()

    def restart(self) -> None:
        role, config = self.role, self.config
        if role is None or config is None:
            return
        self.stop()
        QTimer.singleShot(300, lambda: self.start(role, config))

    def send(self, command: str) -> None:
        with self._lock:
            proc = self._proc
            if proc is None or proc.stdin is None or proc.poll() is not None:
                return
            try:
                proc.stdin.write(command + "\n")
                proc.stdin.flush()
            except (OSError, ValueError):
                pass

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # -- the child's output ---------------------------------------------------
    def _pump(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            self._line_seen.emit(line.rstrip("\r\n"))
        code = proc.wait()
        self._exited.emit(proc, code)

    def _on_line(self, line: str) -> None:
        event = events.parse(line)
        if event is not None and event["event"] == "beat":
            self.beat()
            return
        if event is None:
            if line.strip():
                log.info("%s: %s", self.role or "service", line)
                self.tail.append(line)
                self.output.emit(line)
            return
        if event["event"] == "running":
            self._set_state(RUNNING)
        elif event["event"] == "log" and event.get("level") in ("warning", "error", "critical"):
            self.tail.append(event.get("message", ""))
        self.event.emit(event)

    def beat(self, now: float | None = None) -> None:
        """The child's loop is going (a ``beat`` event)."""
        now = time.monotonic() if now is None else now
        if self._cursor_put_back:
            log.info("%s is answering again after %.1f s", self.role, now - (self._beat_at or now))
        self._beat_at = now
        self._cursor_put_back = False

    def _watch(self, now: float | None = None) -> None:
        """Every WATCH_EVERY_MS: has the child stopped beating?"""
        now = time.monotonic() if now is None else now
        late = now - self._watched_at > 3 * WATCH_EVERY_MS / 1000
        self._watched_at = now
        proc = self._proc
        way_out = self.escape_wanted()
        came_up, self._way_out = way_out if way_out != self._way_out else None, way_out
        if came_up:
            from curslink.cursorhide import restore_windows

            log.info("%s on this PC: putting the cursor back", came_up)
            restore_windows()
        if proc is None or self._beat_at is None or self._stopping or proc.poll() is not None:
            return
        if came_up and now - self._beat_at >= STUCK_WAY_OUT_S:
            # The user is asking for a way out and the child isn't answering:
            # don't make them wait for STUCK_RESTART_S.
            log.error("%s (pid %s) isn't answering (%.0f s) and %s came up: starting it again",
                      self.role, proc.pid, now - self._beat_at, came_up)
            self._beat_at = None
            self._killed_stuck = proc
            try:
                proc.kill()
            except OSError:
                pass
            return
        if late:
            # This app was stuck itself, or the computer slept: the child
            # missed nothing it could have sent; start counting again.
            self._beat_at = now
            return
        quiet = now - self._beat_at
        if quiet >= STUCK_SHOW_CURSOR_S and not self._cursor_put_back:
            self._cursor_put_back = True
            log.warning("%s (pid %s) hasn't answered for %.0f s: putting the cursor back",
                        self.role, proc.pid, quiet)
            from curslink.cursorhide import restore_windows

            restore_windows()
        if quiet >= STUCK_RESTART_S:
            log.error("%s (pid %s) has been stuck for %.0f s: stopping it and starting it again",
                      self.role, proc.pid, quiet)
            self._beat_at = None
            self._killed_stuck = proc
            try:
                proc.kill()
            except OSError:
                pass

    def _on_exit(self, proc: subprocess.Popen, code: int) -> None:
        if proc is not self._proc:
            # The one restart() or a role switch replaced: its exit says
            # nothing about the one running now (taking it for that one left
            # the app thinking nothing ran, so saving the config never
            # restarted the host, which then turned away a phone paired since).
            log.info("previous %s (pid %s) exited (code %s)", self.role, proc.pid, code)
            return
        up_for = time.monotonic() - self._started_at
        self._proc = None
        # A service that was killed can leave Windows' cursors blank (see
        # curslink.cursorhide): put the user's own back.
        from curslink.cursorhide import restore_windows

        restore_windows()
        if self._stopping or code == 0:
            log.info("%s exited (code %s) after %.0fs", self.role, code, up_for)
        else:
            how = f"signal {-code}" if code < 0 else f"exit code {code}"
            log.error("%s died (%s) after %.0fs; last output:\n%s", self.role, how, up_for,
                      "\n".join(self.tail) or "(none)")
        if self._stopping or code == 0:
            self._set_state(STOPPED)
            return
        last = [line for line in self.tail if line.strip()]
        self.error = last[-1] if last else f"Curslink stopped unexpectedly (exit code {code})."
        stuck, self._killed_stuck = self._killed_stuck is proc, None
        if stuck:
            now = time.monotonic()
            self._stuck_restarts = [t for t in self._stuck_restarts if now - t < 300.0] + [now]
            if len(self._stuck_restarts) > 3:
                log.error("%s keeps getting stuck: not starting it again by itself", self.role)
                self.error = "Curslink kept getting stuck, so it was stopped. Start it again from the app."
                stuck = False
                up_for = 0.0
        if (stuck or up_for >= RESTART_IF_UP_FOR_S) and self.role and self.config:
            log.warning("service exited with %s after %.0fs; restarting", code, up_for)
            role, config = self.role, self.config
            QTimer.singleShot(2000, lambda: self.start(role, config))
            self._set_state(STARTING)
            return
        self._set_state(FAILED)

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.state_changed.emit(state)
