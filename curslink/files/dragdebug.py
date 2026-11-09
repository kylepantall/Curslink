"""Drag and drop debug lines for the diagnostics zip.

At each step of a file drag between computers (it reaches an edge, the real
drag starts, a window is brought forward, the drop, the end) one
``drag-debug`` line records what the computer looked like: where the cursor
is, which app and window are in front, which window is under the cursor,
and the windows open on screen, front to back. Drags are rare, so this is
always on; anything it can't find out is left out, never an error.
"""

from __future__ import annotations

import logging
import sys
import time

log = logging.getLogger("curslink.dragdebug")

MAX_WINDOWS = 12   # the front-most windows listed in each line
MIN_GAP_S = 0.05   # repeated steps closer than this are only counted


_last: dict[str, float] = {}


def trace(step: str, **extra) -> None:
    """Log one ``drag-debug <step>`` line with a snapshot of the screen."""
    now = time.monotonic()
    if now - _last.get(step, -1.0) < MIN_GAP_S:
        return
    _last[step] = now
    try:
        snap = snapshot()
    except Exception as exc:  # never break a drag for a log line
        snap = {"error": f"{type(exc).__name__}: {exc}"}
    parts = [f"{k}={v}" for k, v in extra.items()]
    log.info("drag-debug %s%s | %s", step, (" " + " ".join(parts)) if parts else "", format_snapshot(snap))


def format_snapshot(snap: dict) -> str:
    out = []
    if "cursor" in snap:
        x, y = snap["cursor"]
        out.append(f"cursor=({x:.0f},{y:.0f})")
    for key in ("front", "front_ax", "front_ws", "under"):
        if snap.get(key):
            out.append(f"{key}={_win(snap[key])}")
    if snap.get("windows") is not None:
        out.append("windows=[" + "; ".join(_win(w) for w in snap["windows"]) + "]")
    if snap.get("error"):
        out.append("error=" + snap["error"])
    return " ".join(out)


def _win(w: dict) -> str:
    s = f"{w.get('app') or '?'}"
    if w.get("pid") is not None:
        s += f"#{w['pid']}"
    if w.get("title"):
        s += f" '{w['title'][:40]}'"
    if w.get("cls"):
        s += f" <{w['cls']}>"
    if w.get("rect"):
        x, y, wd, ht = w["rect"]
        s += f" {x:.0f},{y:.0f} {wd:.0f}x{ht:.0f}"
    return s


def snapshot() -> dict:
    if sys.platform == "darwin":
        return _mac()
    if sys.platform == "win32":
        return _windows()
    return {}


# ---------------------------------------------------------------- macOS


def _mac() -> dict:
    snap: dict = {}
    import Quartz

    loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    x, y = loc.x, loc.y
    snap["cursor"] = (x, y)
    from curslink.files import macfocus

    ax = macfocus.ax_front_pid()
    if ax is not None:
        snap["front_ax"] = {"pid": ax, "app": _mac_name(ax)}
    try:
        from AppKit import NSWorkspace

        front = NSWorkspace.sharedWorkspace().frontmostApplication()
        if front is not None:
            snap["front_ws"] = {"pid": int(front.processIdentifier()), "app": str(front.localizedName())}
    except Exception:
        pass
    infos = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID) or []
    windows = []
    for info in infos:  # front to back
        if int(info.get("kCGWindowLayer", 1)) != 0 or float(info.get("kCGWindowAlpha", 1)) <= 0:
            continue
        b = info.get("kCGWindowBounds") or {}
        rect = tuple(float(b.get(k, 0)) for k in ("X", "Y", "Width", "Height"))
        w = {"app": str(info.get("kCGWindowOwnerName", "")), "pid": int(info.get("kCGWindowOwnerPID", 0)),
             "title": str(info.get("kCGWindowName") or ""), "rect": rect}
        if "under" not in snap and rect[0] <= x < rect[0] + rect[2] and rect[1] <= y < rect[1] + rect[3]:
            snap["under"] = w
        windows.append(w)
    snap["windows"] = windows[:MAX_WINDOWS]
    return snap


def _mac_name(pid: int) -> str:
    try:
        from AppKit import NSRunningApplication

        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        return str(app.localizedName()) if app is not None else ""
    except Exception:
        return ""


# -------------------------------------------------------------- Windows


def _windows() -> dict:
    import ctypes
    import ctypes.wintypes as wt

    u = ctypes.windll.user32
    snap: dict = {}
    pt = wt.POINT()
    if u.GetCursorPos(ctypes.byref(pt)):
        snap["cursor"] = (pt.x, pt.y)
    u.GetForegroundWindow.restype = wt.HWND
    u.GetAncestor.restype = wt.HWND
    u.WindowFromPoint.restype = wt.HWND
    u.WindowFromPoint.argtypes = [wt.POINT]
    fg = u.GetForegroundWindow()
    if fg:
        snap["front"] = _win32_info(u, fg)
    if "cursor" in snap:
        h = u.WindowFromPoint(pt)
        if h:
            snap["under"] = _win32_info(u, u.GetAncestor(h, 2) or h)
    windows: list = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def each(hwnd, _):
        if len(windows) >= MAX_WINDOWS:
            return False
        if u.IsWindowVisible(hwnd) and not u.IsIconic(hwnd) and u.GetWindowTextLengthW(hwnd) > 0:
            windows.append(_win32_info(u, hwnd))
        return True

    u.EnumWindows(WNDENUMPROC(each), 0)  # top to bottom
    snap["windows"] = windows
    return snap


def _win32_info(u, hwnd) -> dict:
    import ctypes
    import ctypes.wintypes as wt

    title = ctypes.create_unicode_buffer(256)
    u.GetWindowTextW(hwnd, title, 256)
    cls = ctypes.create_unicode_buffer(128)
    u.GetClassNameW(hwnd, cls, 128)
    pid = wt.DWORD()
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    r = wt.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    return {"app": _win32_exe(pid.value), "pid": pid.value, "title": title.value, "cls": cls.value,
            "rect": (r.left, r.top, r.right - r.left, r.bottom - r.top)}


def _win32_exe(pid: int) -> str:
    import ctypes
    import ctypes.wintypes as wt

    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = wt.DWORD(512)
        if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
        return ""
    finally:
        k.CloseHandle(h)
