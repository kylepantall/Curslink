"""Reading lines from stdin without leaving a read waiting on it.

On Windows a pipe handle does one thing at a time: while one thread sits in
a blocking read on stdin, anything else that touches that handle waits for
the read to end. A C runtime starting up inside a DLL asks what kind of
file stdin is (``GetFileType``), so *loading a DLL* waits too, with
Windows' loader lock held, and then no thread can start either.

That is what made ``import cv2`` take from 10 seconds to minutes in a
process that was reading commands on stdin (the host, 0.4.98 to 0.4.100,
and head tracking's camera child up to 0.4.114, where the camera only
opened as the host stopped and closed the pipe: Kyle's "Follow my head
doesn't activate the camera"). So on Windows the pipe is only looked at
(``PeekNamedPipe``), and read when there is something in it.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Callable, Iterator, TextIO

POLL_S = 0.02


def split(peek: Callable[[], int | None], read: Callable[[int], bytes], encoding: str = "utf-8",
          sleep: Callable[[float], None] = time.sleep, poll: float = POLL_S) -> Iterator[str]:
    """Lines (with their newline) from a pipe: ``peek()`` says how many bytes
    wait in it (None: the other end closed), ``read(n)`` takes them."""
    held = b""
    while True:
        waiting = peek()
        if waiting is None:
            break
        if not waiting:
            sleep(poll)
            continue
        data = read(waiting)
        if not data:
            break
        held += data
        while b"\n" in held:
            line, held = held.split(b"\n", 1)
            yield line.decode(encoding, "replace") + "\n"
    if held:
        yield held.decode(encoding, "replace")


def _windows_pipe(stream: TextIO):
    """(peek, read) for ``stream`` if it's a pipe on Windows, else None."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        fd = stream.fileno()
        handle = msvcrt.get_osfhandle(fd)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetFileType.argtypes = [wintypes.HANDLE]
        kernel32.GetFileType.restype = wintypes.DWORD
        kernel32.PeekNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, wintypes.LPDWORD,
                                           wintypes.LPDWORD, wintypes.LPDWORD]
        kernel32.PeekNamedPipe.restype = wintypes.BOOL
        if kernel32.GetFileType(handle) != 3:  # FILE_TYPE_PIPE
            return None
    except Exception:  # no stdin, not a real file, no ctypes: read it the plain way
        return None

    def peek() -> int | None:
        waiting = wintypes.DWORD(0)
        if not kernel32.PeekNamedPipe(handle, None, 0, None, ctypes.byref(waiting), None):
            return None  # (broken pipe: the writer went away)
        return waiting.value

    return peek, lambda n: os.read(fd, n)


def lines(stream: TextIO | None = None) -> Iterator[str]:
    """Each line of ``stream`` (stdin) until it closes, like iterating it,
    but with no read left waiting on a Windows pipe in between."""
    stream = sys.stdin if stream is None else stream
    if stream is None:
        return
    pipe = _windows_pipe(stream)
    if pipe is None:
        yield from stream
        return
    yield from split(pipe[0], pipe[1], getattr(stream, "encoding", None) or "utf-8")
