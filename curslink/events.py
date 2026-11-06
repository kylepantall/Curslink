"""Status events and commands between a running host or agent and the desktop app.

The desktop app (:mod:`curslink.gui`) runs ``curslink host --events`` or
``curslink agent --events`` as a child process. With ``--events`` the child
prints one JSON object per line on stdout, each prefixed so it can't be
confused with anything else printed there::

    @curslink {"event": "switch", "from": "desktop", "to": "laptop", ...}

and reads one command per line on stdin (``home``, ``gaze pause``,
``gaze resume``, ``gaze toggle``, ``stop``). The child stops when stdin
closes, so it never outlives the app that started it.

Without ``--events`` nothing is printed and :func:`emit` does nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
from typing import Any, Callable, TextIO

PREFIX = "@curslink "

COMMANDS = ("home", "gaze pause", "gaze resume", "gaze toggle", "stop")


class EventChannel:
    def __init__(self) -> None:
        self._out: TextIO | None = None
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._out is not None

    def enable(self, out: TextIO | None = None) -> None:
        self._out = out or sys.stdout

    def disable(self) -> None:
        self._out = None

    def emit(self, kind: str, **data: Any) -> None:
        out = self._out
        if out is None:
            return
        line = PREFIX + json.dumps({"event": kind, **data}, default=str, separators=(",", ":")) + "\n"
        with self._lock:
            try:
                out.write(line)
                out.flush()
            except (OSError, ValueError):  # the app went away
                self._out = None


channel = EventChannel()


def enable(out: TextIO | None = None) -> None:
    """Start printing events (and forward log records as ``log`` events)."""
    channel.enable(out)
    root = logging.getLogger()
    # Log records reach the app as events; printing them as well would show
    # everything twice. (Crashes still print their traceback on stderr.)
    for h in list(root.handlers):
        if type(h) is logging.StreamHandler:
            root.removeHandler(h)
    if not any(isinstance(h, _LogForwarder) for h in root.handlers):
        root.addHandler(_LogForwarder())


def emit(kind: str, **data: Any) -> None:
    channel.emit(kind, **data)


def parse(line: str) -> dict[str, Any] | None:
    """One line of a child's stdout -> the event, or None if it isn't one."""
    line = line.strip()
    if not line.startswith(PREFIX.strip()):
        return None
    try:
        event = json.loads(line[len(PREFIX.strip()):])
    except ValueError:
        return None
    if not isinstance(event, dict) or not isinstance(event.get("event"), str):
        return None
    return event


class _LogForwarder(logging.Handler):
    """Log records as ``log`` events, so the app can show what happened."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - a broken format string
            return
        channel.emit("log", level=record.levelname.lower(), logger=record.name, message=message)


def read_commands(loop: asyncio.AbstractEventLoop, handle: Callable[[str], None],
                  stream: TextIO | None = None) -> threading.Thread:
    """Feed each command line on ``stream`` (stdin) to ``handle`` on ``loop``;
    ``stop`` once the stream closes."""
    stream = stream or sys.stdin

    def send(command: str) -> None:
        try:
            loop.call_soon_threadsafe(handle, command)
        except RuntimeError:  # the loop has already closed
            pass

    def run() -> None:
        try:
            for line in stream:
                command = " ".join(line.split()).lower()
                if command:
                    send(command)
        except (OSError, ValueError):
            pass
        send("stop")

    t = threading.Thread(target=run, name="curslink-commands", daemon=True)
    t.start()
    return t
