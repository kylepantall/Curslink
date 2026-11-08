"""Windows: the top-level window under the pointer, and bringing it forward
(see :mod:`curslink.files.focus`)."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt

GA_ROOT = 2
SKIP_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "NotifyIconOverflowWindow"}
DESKTOP_CLASSES = {"Progman", "WorkerW"}


def _user32():
    return ctypes.windll.user32


def probe(skip: set[int]):
    """(hwnd, name, x, y) of the top-level window under the pointer, or None
    (the taskbar, or one of ours). Over the desktop: the desktop's window,
    so Explorer takes the drop."""
    u = _user32()
    pt = wt.POINT()
    if not u.GetCursorPos(ctypes.byref(pt)):
        return None
    u.WindowFromPoint.restype = wt.HWND
    u.WindowFromPoint.argtypes = [wt.POINT]
    hwnd = u.WindowFromPoint(pt)
    if not hwnd:
        return None
    u.GetAncestor.restype = wt.HWND
    root = u.GetAncestor(hwnd, GA_ROOT) or hwnd
    pid = wt.DWORD()
    u.GetWindowThreadProcessId(root, ctypes.byref(pid))
    if pid.value in skip:
        return None
    cls = ctypes.create_unicode_buffer(256)
    u.GetClassNameW(root, cls, 256)
    if cls.value in SKIP_CLASSES:
        return None
    if cls.value in DESKTOP_CLASSES:
        return int(root), "the desktop", pt.x, pt.y
    title = ctypes.create_unicode_buffer(256)
    u.GetWindowTextW(root, title, 256)
    return int(root), title.value or cls.value, pt.x, pt.y


def bring(hwnd: int, x: int, y: int) -> None:
    u = _user32()
    # This process just sent the input Windows is acting on (the injected
    # drag), which is what lets it move another window to the front.
    u.SetForegroundWindow(wt.HWND(hwnd))
    u.BringWindowToTop(wt.HWND(hwnd))
