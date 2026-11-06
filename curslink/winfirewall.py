"""Windows Firewall: lets other computers find and reach Curslink.

Windows asks "allow access?" the first time a program listens. If that was
cancelled, or the network is marked Public, Windows adds a rule that blocks
Curslink, and nearby devices can't see this PC (mDNS on UDP 5353) or pair
with it (TCP 24851). :func:`status` checks for that and :func:`allow` fixes
it, which needs admin rights, so Windows shows its own prompt.

Everywhere but Windows (and when not installed) it reports "ok".
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

WINDOWS = sys.platform == "win32"
RULE = "Curslink"


def programs() -> list[str]:
    """The installed executables: curslink.exe (the service) and curslinkw.exe (the app)."""
    if not getattr(sys, "frozen", False):
        return []
    here = Path(sys.executable).parent
    return [str(p) for p in (here / "curslink.exe", here / "curslinkw.exe") if p.exists()]


def _ps_list(paths: list[str]) -> str:
    return "@(" + ",".join("'" + p.replace("'", "''") + "'" for p in paths) + ")"


def _check_script(paths: list[str]) -> str:
    return (
        f"$ok=$true; foreach($p in {_ps_list(paths)}){{"
        "$r=@(Get-NetFirewallApplicationFilter -Program $p -ErrorAction SilentlyContinue | Get-NetFirewallRule "
        "| Where-Object {$_.Enabled -eq 'True' -and $_.Direction -eq 'Inbound'});"
        "if(@($r | Where-Object {$_.Action -eq 'Block'}).Count -gt 0 -or "
        "@($r | Where-Object {$_.Action -eq 'Allow'}).Count -eq 0){$ok=$false}};"
        "if($ok){'ok'}else{'blocked'}"
    )


def _fix_script(paths: list[str]) -> str:
    return (
        f"foreach($p in {_ps_list(paths)}){{"
        "Get-NetFirewallApplicationFilter -Program $p -ErrorAction SilentlyContinue | Get-NetFirewallRule "
        "| Where-Object {$_.Direction -eq 'Inbound' -and $_.Action -eq 'Block'} | Remove-NetFirewallRule;"
        f"Get-NetFirewallRule -DisplayName '{RULE}' -ErrorAction SilentlyContinue "
        "| Get-NetFirewallApplicationFilter | Where-Object {$_.Program -eq $p} | Get-NetFirewallRule "
        "| Remove-NetFirewallRule;"
        f"New-NetFirewallRule -DisplayName '{RULE}' -Description 'Lets your other computers find and "
        "reach Curslink on this network.' -Direction Inbound -Action Allow -Profile Any -Program $p | Out-Null}"
    )


def status(timeout: float = 30.0) -> str:
    """"ok", "blocked", or "unknown" (couldn't tell)."""
    paths = programs()
    if not WINDOWS or not paths:
        return "ok"
    try:
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _check_script(paths)],
            capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.strip().splitlines()
        answer = out[-1].strip() if out else "unknown"
    except Exception:
        log.warning("couldn't check Windows Firewall", exc_info=True)
        return "unknown"
    log.info("Windows Firewall for Curslink: %s", answer)
    return answer if answer in ("ok", "blocked") else "unknown"


def allow() -> bool:
    """Ask Windows (with its admin prompt) to let Curslink through. True if
    the prompt was accepted and the change started."""
    paths = programs()
    if not WINDOWS or not paths:
        return True
    import ctypes

    args = "-NoProfile -NonInteractive -WindowStyle Hidden -Command \"" + _fix_script(paths).replace('"', '\\"') + "\""
    log.info("asking Windows to allow Curslink through the firewall")
    # ShellExecuteW returns > 32 on success; "runas" shows the admin prompt.
    result = ctypes.windll.shell32.ShellExecuteW(None, "runas", "powershell.exe", args, None, 0)
    if result <= 32:
        log.warning("firewall change not made (ShellExecute %s: cancelled or refused)", result)
        return False
    return True
