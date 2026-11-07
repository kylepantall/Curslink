"""The Windows calls the helper needs (ctypes; imported on Windows only)."""

from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes

log = logging.getLogger(__name__)

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
u32 = ctypes.WinDLL("user32", use_last_error=True)
adv = ctypes.WinDLL("advapi32", use_last_error=True)
wts = ctypes.WinDLL("wtsapi32", use_last_error=True)

HANDLE = wintypes.HANDLE
DWORD = wintypes.DWORD
BOOL = wintypes.BOOL
LPVOID = wintypes.LPVOID
INVALID_HANDLE = HANDLE(-1).value
NO_SESSION = 0xFFFFFFFF
GENERIC_ALL = 0x10000000
MAXIMUM_ALLOWED = 0x02000000


def _err(what: str) -> OSError:
    code = ctypes.get_last_error()
    return OSError(code, f"{what} failed: {ctypes.FormatError(code).strip()}")


# -- sessions ---------------------------------------------------------------
k32.WTSGetActiveConsoleSessionId.restype = DWORD
k32.GetCurrentProcess.restype = HANDLE
k32.GetCurrentProcessId.restype = DWORD


def this_session() -> int:
    session = DWORD()
    if not k32.ProcessIdToSessionId(k32.GetCurrentProcessId(), ctypes.byref(session)):
        raise _err("ProcessIdToSessionId")
    return session.value


def session_of(pid: int) -> int | None:
    session = DWORD()
    return session.value if k32.ProcessIdToSessionId(DWORD(pid), ctypes.byref(session)) else None


def console_session() -> int | None:
    """The session at the physical screen, keyboard and mouse (None between sign-ins)."""
    session = k32.WTSGetActiveConsoleSessionId()
    return None if session == NO_SESSION else session


# -- the session user's SID (for the pipe's access list) -----------------------
class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", LPVOID), ("Attributes", DWORD)]


adv.ConvertSidToStringSidW.argtypes = [LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
k32.LocalFree.argtypes = [LPVOID]
k32.CloseHandle.argtypes = [HANDLE]


def session_user_sid(session: int) -> str | None:
    """The string SID of who's signed in to ``session`` (needs SYSTEM)."""
    token = HANDLE()
    if not wts.WTSQueryUserToken(DWORD(session), ctypes.byref(token)):
        return None
    try:
        needed = DWORD()
        adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))  # TokenUser
        buf = ctypes.create_string_buffer(needed.value)
        if not adv.GetTokenInformation(token, 1, buf, needed, ctypes.byref(needed)):
            return None
        user = _SID_AND_ATTRIBUTES.from_buffer(buf)
        text = wintypes.LPWSTR()
        if not adv.ConvertSidToStringSidW(user.Sid, ctypes.byref(text)):
            return None
        try:
            return text.value
        finally:
            k32.LocalFree(text)
    finally:
        k32.CloseHandle(token)


# -- the pipe ---------------------------------------------------------------
class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", DWORD), ("lpSecurityDescriptor", LPVOID), ("bInheritHandle", BOOL)]


PIPE_ACCESS_DUPLEX = 0x3
PIPE_REJECT_REMOTE_CLIENTS = 0x8
ERROR_PIPE_CONNECTED = 535
k32.CreateNamedPipeW.restype = HANDLE
k32.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD,
                                 ctypes.POINTER(SECURITY_ATTRIBUTES)]
k32.ConnectNamedPipe.argtypes = [HANDLE, LPVOID]
k32.GetNamedPipeClientProcessId.argtypes = [HANDLE, ctypes.POINTER(wintypes.ULONG)]
adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
    wintypes.LPCWSTR, DWORD, ctypes.POINTER(LPVOID), LPVOID]


def pipe_sddl(user_sid: str) -> str:
    """SYSTEM and the session's user only; nobody else, local or remote."""
    return f"D:P(A;;GA;;;SY)(A;;GRGW;;;{user_sid})"


def create_pipe(name: str, sddl: str) -> int:
    sd = LPVOID()
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(sd), None):
        raise _err("ConvertStringSecurityDescriptorToSecurityDescriptor")
    try:
        sa = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), sd, False)
        handle = k32.CreateNamedPipeW(name, PIPE_ACCESS_DUPLEX, PIPE_REJECT_REMOTE_CLIENTS,  # byte mode, blocking
                                      1, 4096, 4096, 0, ctypes.byref(sa))
        if handle in (None, INVALID_HANDLE):
            raise _err("CreateNamedPipe")
        return handle
    finally:
        k32.LocalFree(sd)


def wait_for_client(handle: int) -> int:
    """Block until someone opens the pipe; their process id."""
    if not k32.ConnectNamedPipe(handle, None) and ctypes.get_last_error() != ERROR_PIPE_CONNECTED:
        raise _err("ConnectNamedPipe")
    pid = wintypes.ULONG()
    if not k32.GetNamedPipeClientProcessId(handle, ctypes.byref(pid)):
        raise _err("GetNamedPipeClientProcessId")
    return pid.value


# -- following the input desktop ------------------------------------------------
u32.OpenInputDesktop.restype = HANDLE
u32.OpenInputDesktop.argtypes = [DWORD, BOOL, DWORD]
u32.SetThreadDesktop.argtypes = [HANDLE]
u32.CloseDesktop.argtypes = [HANDLE]
u32.GetUserObjectInformationW.argtypes = [HANDLE, ctypes.c_int, LPVOID, DWORD, ctypes.POINTER(DWORD)]


def desktop_name(handle: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    needed = DWORD()
    if not u32.GetUserObjectInformationW(handle, 2, buf, ctypes.sizeof(buf), ctypes.byref(needed)):  # UOI_NAME
        return "?"
    return buf.value


class DesktopFollower:
    """Keeps the calling thread on the desktop that has the input: "Default"
    normally, "Winlogon" while a UAC prompt or the lock screen is up.
    SendInput only reaches the desktop its thread is on."""

    CHECK_EVERY_S = 0.1

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.name: str | None = None
        self._handle = None   # one we opened (the thread's first desktop isn't ours to close)
        self._checked: float | None = None

    def follow(self) -> None:
        now = self.clock()
        if self._checked is not None and now - self._checked < self.CHECK_EVERY_S:
            return
        self._checked = now
        handle = u32.OpenInputDesktop(0, False, GENERIC_ALL)
        if not handle:
            return  # e.g. mid-switch: try again on the next check
        name = desktop_name(handle)
        if name == self.name:
            u32.CloseDesktop(handle)
            return
        if not u32.SetThreadDesktop(handle):
            log.warning("couldn't move to the %s desktop: %s", name, _err("SetThreadDesktop"))
            u32.CloseDesktop(handle)
            return
        if self._handle:
            u32.CloseDesktop(self._handle)
        self._handle, self.name = handle, name
        log.info("injecting on the %s desktop", name)


# -- starting a process as SYSTEM in a session ---------------------------------
class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR), ("dwX", DWORD), ("dwY", DWORD), ("dwXSize", DWORD),
        ("dwYSize", DWORD), ("dwXCountChars", DWORD), ("dwYCountChars", DWORD),
        ("dwFillAttribute", DWORD), ("dwFlags", DWORD), ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD), ("lpReserved2", LPVOID), ("hStdInput", HANDLE),
        ("hStdOutput", HANDLE), ("hStdError", HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", HANDLE), ("hThread", HANDLE), ("dwProcessId", DWORD), ("dwThreadId", DWORD)]


TOKEN_ALL_ACCESS = 0xF01FF
CREATE_NO_WINDOW = 0x08000000
adv.OpenProcessToken.argtypes = [HANDLE, DWORD, ctypes.POINTER(HANDLE)]
adv.DuplicateTokenEx.argtypes = [HANDLE, DWORD, LPVOID, ctypes.c_int, ctypes.c_int, ctypes.POINTER(HANDLE)]
adv.SetTokenInformation.argtypes = [HANDLE, ctypes.c_int, LPVOID, DWORD]
adv.CreateProcessAsUserW.argtypes = [HANDLE, wintypes.LPCWSTR, wintypes.LPWSTR, LPVOID, LPVOID, BOOL, DWORD,
                                     LPVOID, wintypes.LPCWSTR, ctypes.POINTER(STARTUPINFOW),
                                     ctypes.POINTER(PROCESS_INFORMATION)]
k32.WaitForSingleObject.argtypes = [HANDLE, DWORD]
k32.WaitForSingleObject.restype = DWORD
k32.TerminateProcess.argtypes = [HANDLE, wintypes.UINT]


def start_in_session(session: int, command_line: str) -> tuple[int, int]:
    """Start a copy of this (SYSTEM) process's token in ``session``, on its
    normal desktop. Returns (process handle, pid)."""
    token, dup = HANDLE(), HANDLE()
    if not adv.OpenProcessToken(k32.GetCurrentProcess(), TOKEN_ALL_ACCESS, ctypes.byref(token)):
        raise _err("OpenProcessToken")
    try:
        if not adv.DuplicateTokenEx(token, MAXIMUM_ALLOWED, None, 2, 1, ctypes.byref(dup)):  # impersonation, primary
            raise _err("DuplicateTokenEx")
        target = DWORD(session)
        if not adv.SetTokenInformation(dup, 12, ctypes.byref(target), ctypes.sizeof(target)):  # TokenSessionId
            raise _err("SetTokenInformation")
        si = STARTUPINFOW()
        si.cb = ctypes.sizeof(STARTUPINFOW)
        si.lpDesktop = "winsta0\\default"
        pi = PROCESS_INFORMATION()
        cmd = ctypes.create_unicode_buffer(command_line)
        if not adv.CreateProcessAsUserW(dup, None, cmd, None, None, False, CREATE_NO_WINDOW, None, None,
                                        ctypes.byref(si), ctypes.byref(pi)):
            raise _err("CreateProcessAsUser")
        k32.CloseHandle(pi.hThread)
        return pi.hProcess, pi.dwProcessId
    finally:
        for h in (token, dup):
            if h:
                k32.CloseHandle(h)


def has_exited(process: int) -> bool:
    return k32.WaitForSingleObject(process, 0) == 0  # WAIT_OBJECT_0


def stop_process(process: int) -> None:
    k32.TerminateProcess(process, 0)
    k32.WaitForSingleObject(process, 3000)
    k32.CloseHandle(process)
