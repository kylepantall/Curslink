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

    _line_seen = Signal(str)
    _exited = Signal(int)

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
        self._exited.emit(code)

    def _on_line(self, line: str) -> None:
        event = events.parse(line)
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

    def _on_exit(self, code: int) -> None:
        up_for = time.monotonic() - self._started_at
        self._proc = None
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
        if up_for >= RESTART_IF_UP_FOR_S and self.role and self.config:
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
