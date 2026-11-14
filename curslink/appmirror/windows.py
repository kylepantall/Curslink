"""App windows on this computer, for App mirror (see :mod:`curslink.appmirror`).

The window under the pointer, where a window is now, putting one back and
bringing one to the front: on Windows through user32 and DWM, on macOS
through Quartz's window list and Accessibility. Each window is a small dict
that goes across the link as it is::

    {"wid": <HWND or CGWindowID>, "pid": <owner>, "app": "Safari",
     "title": "Apple", "os": [x, y, w, h]}

``os`` is in this computer's OS pixels (physical on Windows, points on
macOS), the window's visible frame: on Windows without the invisible
resize borders, on macOS without the shadow. Elsewhere (Linux) there is no
window to find, and App mirror can't start from there.

Everything is best effort: any call may answer None.
"""

from __future__ import annotations

import logging
import os
import sys

log = logging.getLogger(__name__)

SKIP_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "NotifyIconOverflowWindow", "Progman", "WorkerW",
                "Windows.UI.Core.CoreWindow"}
SKIP_OWNERS = {"Window Server", "Dock", "SystemUIServer", "Control Center", "Notification Center", "Finder Desktop"}


def supported() -> bool:
    return sys.platform in ("win32", "darwin")


# ------------------------------------------------------------------ Windows
def _u32():  # pragma: no cover - Windows only
    import ctypes

    return ctypes.windll.user32


def _win_rect(hwnd: int) -> list[int] | None:  # pragma: no cover - Windows only
    """The visible frame (DWM's extended frame bounds: no invisible borders)."""
    import ctypes
    import ctypes.wintypes as wt

    u = _u32()
    h = wt.HWND(hwnd)
    if not u.IsWindow(h) or not u.IsWindowVisible(h):
        return None
    r = wt.RECT()
    try:
        dwm = ctypes.windll.dwmapi
        if dwm.DwmGetWindowAttribute(h, 9, ctypes.byref(r), ctypes.sizeof(r)) != 0:  # DWMWA_EXTENDED_FRAME_BOUNDS
            raise OSError
    except (OSError, AttributeError):
        if not u.GetWindowRect(h, ctypes.byref(r)):
            return None
    return [r.left, r.top, r.right - r.left, r.bottom - r.top]


def _win_process_name(pid: int) -> str:  # pragma: no cover - Windows only
    import ctypes
    import ctypes.wintypes as wt

    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = wt.DWORD(1024)
        if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            name = os.path.basename(buf.value)
            return name[:-4] if name.lower().endswith(".exe") else name
    finally:
        k.CloseHandle(h)
    return ""


def _win_at(x: int, y: int, skip: set[int]) -> dict | None:  # pragma: no cover - Windows only
    import ctypes
    import ctypes.wintypes as wt

    u = _u32()
    u.WindowFromPoint.restype = wt.HWND
    u.WindowFromPoint.argtypes = [wt.POINT]
    hwnd = u.WindowFromPoint(wt.POINT(int(x), int(y)))
    if not hwnd:
        return None
    u.GetAncestor.restype = wt.HWND
    root = u.GetAncestor(hwnd, 2) or hwnd  # GA_ROOT
    pid = wt.DWORD()
    u.GetWindowThreadProcessId(root, ctypes.byref(pid))
    if pid.value in skip:
        return None
    cls = ctypes.create_unicode_buffer(256)
    u.GetClassNameW(root, cls, 256)
    if cls.value in SKIP_CLASSES:
        return None
    rect = _win_rect(int(root))
    if rect is None:
        return None
    title = ctypes.create_unicode_buffer(256)
    u.GetWindowTextW(root, title, 256)
    return {"wid": int(root), "pid": int(pid.value), "app": _win_process_name(pid.value) or cls.value,
            "title": title.value, "os": rect}


def _win_move(win: dict, rect) -> bool:  # pragma: no cover - Windows only
    """Put the window's visible frame at ``rect`` (allowing for its borders),
    un-snapping it first if it was let go of at a screen edge."""
    import ctypes
    import ctypes.wintypes as wt

    u = _u32()
    h = wt.HWND(int(win["wid"]))
    if u.IsZoomed(h):
        u.ShowWindow(h, 9)  # SW_RESTORE
    outer, inner = wt.RECT(), _win_rect(int(win["wid"]))
    if inner is None or not u.GetWindowRect(h, ctypes.byref(outer)):
        return False
    left, top = inner[0] - outer.left, inner[1] - outer.top
    right, bottom = outer.right - (inner[0] + inner[2]), outer.bottom - (inner[1] + inner[3])
    x, y = int(rect[0]) - left, int(rect[1]) - top
    w, ht = int(rect[2]) + left + right, int(rect[3]) + top + bottom
    # SWP_NOZORDER | SWP_NOACTIVATE
    return bool(u.SetWindowPos(h, None, x, y, w, ht, 0x0004 | 0x0010))


def _win_raise(win: dict) -> None:  # pragma: no cover - Windows only
    from curslink.files import winfocus

    winfocus.bring_here(int(win["wid"]))


# -------------------------------------------------------------------- macOS
def _mac_info(wid: int):  # pragma: no cover - macOS only
    import Quartz

    infos = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionIncludingWindow, int(wid)) or []
    return infos[0] if len(infos) else None


def _mac_bounds(info) -> list[float]:  # pragma: no cover - macOS only
    b = info.get("kCGWindowBounds") or {}
    return [float(b.get(k, 0)) for k in ("X", "Y", "Width", "Height")]


def _mac_at(x: float, y: float, skip: set[int]) -> dict | None:  # pragma: no cover - macOS only
    import Quartz

    infos = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID) or []
    for info in infos:  # front to back
        if int(info.get("kCGWindowLayer", 1)) != 0 or float(info.get("kCGWindowAlpha", 1)) <= 0:
            continue
        pid = int(info.get("kCGWindowOwnerPID", 0))
        owner = str(info.get("kCGWindowOwnerName", ""))
        if pid in skip or owner in SKIP_OWNERS:
            continue
        bx, by, bw, bh = _mac_bounds(info)
        if bx <= x < bx + bw and by <= y < by + bh:
            return {"wid": int(info.get("kCGWindowNumber", 0)), "pid": pid, "app": owner,
                    "title": str(info.get("kCGWindowName", "") or ""), "os": [bx, by, bw, bh]}
    return None


def _ax():  # pragma: no cover - macOS only
    try:
        import HIServices as AX
    except ImportError:
        import ApplicationServices as AX
    return AX


def _mac_ax_window(win: dict):  # pragma: no cover - macOS only
    """The window's Accessibility element: its app's window with the same frame."""
    AX = _ax()
    info = _mac_info(int(win["wid"]))
    if info is None:
        return None
    bx, by, bw, bh = _mac_bounds(info)
    err, windows = AX.AXUIElementCopyAttributeValue(AX.AXUIElementCreateApplication(int(win["pid"])), "AXWindows", None)
    best = None
    for w in windows or []:
        _, pos = AX.AXUIElementCopyAttributeValue(w, "AXPosition", None)
        _, size = AX.AXUIElementCopyAttributeValue(w, "AXSize", None)
        ok_p, p = AX.AXValueGetValue(pos, AX.kAXValueCGPointType, None)
        ok_s, s = AX.AXValueGetValue(size, AX.kAXValueCGSizeType, None)
        if not (ok_p and ok_s):
            continue
        off = abs(p.x - bx) + abs(p.y - by) + abs(s.width - bw) + abs(s.height - bh)
        if best is None or off < best[0]:
            best = (off, w)
    return best[1] if best is not None and best[0] < 8 else None


def _mac_move(win: dict, rect) -> bool:  # pragma: no cover - macOS only
    import Quartz

    AX = _ax()
    w = _mac_ax_window(win)
    if w is None:
        return False
    value = AX.AXValueCreate(AX.kAXValueCGPointType, Quartz.CGPointMake(float(rect[0]), float(rect[1])))
    ok = AX.AXUIElementSetAttributeValue(w, "AXPosition", value) == 0
    size = AX.AXValueCreate(AX.kAXValueCGSizeType, Quartz.CGSizeMake(float(rect[2]), float(rect[3])))
    AX.AXUIElementSetAttributeValue(w, "AXSize", size)
    return ok


def _mac_raise(win: dict) -> None:  # pragma: no cover - macOS only
    from curslink.files import macfocus

    r = win.get("os") or [0, 0, 0, 0]
    info = _mac_info(int(win["wid"]))
    if info is not None:
        r = _mac_bounds(info)
    w = None
    try:
        w = _mac_ax_window(win)
        if w is not None:
            _ax().AXUIElementPerformAction(w, "AXRaise")
    except Exception:
        log.debug("app mirror: couldn't raise the window by Accessibility", exc_info=True)
    macfocus.raise_window(int(win["pid"]), r[0] + r[2] / 2, r[1] + min(12, r[3] / 2))


# ------------------------------------------------------------------- shared
def cursor() -> tuple[float, float] | None:
    """Where this computer's pointer is, in OS pixels."""
    try:
        if sys.platform == "darwin":
            from curslink.inputs.pynput_backend import mac_cursor

            return mac_cursor()
        if sys.platform == "win32":
            from curslink.inputs.pynput_backend import _win_cursor_pos

            return _win_cursor_pos()
    except Exception:
        log.debug("app mirror: couldn't read the pointer", exc_info=True)
    return None


def window_at_cursor(skip: set[int] | None = None) -> dict | None:
    """The ordinary app window under the pointer (not the desktop, the
    taskbar or the Dock, nor one of Curslink's), with ``at``: where the
    pointer is on it, as fractions of it."""
    skip = {os.getpid(), *(skip or ())}
    at = cursor()
    if at is None:
        return None
    try:
        if sys.platform == "win32":
            win = _win_at(int(at[0]), int(at[1]), skip)
        elif sys.platform == "darwin":
            win = _mac_at(at[0], at[1], skip)
        else:
            return None
    except Exception:
        log.debug("app mirror: couldn't find the window under the pointer", exc_info=True)
        return None
    if win is None or win["os"][2] < 40 or win["os"][3] < 30:
        return None
    x, y, w, h = win["os"]
    win["at"] = [round((at[0] - x) / w, 4), round((at[1] - y) / h, 4)]
    return win


def window_rect(win: dict) -> list[float] | None:
    """Where the window is now, or None if it's gone (closed, or minimised)."""
    try:
        if sys.platform == "win32":
            return _win_rect(int(win["wid"]))
        if sys.platform == "darwin":
            info = _mac_info(int(win["wid"]))
            if info is None or not info.get("kCGWindowIsOnscreen", True):
                return None
            return _mac_bounds(info)
    except Exception:
        log.debug("app mirror: couldn't find a window", exc_info=True)
    return None


def move_window(win: dict, rect) -> bool:
    """Put the window back at ``rect`` (where it was, the size it was)."""
    try:
        if sys.platform == "win32":
            return _win_move(win, rect)
        if sys.platform == "darwin":
            return _mac_move(win, rect)
    except Exception:
        log.debug("app mirror: couldn't move a window", exc_info=True)
    return False


def raise_window(win: dict) -> None:
    """Bring the window to the front, so input sent to it lands in it."""
    try:
        if sys.platform == "win32":
            _win_raise(win)
        elif sys.platform == "darwin":
            _mac_raise(win)
    except Exception:
        log.debug("app mirror: couldn't bring a window forward", exc_info=True)
