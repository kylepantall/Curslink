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


def parks() -> bool:
    """Hiding a window moves it out of sight (macOS), rather than making it
    see-through where it is (Windows)."""
    return sys.platform == "darwin"


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
    scale = 1.0
    try:
        dpi = u.GetDpiForWindow(root)
        if dpi:
            scale = round(dpi / 96, 3)
    except (AttributeError, OSError):
        pass
    return {"wid": int(root), "pid": int(pid.value), "app": _win_process_name(pid.value) or cls.value,
            "title": title.value, "os": rect, "scale": scale}


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


_GWL_EXSTYLE = -20
_WS_EX_LAYERED, _WS_EX_TRANSPARENT = 0x00080000, 0x00000020
_win_styles: dict[int, int] = {}  # hidden windows: their own extended style, to put back


def _win_exstyle(hwnd: int, value: int | None = None) -> int:  # pragma: no cover - Windows only
    import ctypes
    import ctypes.wintypes as wt

    u = _u32()
    u.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    u.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
    u.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    u.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_ssize_t]
    if value is None:
        return int(u.GetWindowLongPtrW(wt.HWND(hwnd), _GWL_EXSTYLE))
    return int(u.SetWindowLongPtrW(wt.HWND(hwnd), _GWL_EXSTYLE, value))


def _win_hide(win: dict, in_use: bool) -> bool:  # pragma: no cover - Windows only
    """Invisible where it is (all but see-through: it still draws, so it can
    still be captured, and still takes the input sent to it while it's in
    use); clicks pass through it while it isn't."""
    import ctypes.wintypes as wt

    hwnd = int(win["wid"])
    u = _u32()
    if hwnd not in _win_styles:
        style = _win_exstyle(hwnd)
        if style & _WS_EX_LAYERED:
            return False  # it sets its own see-through (and would lose it)
        _win_styles[hwnd] = style
    style = _win_styles[hwnd] | _WS_EX_LAYERED | (0 if in_use else _WS_EX_TRANSPARENT)
    _win_exstyle(hwnd, style)
    u.SetLayeredWindowAttributes.argtypes = [wt.HWND, wt.DWORD, wt.BYTE, wt.DWORD]
    return bool(u.SetLayeredWindowAttributes(wt.HWND(hwnd), 0, 1, 0x2))  # LWA_ALPHA, 1 of 255


def _win_show(win: dict) -> None:  # pragma: no cover - Windows only
    import ctypes
    import ctypes.wintypes as wt

    hwnd = int(win["wid"])
    style = _win_styles.pop(hwnd, None)
    if style is None and _u32().IsWindow(hwnd):
        # Hidden by another of our processes: hidden as we hide (alpha 1)?
        alpha, flags = wt.BYTE(), wt.DWORD()
        if (_u32().GetLayeredWindowAttributes(wt.HWND(hwnd), None, ctypes.byref(alpha), ctypes.byref(flags))
                and flags.value & 0x2 and alpha.value == 1):
            style = _win_exstyle(hwnd) & ~(_WS_EX_LAYERED | _WS_EX_TRANSPARENT)
    if style is not None and _u32().IsWindow(hwnd):
        _win_exstyle(hwnd, style)
        _u32().RedrawWindow(hwnd, None, None, 0x0001 | 0x0004 | 0x0080 | 0x0400)  # invalidate, erase, children, frame


def _win_is_popup(hwnd: int, main: int) -> bool:  # pragma: no cover - Windows only
    """A menu, a dropdown, a tooltip or a dialog of ``main``'s (not another
    window of the same app)."""
    import ctypes
    import ctypes.wintypes as wt

    u = _u32()
    u.GetWindow.restype = wt.HWND
    if int(u.GetWindow(wt.HWND(hwnd), 4) or 0) == main:  # GW_OWNER
        return True
    cls = ctypes.create_unicode_buffer(64)
    u.GetClassNameW(wt.HWND(hwnd), cls, 64)
    if cls.value == "#32768":  # a menu
        return True
    u.GetWindowLongW.restype = ctypes.c_long
    style = u.GetWindowLongW(wt.HWND(hwnd), -16) & 0xFFFFFFFF  # GWL_STYLE
    return bool(style & 0x80000000) and (style & 0x00C00000) != 0x00C00000  # WS_POPUP, no WS_CAPTION


def _win_popups(win: dict) -> list[tuple[int, list[int]]]:  # pragma: no cover - Windows only
    """The app's other windows showing now (its menus, dropdowns, tooltips),
    front last."""
    import ctypes
    import ctypes.wintypes as wt

    u = _u32()
    main, pid = int(win["wid"]), int(win.get("pid") or 0)
    found: list[tuple[int, list[int]]] = []
    proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def each(hwnd, _l):
        h = int(hwnd or 0)
        if h and h != main and u.IsWindowVisible(hwnd):
            owner = wt.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid and _win_is_popup(h, main):
                rect = _win_rect(h)
                if rect is not None and rect[2] > 0 and rect[3] > 0:
                    found.append((h, rect))
        return True

    u.EnumWindows(proc(each), 0)
    found.reverse()  # EnumWindows goes front to back
    return found


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


def _mac_popups(win: dict) -> list[tuple[int, list[float]]]:  # pragma: no cover - macOS only
    """The app's menus, popovers and tooltips showing now, front last."""
    import Quartz

    pid, main = int(win.get("pid") or 0), int(win["wid"])
    infos = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []
    found = []
    for info in infos:  # front to back
        if int(info.get("kCGWindowOwnerPID", 0)) != pid or int(info.get("kCGWindowNumber", 0)) == main:
            continue
        if int(info.get("kCGWindowLayer", 0)) <= 0 or float(info.get("kCGWindowAlpha", 1)) <= 0:
            continue  # its other windows are its own; menus and popovers float above
        found.append((int(info.get("kCGWindowNumber", 0)), _mac_bounds(info)))
    found.reverse()
    return found


def _mac_park_at() -> tuple[float, float] | None:  # pragma: no cover - macOS only
    """The bottom-right corner of the desk (macOS won't put a window
    wholly off every screen: there, all but a pixel of it is)."""
    from curslink.displays import os_screens

    screens = os_screens()
    if not screens:
        return None
    s = max(screens, key=lambda d: (d["x"] + d["width"]) + (d["y"] + d["height"]))
    return (s["x"] + s["width"] - 1, s["y"] + s["height"] - 1)


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


def hide_window(win: dict, home, in_use: bool) -> bool:
    """While its app is cast elsewhere, hide the window on this computer's
    screens: on Windows it stays where it is, all but see-through (clicks
    pass through it unless ``in_use``); on macOS, which can't do that to
    another app's window, it's put in the far corner, and back at ``home``
    while it's ``in_use`` (input has to land on it). False if it can't be."""
    try:
        if sys.platform == "win32":
            return _win_hide(win, in_use)
        if sys.platform == "darwin":
            if in_use:
                return _mac_move(win, home)
            at = _mac_park_at()
            if at is None:
                return False
            return _mac_move(win, [at[0], at[1], home[2], home[3]])
    except Exception:
        log.debug("app mirror: couldn't hide a window", exc_info=True)
    return False


def show_window(win: dict, home) -> None:
    """Undo :func:`hide_window`: the window, as it was, at ``home``."""
    try:
        if sys.platform == "win32":
            _win_show(win)
        elif sys.platform == "darwin" and home is not None:
            _mac_move(win, home)
    except Exception:
        log.debug("app mirror: couldn't show a window again", exc_info=True)


def window_alive(win: dict) -> bool:
    """The window is still there (wherever it is)."""
    try:
        if sys.platform == "win32":
            return bool(_u32().IsWindow(int(win["wid"])))
        if sys.platform == "darwin":
            return _mac_info(int(win["wid"])) is not None
    except Exception:
        log.debug("app mirror: couldn't look for a window", exc_info=True)
    return False


def popups(win: dict) -> list[tuple[int, list[float]]]:
    """The app's other windows showing now (menus, dropdowns, tooltips),
    as (id, os rect), front last."""
    try:
        if sys.platform == "win32":
            return _win_popups(win)
        if sys.platform == "darwin":
            return _mac_popups(win)
    except Exception:
        log.debug("app mirror: couldn't list an app's menus", exc_info=True)
    return []


def raise_window(win: dict) -> None:
    """Bring the window to the front, so input sent to it lands in it."""
    try:
        if sys.platform == "win32":
            _win_raise(win)
        elif sys.platform == "darwin":
            _mac_raise(win)
    except Exception:
        log.debug("app mirror: couldn't bring a window forward", exc_info=True)
