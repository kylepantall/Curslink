"""The injector: SYSTEM, in the signed-in user's session, on the input desktop.

Started by the service (:mod:`.service`), one per console session. It
serves one agent at a time on ``\\\\.\\pipe\\curslink-input-<session>``,
which only that session's user (and SYSTEM) can open and which refuses
connections from other computers.
"""

from __future__ import annotations

import logging
import os
import time

from curslink.winhelper import WIRE_VERSION, pipe_name, wire

log = logging.getLogger(__name__)


def serve_one(handle: int, session: int, sink, follower) -> int:
    """Read one client's commands until it goes away. How many were applied."""
    import msvcrt

    from curslink.winhelper import win32

    try:
        pid = win32.wait_for_client(handle)
    except OSError:
        win32.k32.CloseHandle(handle)
        raise
    fd = msvcrt.open_osfhandle(handle, 0)
    count = 0
    with os.fdopen(fd, "r+b", buffering=0) as raw:
        if win32.session_of(pid) != session:
            log.warning("refused process %d: it isn't in session %d", pid, session)
            return 0
        log.info("agent (process %d) connected", pid)
        raw.write(wire.encode("hello", WIRE_VERSION))
        reader = _lines(raw)
        try:
            for line in reader:
                command = wire.decode(line)
                if command is None:
                    continue
                follower.follow()
                try:
                    wire.apply(sink, command)
                    count += 1
                except Exception:
                    log.exception("couldn't inject %s", command[0])
        except OSError as exc:
            log.info("agent went away (%s)", exc)
    log.info("agent (process %d) disconnected after %d commands", pid, count)
    return count


def _lines(raw):
    """Lines from an unbuffered pipe, reading whatever has arrived at once."""
    pending = b""
    while True:
        chunk = raw.read(4096)
        if not chunk:
            return
        pending += chunk
        *lines, pending = pending.split(b"\n")
        yield from lines


def main() -> int:
    from curslink.inputs.pynput_backend import PynputSink
    from curslink.winhelper import win32
    from curslink.winhelper.service import setup_logging

    session = win32.this_session()
    setup_logging(f"inject-{session}")
    log.info("input helper for session %d starting", session)
    sink = PynputSink()
    follower = win32.DesktopFollower()
    while True:
        sid = win32.session_user_sid(session)
        if sid is None:
            time.sleep(2.0)  # nobody signed in to it (yet)
            continue
        try:
            handle = win32.create_pipe(pipe_name(session), win32.pipe_sddl(sid))
            serve_one(handle, session, sink, follower)
        except OSError:
            log.exception("pipe failed")
            time.sleep(1.0)
