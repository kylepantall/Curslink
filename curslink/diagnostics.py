"""Logs and crash reports.

Every Curslink process (the app, the host or agent it runs, the CLI) writes a
rotating log file, and on a crash a report that says what happened:

* a Python exception nobody caught (on any thread) is written to
  ``crash-<when>-<process>.txt`` with the traceback, every thread's stack,
  the versions involved and the last few hundred log lines;
* a native crash (a segfault in Qt, MediaPipe or OpenCV) can't run Python
  code, so :mod:`faulthandler` writes the stacks to ``fault-<process>.log``
  as it dies, and the next start turns that into a crash report.

Where the files go:

* macOS: ``~/Library/Logs/Curslink``
* Windows: ``%LOCALAPPDATA%\\Curslink\\Logs``
* Linux: ``$XDG_STATE_HOME/curslink/logs`` (``~/.local/state/curslink/logs``)

``CURSLINK_LOG_DIR`` overrides it (as does ``CURSLINK_HOME``, under ``logs``), and ``CURSLINK_DEBUG=1`` logs everything
at debug level.
"""

from __future__ import annotations

import collections
import datetime as _dt
import faulthandler
import io
import logging
import logging.handlers
import os
import platform
import sys
import threading
import time
import traceback
import zipfile
from pathlib import Path
from types import TracebackType
from typing import Callable

from curslink import __version__

log = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(process)d %(threadName)s] %(name)s: %(message)s"
MAX_LOG_BYTES = 2 * 1024 * 1024
LOG_BACKUPS = 4
KEEP_CRASHES = 20
RECENT_LINES = 400

_state: dict = {}
_lock = threading.Lock()
_crash_listeners: list[Callable[[Path], None]] = []


def log_dir() -> Path:
    override = os.environ.get("CURSLINK_LOG_DIR")
    home = os.environ.get("CURSLINK_HOME")
    if override:
        path = Path(override).expanduser()
    elif home:
        path = Path(home).expanduser() / "logs"
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Logs" / "Curslink"
    elif sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        path = Path(base) / "Curslink" / "Logs"
    else:
        base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
        path = Path(base) / "curslink" / "logs"
    return path


def debug_wanted() -> bool:
    return os.environ.get("CURSLINK_DEBUG", "").strip().lower() not in ("", "0", "false", "no")


class _Recent(logging.Handler):
    """The last few hundred formatted lines, for crash reports."""

    def __init__(self, size: int = RECENT_LINES):
        super().__init__(logging.DEBUG)
        self.lines: collections.deque[str] = collections.deque(maxlen=size)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(self.format(record))
        except Exception:  # pragma: no cover - a broken format string
            pass


def setup(process: str, verbose: bool = False, console: bool = True) -> Path | None:
    """Start logging to a file for ``process`` (``gui``, ``host``, ``agent``,
    ``cli``) and catch crashes. Safe to call more than once; returns the log
    file, or None if the folder can't be written."""
    with _lock:
        if _state.get("process") == process:
            return _state.get("log_file")
        level = logging.DEBUG if (verbose or debug_wanted()) else logging.INFO
        root = logging.getLogger()
        root.setLevel(logging.DEBUG)
        formatter = logging.Formatter(LOG_FORMAT)

        if console and sys.stderr is not None and not any(type(h) is logging.StreamHandler for h in root.handlers):
            stream = logging.StreamHandler()
            stream.setLevel(level)
            stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
            root.addHandler(stream)

        recent = _Recent()
        recent.setFormatter(formatter)
        root.addHandler(recent)

        log_file: Path | None = None
        folder = log_dir()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            log_file = folder / f"{process}.log"
            handler = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=MAX_LOG_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8", delay=False)
            # The file always gets debug detail; the console follows --verbose.
            handler.setLevel(logging.DEBUG if level == logging.DEBUG else logging.INFO)
            handler.setFormatter(formatter)
            root.addHandler(handler)
        except OSError as exc:
            log_file = None
            print(f"curslink: can't write logs to {folder}: {exc}", file=sys.stderr)

        _state.update(process=process, log_file=log_file, recent=recent, folder=folder)
        _collect_native_crash(process)
        _install_fault_handler(process)
        _install_hooks()
        _prune(folder)

    log.info("Curslink %s (%s) starting: %s, Python %s, pid %d", __version__, process,
             platform_summary(), platform.python_version(), os.getpid())
    if log_file:
        log.info("logging to %s", log_file)
    return log_file


def current_log_file() -> Path | None:
    return _state.get("log_file")


def on_crash(listener: Callable[[Path], None]) -> None:
    """Call ``listener(report_path)`` after a crash report is written."""
    _crash_listeners.append(listener)


# ----------------------------------------------------------------- crashes

def _install_fault_handler(process: str) -> None:
    folder: Path = _state["folder"]
    try:
        f = open(folder / f"fault-{process}.log", "w", encoding="utf-8")  # noqa: SIM115 - kept open
    except OSError:
        f = sys.stderr if sys.stderr is not None else None
    if f is None:
        return
    old = _state.get("fault_file")
    _state["fault_file"] = f
    try:
        faulthandler.enable(file=f, all_threads=True)
    except (RuntimeError, ValueError, io.UnsupportedOperation):  # no usable file descriptor
        return
    if old not in (None, sys.stderr):
        old.close()


def _collect_native_crash(process: str) -> None:
    """A previous run that died in native code left its stacks behind."""
    folder: Path = _state["folder"]
    fault = folder / f"fault-{process}.log"
    try:
        text = fault.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return
    if not text:
        return
    when = _dt.datetime.fromtimestamp(fault.stat().st_mtime)
    previous_log = ""
    log_file = folder / f"{process}.log"
    try:
        previous_log = "\n".join(log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-RECENT_LINES:])
    except OSError:
        pass
    body = [
        f"Curslink {process} crashed in native code (Python couldn't catch it).",
        f"When: {when.isoformat(timespec='seconds')}",
        "",
        "== Where each thread was (faulthandler) ==",
        text,
        "",
        "== The log before it crashed ==",
        previous_log or "(none)",
    ]
    report = _write_report(process, "native", "\n".join(body), when)
    if report:
        log.error("the last %s run crashed; report saved to %s", process, report)
        _state["pending_native"] = report


def last_native_crash() -> Path | None:
    """The report for a native crash found at start-up, if there was one."""
    return _state.get("pending_native")


def _install_hooks() -> None:
    if _state.get("hooks"):
        return
    _state["hooks"] = True
    previous_sys = sys.excepthook

    def sys_hook(kind: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
        if issubclass(kind, KeyboardInterrupt):
            previous_sys(kind, exc, tb)
            return
        report_exception(exc, where="main thread", tb=tb)
        previous_sys(kind, exc, tb)

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit or args.exc_value is None:
            return
        name = args.thread.name if args.thread else "a thread"
        report_exception(args.exc_value, where=f"thread {name}", tb=args.exc_traceback)

    def unraisable_hook(args) -> None:  # sys.UnraisableHookArgs
        exc = args.exc_value
        what = getattr(args, "err_msg", None) or "Exception ignored"
        log.error("%s: %r", what, args.object, exc_info=(args.exc_type, exc, args.exc_traceback) if exc else None)

    sys.excepthook = sys_hook
    threading.excepthook = thread_hook
    sys.unraisablehook = unraisable_hook


def report_exception(exc: BaseException, where: str = "", tb: TracebackType | None = None) -> Path | None:
    """Log ``exc`` and write a crash report. Returns the report's path."""
    tb = tb or exc.__traceback__
    process = _state.get("process", "curslink")
    log.critical("unhandled %s in %s: %s", type(exc).__name__, where or "Curslink", exc,
                 exc_info=(type(exc), exc, tb))
    body = [
        f"Curslink {process} hit an error it didn't handle{' in ' + where if where else ''}.",
        f"When: {_dt.datetime.now().isoformat(timespec='seconds')}",
        "",
        "== The error ==",
        "".join(traceback.format_exception(type(exc), exc, tb)).rstrip(),
        "",
        "== Every thread ==",
        all_stacks(),
        "",
        "== The log before it ==",
        recent_log() or "(none)",
    ]
    report = _write_report(process, "error", "\n".join(body))
    if report:
        log.critical("crash report saved to %s", report)
        for listener in list(_crash_listeners):
            try:
                listener(report)
            except Exception:  # pragma: no cover - a listener mustn't loop us
                pass
    return report


def all_stacks() -> str:
    names = {t.ident: t.name for t in threading.enumerate()}
    out = []
    for ident, frame in sys._current_frames().items():  # noqa: SLF001 - this is what it's for
        out.append(f"--- {names.get(ident, 'thread')} ({ident}) ---")
        out.append("".join(traceback.format_stack(frame)).rstrip())
    return "\n".join(out)


def recent_log() -> str:
    recent: _Recent | None = _state.get("recent")
    return "\n".join(recent.lines) if recent else ""


def environment() -> str:
    lines = [
        f"Curslink: {__version__}",
        f"Process: {_state.get('process', '?')} (pid {os.getpid()})",
        f"Platform: {platform_summary()}",
        f"Python: {sys.version.split()[0]} ({sys.executable})",
        f"Frozen: {bool(getattr(sys, 'frozen', False))}",
    ]
    for module in ("PySide6", "mediapipe", "cv2", "numpy", "pynput", "zeroconf"):
        mod = sys.modules.get(module)
        if mod is not None:
            lines.append(f"{module}: {getattr(mod, '__version__', '?')}")
    qt = sys.modules.get("PySide6.QtCore")
    if qt is not None:
        lines.append(f"Qt: {qt.qVersion()}")
    return "\n".join(lines)


def platform_summary() -> str:
    if sys.platform == "darwin":
        name = f"macOS {platform.mac_ver()[0]}"
    elif sys.platform == "win32":
        name = f"Windows {platform.version()}"
    else:
        name = f"{platform.system()} {platform.release()}"
        session = os.environ.get("XDG_SESSION_TYPE")
        desktop = os.environ.get("XDG_CURRENT_DESKTOP")
        if session or desktop:
            name += f" ({', '.join(x for x in (desktop, session) if x)})"
    return f"{name} {platform.machine()}"


def _write_report(process: str, kind: str, body: str, when: _dt.datetime | None = None) -> Path | None:
    folder: Path | None = _state.get("folder")
    if folder is None:
        return None
    when = when or _dt.datetime.now()
    path = folder / f"crash-{when:%Y%m%d-%H%M%S}-{process}-{kind}.txt"
    text = f"{body}\n\n== Environment ==\n{environment()}\n"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError:
        try:
            if sys.stderr is not None:  # None in the windowed app
                sys.stderr.write(text)
        except Exception:
            pass
        return None
    return path


def _prune(folder: Path) -> None:
    try:
        crashes = sorted(folder.glob("crash-*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for old in crashes[KEEP_CRASHES:]:
        try:
            old.unlink()
        except OSError:
            pass


def crash_reports() -> list[Path]:
    folder = _state.get("folder") or log_dir()
    try:
        return sorted(folder.glob("crash-*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []


def write_bundle(dest: Path) -> Path:
    """Zip the logs and crash reports (no pairing keys) into ``dest``."""
    folder = _state.get("folder") or log_dir()
    dest = Path(dest)
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("environment.txt", environment() + "\n\n== Every thread ==\n" + all_stacks() + "\n")
        if folder.exists():
            for f in sorted(folder.iterdir()):
                if f.is_file() and (f.suffix in (".txt", ".log") or ".log." in f.name):
                    z.write(f, f.name)
    return dest


def bundle_name() -> str:
    return f"curslink-diagnostics-{time.strftime('%Y%m%d-%H%M%S')}.zip"


def qt_message_handler(mode, context, message: str) -> None:
    """Route Qt's own warnings into the log (install with qInstallMessageHandler)."""
    from PySide6.QtCore import QtMsgType

    qt_log = logging.getLogger("qt")
    where = ""
    if context is not None and getattr(context, "file", None):
        where = f" ({context.file}:{context.line})"
    level = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }.get(mode, logging.WARNING)
    qt_log.log(level, "%s%s", message, where)
