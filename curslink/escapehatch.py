"""Windows' own ways out free the cursor: Ctrl+Alt+Delete and Task Manager.

Kyle (0.4.102): "we should force ctrl+alt+delete on windows to free the
cursor too ... or task manager ctrl+shift+esc ... both". Low-level hooks
never see Ctrl+Alt+Delete (Windows keeps it for itself), but it switches
the input to the secure desktop ("Winlogon"), as the lock screen and UAC
prompts do, and that can be seen from any thread or process. Task Manager
is seen as the app in front.

The host's hooks watchdog looks while input is on another computer and
gives it back (curslink.inputs.pynput_backend), and the app (a process of
its own, curslink.gui.service) puts the cursor back, so this works even
when the host is stuck.
"""

from __future__ import annotations

import ctypes
import logging
import sys

log = logging.getLogger(__name__)

SECURE = "Ctrl+Alt+Delete (or the lock screen)"
TASK_MANAGER = "Task Manager"

_DESKTOP_READOBJECTS = 0x0001
_UOI_NAME = 2


def _input_desktop() -> str | None:
    """The name of the desktop that has the input ("Default" normally), or
    None if this process can't open it: the secure desktop."""
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.OpenInputDesktop.restype = wintypes.HANDLE
    user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    user32.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
                                                 wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    handle = user32.OpenInputDesktop(0, False, _DESKTOP_READOBJECTS)
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(256)
        needed = wintypes.DWORD()
        if not user32.GetUserObjectInformationW(handle, _UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(needed)):
            return "?"
        return buf.value
    finally:
        user32.CloseDesktop(handle)


def _foreground_program() -> str:
    """The program name of the app in front, lower case ("" if unknown)."""
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    process = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not process:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if not kernel32.QueryFullProcessImageNameW(process, 0, buf, ctypes.byref(size)):
            return ""
        return buf.value.rsplit("\\", 1)[-1].lower()
    finally:
        kernel32.CloseHandle(process)


def escape_wanted(desktop=None, foreground=None) -> str | None:
    """Windows: which way out the user has taken, if any (SECURE or
    TASK_MANAGER), else None. Never raises."""
    if sys.platform != "win32" and desktop is None:
        return None
    try:
        name = (desktop or _input_desktop)()
        if name is None or name.lower() != "default":
            return SECURE
        if (foreground or _foreground_program)() == "taskmgr.exe":
            return TASK_MANAGER
    except Exception:
        log.debug("couldn't tell the input desktop or the app in front", exc_info=True)
    return None
