"""The switch animation: a "pop" ring or a "blink" pulse where the pointer lands.

The GUI toolkit wants to own the main thread (macOS insists on it) while
the host and agent run asyncio there, so the overlay is a small long-lived
helper process (``python -m curslink.overlay``) that reads commands on stdin:

    pop 1920 540
    blink 300 200

The coordinates are the OS's own, as the pointer is warped to them.

The helper draws with Qt where it's installed (every packaged build) and
falls back to Tk. The Qt one never takes focus or clicks: it's a tool
window that doesn't accept focus, is shown without activating, and lets
input through, and on macOS the helper is an accessory app (no Dock icon,
never the active app). Tk windows activate their app when they appear,
which took focus away from whatever you were typing in.
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
    try:
        import PySide6.QtWidgets  # noqa: F401
    except ImportError:
        _run_tk_helper()
        return
    _run_qt_helper()


# ------------------------------------------------------------ the Qt helper


def native_to_logical(screens, x: float, y: float, scale_positions: bool) -> tuple[float, float]:
    """An OS pixel position -> Qt's device independent one.

    ``screens`` is ``[(gx, gy, gw, gh, dpr), ...]`` from Qt. On Windows the
    OS works in physical pixels while Qt keeps each screen's top-left where
    the OS has it and scales the rest by the screen's ratio; on macOS both
    work in points already (``scale_positions`` False)."""
    if not scale_positions:
        return float(x), float(y)
    for gx, gy, gw, gh, dpr in screens:
        if gx <= x < gx + gw * dpr and gy <= y < gy + gh * dpr:
            return gx + (x - gx) / dpr, gy + (y - gy) / dpr
    return float(x), float(y)


def _mac_accessory() -> None:
    """Never become the active app (or show in the Dock) for an animation."""
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication

        NSApplication.sharedApplication().setActivationPolicy_(1)  # Accessory
    except Exception:  # pragma: no cover - macOS only
        log.debug("couldn't make the overlay helper an accessory app", exc_info=True)


def make_effect_widget(effect: str, x: int, y: int):
    """One pop or blink at OS position (x, y), already showing; it closes
    itself when done."""
    from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
    from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPen
    from PySide6.QtWidgets import QWidget

    screens = []
    for sc in QGuiApplication.screens():
        g = sc.geometry()
        screens.append((g.x(), g.y(), g.width(), g.height(), sc.devicePixelRatio() or 1.0))
    lx, ly = native_to_logical(screens, x, y, sys.platform == "win32")

    class Effect(QWidget):
        def __init__(self):
            flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
                     | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowTransparentForInput
                     | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.NoDropShadowWindowHint)
            if sys.platform.startswith("linux"):
                flags |= Qt.WindowType.X11BypassWindowManagerHint
            super().__init__(None, flags)
            for attr in (Qt.WidgetAttribute.WA_TranslucentBackground, Qt.WidgetAttribute.WA_ShowWithoutActivating,
                         Qt.WidgetAttribute.WA_TransparentForMouseEvents, Qt.WidgetAttribute.WA_DeleteOnClose):
                self.setAttribute(attr, True)
            self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.setGeometry(round(lx - SIZE / 2), round(ly - SIZE / 2), SIZE, SIZE)
            self.effect = effect
            self.start = time.monotonic()
            self.timer = QTimer(self)
            self.timer.timeout.connect(self.tick)
            self.timer.start(12)

        def progress(self) -> float:
            return (time.monotonic() - self.start) / DURATION_S

        def tick(self) -> None:
            if self.progress() >= 1:
                self.timer.stop()
                self.close()
            else:
                self.update()

        def paintEvent(self, _e) -> None:  # noqa: N802
            p = min(1.0, self.progress())
            accent = QColor(ACCENT)
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            c = QPointF(SIZE / 2, SIZE / 2)
            if self.effect == "pop":
                accent.setAlphaF(max(0.0, 1 - p * 0.6))
                r = 10 + _ease_out(p) * (SIZE / 2 - 14)
                painter.setPen(QPen(accent, max(1.0, 9 * (1 - p))))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(c, r, r)
                dot = 7 * (1 - p)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(accent)
                painter.drawEllipse(QRectF(c.x() - dot, c.y() - dot, 2 * dot, 2 * dot))
            elif int(p * 4) % 2 == 0:  # blink: two quick flashes
                painter.setPen(QPen(accent, 6))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(c, 34, 34)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(accent)
                painter.drawEllipse(c, 9, 9)
            painter.end()

    w = Effect()
    w.show()
    return w


def _run_qt_helper() -> None:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(["curslink-overlay"])
    app.setQuitOnLastWindowClosed(False)
    _mac_accessory()
    commands: queue.Queue[str | None] = queue.Queue()

    def read_stdin() -> None:
        for line in sys.stdin:
            commands.put(line)
        commands.put(None)

    threading.Thread(target=read_stdin, daemon=True).start()
    alive: list = []

    def poll() -> None:
        while True:
            try:
                line = commands.get_nowait()
            except queue.Empty:
                return
            if line is None:
                app.quit()
                return
            parts = line.split()
            if len(parts) == 3 and parts[0] in ("pop", "blink"):
                try:
                    w = make_effect_widget(parts[0], int(parts[1]), int(parts[2]))
                except ValueError:
                    log.debug("bad overlay command %r", line)
                    continue
                alive.append(w)
                w.destroyed.connect(lambda *_a, w=w: alive.remove(w) if w in alive else None)

    timer = QTimer()
    timer.timeout.connect(poll)
    timer.start(10)
    app.exec()


# ------------------------------------------------------------ the Tk helper


def _run_tk_helper() -> None:
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
