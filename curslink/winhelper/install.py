"""Switching the helper on and off (Settings, or ``curslink winhelper``).

``install`` and ``uninstall`` need admin: the app runs them through
:func:`run_elevated`, which is the one UAC prompt.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from curslink.winhelper import DISPLAY_NAME, EXE_NAME, INSTALL_FOLDER, SERVICE_NAME

log = logging.getLogger(__name__)

NO_WINDOW = 0x08000000
ERROR_CANCELLED = 1223


def install_dir() -> Path:
    return Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / INSTALL_FOLDER


def bundled_exe() -> Path:
    """The helper that came with this copy of Curslink."""
    return Path(sys.executable).parent / EXE_NAME


def _sc(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    result = subprocess.run(["sc.exe", *args], capture_output=True, text=True, creationflags=NO_WINDOW)
    if check and result.returncode != 0:
        raise RuntimeError(f"sc {args[0]} failed ({result.returncode}): {(result.stdout or result.stderr).strip()}")
    return result


def parse_state(output: str) -> str:
    for line in output.splitlines():
        if line.strip().upper().startswith("STATE"):
            return "running" if "RUNNING" in line.upper() else "stopped"
    return "stopped"


def status() -> str:
    """"missing", "stopped" or "running". No admin needed."""
    if sys.platform != "win32":
        return "missing"
    result = _sc("query", SERVICE_NAME)
    if result.returncode == 1060:  # ERROR_SERVICE_DOES_NOT_EXIST
        return "missing"
    return parse_state(result.stdout)


def _stop_and_wait(timeout: float = 15.0) -> None:
    _sc("stop", SERVICE_NAME)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and status() == "running":
        time.sleep(0.3)


def _replace(src: Path, dest: Path) -> None:
    for attempt in range(20):  # the old injector can take a moment to let go of it
        try:
            shutil.copy2(src, dest)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.5)


def install(src: Path | None = None) -> None:
    src = src or bundled_exe()
    if not src.is_file():
        raise RuntimeError(f"{src} isn't there: the helper comes with the installed app")
    folder = install_dir()
    exists = status() != "missing"
    if exists:
        _stop_and_wait()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "logs").mkdir(exist_ok=True)
    exe = folder / EXE_NAME
    _replace(src, exe)
    command = subprocess.list2cmdline([str(exe), "service"])
    if exists:
        _sc("config", SERVICE_NAME, "binPath=", command, "start=", "auto", check=True)
    else:
        _sc("create", SERVICE_NAME, "binPath=", command, "start=", "auto", "obj=", "LocalSystem",
            "DisplayName=", DISPLAY_NAME, check=True)
    _sc("description", SERVICE_NAME,
        "Lets Curslink control apps running as administrator, UAC prompts and the lock screen. "
        "Turn it off in Curslink's Settings.")
    _sc("failure", SERVICE_NAME, "reset=", "86400", "actions=", "restart/5000/restart/30000//")
    _sc("start", SERVICE_NAME, check=True)
    log.info("input helper installed in %s and started", folder)


def uninstall() -> None:
    if status() != "missing":
        _stop_and_wait()
        _sc("delete", SERVICE_NAME, check=True)
    folder = install_dir()
    for attempt in range(20):
        try:
            if folder.exists():
                shutil.rmtree(folder)
            break
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.5)
    log.info("input helper removed")


def run_elevated(action: str) -> int:
    """Run ``curslink winhelper <action>`` as administrator (the UAC prompt)
    and wait for it. Its exit code; ERROR_CANCELLED if the prompt was declined."""
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG), ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", wintypes.LPVOID), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE),
        ]

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).with_name("curslinkw.exe")
        exe = exe if exe.is_file() else Path(sys.executable)
        params = f"winhelper {action}"
    else:
        exe = Path(sys.executable).with_name("pythonw.exe")
        exe = exe if exe.is_file() else Path(sys.executable)
        params = f"-m curslink winhelper {action}"
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x40 | 0x100  # SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    info.lpVerb = "runas"
    info.lpFile = str(exe)
    info.lpParameters = params
    info.nShow = 0
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        return ctypes.get_last_error() or 1
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.WaitForSingleObject(info.hProcess, 0xFFFFFFFF)
    code = wintypes.DWORD()
    k32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
    k32.CloseHandle(info.hProcess)
    return code.value
