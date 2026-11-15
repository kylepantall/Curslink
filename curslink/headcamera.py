"""Head tracking's camera, in a process of its own.

Head tracking (curslink.headfollow) needs to know where the head is, from
this computer's camera when gaze switching isn't already watching it. The
camera's libraries (OpenCV, MediaPipe) are big: on a Windows host, loading
them on a thread of the host's own sat in a DLL load for minutes at a time
(Kyle's PC, 0.4.98 to 0.4.100), with every other thread of the host waiting
on it, the hooks that hold the mouse among them. So they're loaded here
instead, in a child the host only reads lines from: whatever the camera
does, the host carries on.

The child (``_head-camera <index>``) prints ``h <right> <down> <away>`` (mm,
as curslink.headfollow.mediapipe_head gives them) for each frame a face is
in, and its log on stderr, which the host puts in its own. It stops when
its stdin closes (the host went away) or it's terminated.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)

RESTARTS = 3  # a camera process that dies is started again this many times at most
RESTART_AFTER_S = 5.0


def command(camera: int, name: str | None = None) -> list[str]:
    """How to start the child: a packaged build runs its own executable.
    ``name``: the camera's name, for the child to find it by if its number
    has changed (curslink.gaze.cameras.resolve: seconds of PowerShell on
    Windows, which is why it's the child that asks)."""
    more = [str(int(camera))] + ([name] if name else [])
    if getattr(sys, "frozen", False):
        return [sys.executable, "_head-camera", *more]
    return [sys.executable, "-m", "curslink.headcamera", *more]


def parse(line: str) -> tuple[float, float, float] | None:
    """A line the child printed: where the head is, or None."""
    parts = line.split()
    if len(parts) != 4 or parts[0] != "h":
        return None
    try:
        return float(parts[1]), float(parts[2]), float(parts[3])
    except ValueError:
        return None


class HeadCameraProcess:
    """The host's handle to the child. ``on_head(t, (right, down, away))``
    is called on a reader thread for each sighting, ``t`` by this process's
    clock (time.monotonic)."""

    def __init__(self, camera: int, on_head: Callable[[float, tuple[float, float, float]], None],
                 name: str | None = None):
        self.camera = camera
        self.name = name
        self.on_head = on_head
        self._proc: subprocess.Popen | None = None
        self._stopped = False
        self._starts = 0
        self.sightings = 0
        self.error: str | None = None

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        self._stopped = False
        self._start()

    def _start(self) -> None:
        self._starts += 1
        try:
            self._proc = subprocess.Popen(
                command(self.camera, self.name), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0)
        except OSError as exc:
            self.error = str(exc)
            log.error("head tracking: couldn't start the camera process: %s", exc)
            return
        log.info("head tracking: camera %s in a process of its own (pid %s)", self.camera, self._proc.pid)
        threading.Thread(target=self._read, args=(self._proc,), name="curslink-head-out", daemon=True).start()
        threading.Thread(target=self._read_log, args=(self._proc,), name="curslink-head-err", daemon=True).start()

    def _read(self, proc: subprocess.Popen) -> None:
        try:
            for line in proc.stdout:
                head = parse(line)
                if head is None:
                    continue
                self.sightings += 1
                if self.sightings == 1:
                    log.info("head tracking: the camera sees a face (%.0f mm from it)", head[2])
                try:
                    self.on_head(time.monotonic(), head)
                except Exception:  # never the camera's problem
                    log.debug("head listener failed", exc_info=True)
        except (OSError, ValueError):
            pass
        code = proc.wait()
        if self._stopped or proc is not self._proc:
            return
        self.error = f"the camera process stopped (exit code {code})"
        if self._starts > RESTARTS:
            log.error("head tracking: %s again; leaving it stopped", self.error)
            return
        log.warning("head tracking: %s; starting it again in %.0f s", self.error, RESTART_AFTER_S)
        time.sleep(RESTART_AFTER_S)
        if not self._stopped and proc is self._proc:
            self._start()

    def _read_log(self, proc: subprocess.Popen) -> None:
        try:
            for line in proc.stderr:
                line = line.rstrip()
                if line:
                    log.info("head camera: %s", line)
        except (OSError, ValueError):
            pass

    def stop(self, timeout: float = 3.0) -> None:
        self._stopped = True
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.stdin.close()  # it stops by itself on that, letting go of the camera
            proc.wait(timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass
        log.info("head tracking: camera process stopped")


def main(argv: list[str] | None = None) -> int:
    """The child: watch camera ``argv[0]`` and print where the head is."""
    argv = sys.argv[1:] if argv is None else argv
    try:
        camera = int(argv[0]) if argv else 0
    except ValueError:
        camera = 0
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    from curslink.gaze import tracker as tracking
    from curslink.gaze.cameras import resolve

    if len(argv) > 1:
        camera = resolve(camera, argv[1])

    out = sys.stdout

    def say(_t: float, head: tuple[float, float, float]) -> None:
        try:
            out.write(f"h {head[0]:.1f} {head[1]:.1f} {head[2]:.1f}\n")
            out.flush()
        except (OSError, ValueError):
            tracker._stop.set()  # nobody's reading: the host went away

    tracking.head_listener = say
    tracker = tracking.HeadPoseTracker(lambda *_sample: None, camera=camera, fps=20.0)

    def until_stdin_closes() -> None:
        # Never a read left waiting on stdin: on Windows that held up the
        # camera libraries' DLLs until the host closed the pipe, so the
        # camera opened only as the host stopped (curslink.pipein).
        from curslink import pipein

        try:
            for _line in pipein.lines():
                pass
        except (OSError, ValueError):
            pass
        tracker._stop.set()

    threading.Thread(target=until_stdin_closes, name="curslink-head-stdin", daemon=True).start()
    tracker._run()
    return 1 if tracker.error else 0


if __name__ == "__main__":
    raise SystemExit(main())
