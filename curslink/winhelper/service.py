"""The Windows service: keeps an injector running in the console session."""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

from curslink.winhelper import SERVICE_NAME

log = logging.getLogger(__name__)

TICK_S = 1.0
BACKOFF_S = (1.0, 2.0, 5.0, 15.0, 30.0)   # after the injector dies, wait this long (more each time)
STABLE_S = 60.0                           # an injector that lasted this long resets the backoff


def setup_logging(name: str) -> None:
    """Next to the helper in Program Files: only admins can write there, so
    nobody can plant a link where SYSTEM writes its log."""
    folder = Path(sys.executable).parent / "logs"
    try:
        folder.mkdir(exist_ok=True)
        handler = RotatingFileHandler(folder / f"{name}.log", maxBytes=1_000_000, backupCount=1, encoding="utf-8")
    except OSError:
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


class Supervisor:
    """Keeps exactly one injector running, in whichever session is at the
    console: a new one when someone else signs in or switches user, a
    restart (with backoff) if it dies."""

    def __init__(self, command_line: str, console_session: Callable[[], int | None],
                 start: Callable[[int, str], tuple], exited: Callable[[object], bool],
                 stop: Callable[[object], None], clock: Callable[[], float] = time.monotonic):
        self.command_line = command_line
        self._console = console_session
        self._start, self._exited, self._stop = start, exited, stop
        self._clock = clock
        self.child = None          # (process handle, pid, session, started at)
        self._failures = 0
        self._next_start = 0.0

    def tick(self) -> None:
        now = self._clock()
        session = self._console()
        if self.child is not None:
            process, pid, child_session, started = self.child
            if self._exited(process):
                lasted = now - started
                self._failures = 0 if lasted >= STABLE_S else self._failures + 1
                wait = BACKOFF_S[min(self._failures, len(BACKOFF_S) - 1)] if self._failures else 0.0
                log.warning("injector %d stopped after %.0f s; starting another in %.0f s", pid, lasted, wait)
                self._next_start = now + wait
                self.child = None
            elif child_session != session:
                log.info("console moved to session %s: stopping the injector in session %d", session, child_session)
                self._stop(process)
                self.child = None
                self._next_start = now
        if self.child is None and session is not None and now >= self._next_start:
            try:
                process, pid = self._start(session, self.command_line)
            except OSError:
                log.exception("couldn't start the injector in session %d", session)
                self._failures += 1
                self._next_start = now + BACKOFF_S[min(self._failures, len(BACKOFF_S) - 1)]
                return
            log.info("injector %d started in session %d", pid, session)
            self.child = (process, pid, session, now)

    def shutdown(self) -> None:
        if self.child is not None:
            self._stop(self.child[0])
            self.child = None


def run() -> int:
    """Entry point when the service control manager starts us."""
    import ctypes
    from ctypes import wintypes

    from curslink.winhelper import win32

    setup_logging("service")
    adv = win32.adv
    stopping = threading.Event()

    class SERVICE_STATUS(ctypes.Structure):
        _fields_ = [(n, wintypes.DWORD) for n in (
            "dwServiceType", "dwCurrentState", "dwControlsAccepted", "dwWin32ExitCode",
            "dwServiceSpecificExitCode", "dwCheckPoint", "dwWaitHint")]

    MAIN = ctypes.WINFUNCTYPE(None, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR))
    HANDLER = ctypes.WINFUNCTYPE(wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID)

    class SERVICE_TABLE_ENTRYW(ctypes.Structure):
        _fields_ = [("lpServiceName", wintypes.LPWSTR), ("lpServiceProc", MAIN)]

    adv.RegisterServiceCtrlHandlerExW.restype = wintypes.HANDLE
    adv.RegisterServiceCtrlHandlerExW.argtypes = [wintypes.LPCWSTR, HANDLER, wintypes.LPVOID]
    adv.SetServiceStatus.argtypes = [wintypes.HANDLE, ctypes.POINTER(SERVICE_STATUS)]
    state = {"handle": None}

    def report(current: int, wait_ms: int = 0) -> None:
        status = SERVICE_STATUS(0x10, current, 0 if current != 4 else 0x1 | 0x4, 0, 0, 0, wait_ms)
        if state["handle"]:
            adv.SetServiceStatus(state["handle"], ctypes.byref(status))

    @HANDLER
    def handler(control, event_type, event_data, context):
        if control in (1, 5):  # SERVICE_CONTROL_STOP, SHUTDOWN
            report(3, 5000)    # STOP_PENDING
            stopping.set()
        return 0

    @MAIN
    def service_main(argc, argv):
        state["handle"] = adv.RegisterServiceCtrlHandlerExW(SERVICE_NAME, handler, None)
        report(4)  # RUNNING
        log.info("service started")
        supervisor = Supervisor(subprocess.list2cmdline([sys.executable, "inject"]),
                                win32.console_session, win32.start_in_session, win32.has_exited,
                                win32.stop_process)
        try:
            while not stopping.is_set():
                try:
                    supervisor.tick()
                except Exception:
                    log.exception("supervisor tick failed")
                stopping.wait(TICK_S)
        finally:
            supervisor.shutdown()
            log.info("service stopped")
            report(1)  # STOPPED

    table = (SERVICE_TABLE_ENTRYW * 2)(SERVICE_TABLE_ENTRYW(SERVICE_NAME, service_main), SERVICE_TABLE_ENTRYW())
    if not adv.StartServiceCtrlDispatcherW(table):
        log.error("not started by the service manager (error %d)", ctypes.get_last_error())
        return 1
    return 0
