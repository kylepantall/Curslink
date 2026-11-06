"""The switch animation: a "pop" ring or a "blink" pulse where the pointer lands.

Tk wants to own the main thread (macOS insists on it) while the host and
agent run asyncio there, so the overlay is a small long-lived helper
process (``python -m curslink.overlay``) that reads commands on stdin:

    pop 1920 540
    blink 300 200
"""

from __future__ import annotations

import logging
import queue
import subprocess
import sys
import threading
import time

log = logging.getLogger(__name__)

SIZE = 180
ACCENT = "#3fa9ff"
DURATION_S = 0.38


def helper_command() -> list[str]:
    """How to start the helper: a packaged build has no ``python -m``, so it
    runs its own executable with the hidden ``_overlay-helper`` command."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "_overlay-helper"]
    return [sys.executable, "-m", "curslink.overlay"]


class OverlayProcess:
    """Host/agent side handle to the helper process."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._disabled = False
        self._lock = threading.Lock()

    def _ensure(self) -> subprocess.Popen | None:
        if self._disabled:
            return None
        if self._proc is None or self._proc.poll() is not None:
            try:
                self._proc = subprocess.Popen(
                    helper_command(),
                    stdin=subprocess.PIPE,
                    text=True,
                )
            except OSError:
                log.warning("switch animation unavailable", exc_info=True)
                self._disabled = True
                return None
        return self._proc

    def start(self) -> None:
        with self._lock:
            self._ensure()

    def show(self, effect: str, x: int, y: int) -> None:
        if effect not in ("pop", "blink"):
            return
        with self._lock:
            proc = self._ensure()
            if proc is None or proc.stdin is None:
                return
            try:
                proc.stdin.write(f"{effect} {int(x)} {int(y)}\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self._proc = None

    def close(self) -> None:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
            self._proc = None


# ---------------------------------------------------------------- helper side


def _ease_out(p: float) -> float:
    return 1 - (1 - p) ** 3


def _make_click_through(win) -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        GWL_EXSTYLE, WS_EX_LAYERED, WS_EX_TRANSPARENT = -20, 0x80000, 0x20
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(
            hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT
        )
    except Exception:  # pragma: no cover - cosmetic only
        pass


def _spawn(root, effect: str, x: int, y: int) -> None:
    import tkinter as tk

    key = "#010203"
    win = tk.Toplevel(root)
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    win.geometry(f"{SIZE}x{SIZE}+{x - SIZE // 2}+{y - SIZE // 2}")
    bg = key
    if sys.platform == "win32":
        win.attributes("-transparentcolor", key)
    elif sys.platform == "darwin":
        win.attributes("-transparent", True)
        bg = "systemTransparent"
    else:
        # No per-pixel transparency on plain X11: fade the whole tiny window.
        bg = "#000000"
    canvas = tk.Canvas(win, width=SIZE, height=SIZE, bg=bg, highlightthickness=0)
    canvas.pack()
    win.update_idletasks()
    _make_click_through(win)
    start = time.monotonic()
    c = SIZE / 2

    def frame() -> None:
        p = (time.monotonic() - start) / DURATION_S
        if p >= 1:
            win.destroy()
            return
        canvas.delete("all")
        if effect == "pop":
            r = 10 + _ease_out(p) * (SIZE / 2 - 14)
            width = max(1.0, 9 * (1 - p))
            canvas.create_oval(c - r, c - r, c + r, c + r, outline=ACCENT, width=width)
            dot = 7 * (1 - p)
            canvas.create_oval(c - dot, c - dot, c + dot, c + dot, fill=ACCENT, outline="")
            alpha = 1 - p * 0.6
        else:  # blink: two quick flashes
            on = int(p * 4) % 2 == 0
            r = 34
            if on:
                canvas.create_oval(c - r, c - r, c + r, c + r, outline=ACCENT, width=6)
                canvas.create_oval(c - 9, c - 9, c + 9, c + 9, fill=ACCENT, outline="")
            alpha = 1.0
        if sys.platform not in ("win32", "darwin"):
            win.attributes("-alpha", max(0.05, alpha * 0.9))
        win.after(12, frame)

    frame()


def run_helper() -> None:
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()
    commands: queue.Queue[str | None] = queue.Queue()

    def read_stdin() -> None:
        for line in sys.stdin:
            commands.put(line)
        commands.put(None)

    threading.Thread(target=read_stdin, daemon=True).start()

    def poll() -> None:
        while True:
            try:
                line = commands.get_nowait()
            except queue.Empty:
                break
            if line is None:
                root.destroy()
                return
            parts = line.split()
            if len(parts) == 3 and parts[0] in ("pop", "blink"):
                try:
                    _spawn(root, parts[0], int(parts[1]), int(parts[2]))
                except (ValueError, tk.TclError):
                    log.debug("bad overlay command %r", line, exc_info=True)
        root.after(10, poll)

    poll()
    root.mainloop()


if __name__ == "__main__":
    run_helper()
