"""Starting the desktop app when the user signs in.

* Windows: a value under ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``.
* macOS: a LaunchAgent plist in ``~/Library/LaunchAgents``.
* Linux and other freedesktop systems: ``~/.config/autostart/curslink.desktop``.

All three are per user, so none of them needs admin rights.
"""

from __future__ import annotations

import logging
import os
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

APP_ID = "io.github.orimovo.curslink"
# The LaunchAgent label before 0.4.3, replaced by refresh().
OLD_APP_IDS = ("io.github.kylepantall.curslink",)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "Curslink"


def launch_command(minimized: bool = True) -> list[str]:
    """How to start the desktop app: the packaged executable, or Python from source."""
    args = ["gui"] + (["--minimized"] if minimized else [])
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        if sys.platform == "win32":
            # The windowed build (curslinkw.exe) has no console window to
            # flash up at sign-in. Windows names are case-insensitive, so this
            # used to look for "Curslink.exe", find the console curslink.exe,
            # and flash a console that exited with a usage error.
            gui = exe.with_name("curslinkw.exe")
            if gui.exists():
                exe = gui
        return [str(exe), *args]
    python = Path(sys.executable)
    if sys.platform == "win32":
        pythonw = python.with_name("pythonw.exe")
        if pythonw.exists():
            python = pythonw
    return [str(python), "-m", "curslink", *args]


class Autostart:
    """Turns start-at-sign-in on and off for one platform.

    ``home``/``config_home``/``registry`` exist so tests can point it somewhere harmless.
    """

    def __init__(self, platform: str | None = None, home: Path | None = None,
                 config_home: Path | None = None, registry=None):
        self.platform = platform or sys.platform
        self.home = home or Path.home()
        self.config_home = config_home or Path(os.environ.get("XDG_CONFIG_HOME") or self.home / ".config")
        self._registry = registry

    # -- where it lives -----------------------------------------------------
    @property
    def path(self) -> Path | None:
        if self.platform == "win32":
            return None
        if self.platform == "darwin":
            return self.home / "Library" / "LaunchAgents" / f"{APP_ID}.plist"
        return self.config_home / "autostart" / "curslink.desktop"

    @property
    def registry(self):
        if self._registry is None:
            import winreg

            self._registry = winreg
        return self._registry

    # -- the public bit -----------------------------------------------------
    def is_enabled(self) -> bool:
        if self.platform == "win32":
            return self._read_run_value() is not None
        path = self.path
        if not path.exists():
            return False
        if self.platform != "darwin":
            # A user can switch an autostart entry off without deleting it.
            text = path.read_text(errors="replace")
            return "Hidden=true" not in text and "X-GNOME-Autostart-enabled=false" not in text
        return True

    def command(self) -> list[str] | None:
        """The command that runs at sign-in, if start-at-sign-in is on."""
        if self.platform == "win32":
            value = self._read_run_value()
            return None if value is None else _split_windows(value)
        path = self.path
        if not path.exists():
            return None
        if self.platform == "darwin":
            with open(path, "rb") as fh:
                return list(plistlib.load(fh).get("ProgramArguments", []))
        for line in path.read_text(errors="replace").splitlines():
            if line.startswith("Exec="):
                return shlex.split(line[len("Exec="):])
        return None

    def set_enabled(self, enabled: bool, command: list[str] | None = None) -> None:
        command = command or launch_command(minimized=True)
        if self.platform == "win32":
            self._set_windows(enabled, command)
            return
        path = self.path
        if not enabled:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.platform == "darwin":
            with open(path, "wb") as fh:
                plistlib.dump({
                    "Label": APP_ID,
                    "ProgramArguments": command,
                    "RunAtLoad": True,
                    "ProcessType": "Interactive",
                }, fh)
        else:
            path.write_text(desktop_entry(command, autostart=True))

    def refresh(self) -> bool:
        """If start-at-sign-in is on but runs another command than this copy
        would write (the executable moved in an update), rewrite it."""
        if not getattr(sys, "frozen", False):
            return False
        want = launch_command(minimized=True)
        if self.platform == "darwin":
            agents = self.home / "Library" / "LaunchAgents"
            old = [agents / f"{name}.plist" for name in OLD_APP_IDS if (agents / f"{name}.plist").exists()]
            if old:
                for path in old:
                    path.unlink(missing_ok=True)
                log.info("start-at-sign-in had an old name; renaming it")
                self.set_enabled(True, want)
                return True
        if not self.is_enabled():
            return False
        if self.command() == want:
            return False
        log.info("start-at-sign-in pointed at an old copy; updating it")
        self.set_enabled(True, want)
        return True

    # -- Windows ------------------------------------------------------------
    def _read_run_value(self) -> str | None:
        reg = self.registry
        try:
            with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY) as key:
                value, _ = reg.QueryValueEx(key, RUN_VALUE)
        except OSError:
            return None
        return value or None

    def _set_windows(self, enabled: bool, command: list[str]) -> None:
        reg = self.registry
        with reg.CreateKey(reg.HKEY_CURRENT_USER, RUN_KEY) as key:
            if enabled:
                reg.SetValueEx(key, RUN_VALUE, 0, reg.REG_SZ, subprocess.list2cmdline(command))
            else:
                try:
                    reg.DeleteValue(key, RUN_VALUE)
                except OSError:
                    pass


def _split_windows(value: str) -> list[str]:
    """Undo ``subprocess.list2cmdline`` well enough for what we write."""
    out, cur, quoted = [], "", False
    for ch in value:
        if ch == '"':
            quoted = not quoted
        elif ch == " " and not quoted:
            if cur:
                out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


def desktop_entry(command: list[str], autostart: bool = False, icon: str = "curslink") -> str:
    """A freedesktop ``.desktop`` file that runs ``command``."""
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        "Name=Curslink",
        "GenericName=Input switcher",
        "Comment=Look at a device and your mouse and keyboard follow",
        "Exec=" + " ".join(shlex.quote(c) for c in command),
        f"Icon={icon}",
        "Terminal=false",
        "Categories=Utility;",
        "StartupNotify=false",
    ]
    if autostart:
        lines += ["X-GNOME-Autostart-enabled=true", "X-GNOME-Autostart-Delay=3"]
    return "\n".join(lines) + "\n"


def autostart() -> Autostart:
    return Autostart()
