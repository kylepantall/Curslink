"""macOS: bring the window under the pointer to the front during a file drag
that came from another computer.

A drag carried onto the Mac from another computer has no mouse-down of the
Mac's own behind it, so apps under the pointer get no mouse-dragged events
and many only take the drop once their window is in front. So while such a
drag is held, the window the pointer rests over is brought forward, as if
you had clicked it.
"""

from __future__ import annotations

import logging
import os
import sys

log = logging.getLogger(__name__)

_skip: set[int] = set()  # our own processes, as the last probe had them

SKIP_OWNERS = {"Window Server", "Dock", "SystemUIServer", "Control Center", "Notification Center"}


def window_under(x: float, y: float, skip_pids: set[int]) -> tuple[int, str, int] | None:
    """(owner pid, owner name, window number) of the topmost ordinary window
    at (x, y) in global points, or Finder's desktop when none is."""
    import Quartz

    infos = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID) or []
    for info in infos:  # front to back
        if int(info.get("kCGWindowLayer", 1)) != 0:
            continue
        pid = int(info.get("kCGWindowOwnerPID", 0))
        owner = str(info.get("kCGWindowOwnerName", ""))
        if pid in skip_pids or owner in SKIP_OWNERS or float(info.get("kCGWindowAlpha", 1)) <= 0:
            continue
        b = info.get("kCGWindowBounds") or {}
        bx, by, bw, bh = (float(b.get(k, 0)) for k in ("X", "Y", "Width", "Height"))
        if bx <= x < bx + bw and by <= y < by + bh:
            return pid, owner, int(info.get("kCGWindowNumber", 0))
    return None


def finder_pid() -> int | None:
    from AppKit import NSRunningApplication

    apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_("com.apple.finder")
    return int(apps[0].processIdentifier()) if apps and len(apps) else None


def raise_window(pid: int, x: float, y: float) -> None:
    """Activate the app and raise its window at (x, y) (Accessibility)."""
    from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication

    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if app is None:
        return
    try:
        try:
            import HIServices as AX  # (what pynput uses, so it's in the app)
        except ImportError:
            import ApplicationServices as AX

        ax_app = AX.AXUIElementCreateApplication(pid)
        err, windows = AX.AXUIElementCopyAttributeValue(ax_app, "AXWindows", None)
        for w in windows or []:
            _, pos = AX.AXUIElementCopyAttributeValue(w, "AXPosition", None)
            _, size = AX.AXUIElementCopyAttributeValue(w, "AXSize", None)
            ok_p, p = AX.AXValueGetValue(pos, AX.kAXValueCGPointType, None)
            ok_s, s = AX.AXValueGetValue(size, AX.kAXValueCGSizeType, None)
            if ok_p and ok_s and p.x <= x < p.x + s.width and p.y <= y < p.y + s.height:
                AX.AXUIElementPerformAction(w, "AXRaise")
                break
        # Frontmost through Accessibility too: since macOS 14 an app in the
        # background asking to activate another is often turned down.
        AX.AXUIElementSetAttributeValue(ax_app, "AXFrontmost", True)
    except Exception:  # Accessibility missing, or the app has no AX windows
        log.debug("couldn't raise the window under the pointer", exc_info=True)
    app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)


def ax_front_pid() -> int | None:
    """The frontmost app's pid as Accessibility has it, or None."""
    try:
        try:
            import HIServices as AX
        except ImportError:
            import ApplicationServices as AX

        err, app = AX.AXUIElementCopyAttributeValue(AX.AXUIElementCreateSystemWide(), "AXFocusedApplication", None)
        if not err and app is not None:
            err, pid = AX.AXUIElementGetPid(app, None)
            if not err:
                return int(pid)
    except Exception:
        log.debug("couldn't ask Accessibility which app is in front", exc_info=True)
    return None


def top_window_pid(skip: set[int] = frozenset()) -> int | None:
    """The owner of the front-most ordinary window on screen (fresh in any
    process, unlike NSWorkspace's frontmostApplication, which only updates
    in one running an AppKit loop)."""
    import Quartz

    infos = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID) or []
    for info in infos:
        if int(info.get("kCGWindowLayer", 1)) != 0 or float(info.get("kCGWindowAlpha", 1)) <= 0:
            continue
        pid = int(info.get("kCGWindowOwnerPID", 0))
        if pid in skip or str(info.get("kCGWindowOwnerName", "")) in SKIP_OWNERS:
            continue
        return pid
    return None


def front_pid() -> int | None:
    """The frontmost app's pid: Accessibility's answer, else the owner of the
    front-most window."""
    pid = ax_front_pid()
    return pid if pid is not None else top_window_pid({os.getpid(), *_skip})


def probe(skip: set[int]):
    """(key, name, x, y) for the window under the pointer, or None."""
    from curslink.inputs.pynput_backend import mac_cursor

    global _skip
    _skip = set(skip)
    x, y = mac_cursor()
    hit = window_under(x, y, skip)
    if hit is None:
        pid = finder_pid()  # the desktop
        return ((pid, 0), "Finder (the desktop)", x, y) if pid else None
    return (hit[0], hit[2]), hit[1], x, y


def bring(key, x: float, y: float) -> None:
    raise_window(key[0], x, y)


def is_front(key) -> bool:
    """In front by Accessibility's answer, or its window is the front-most
    one on screen (on 0.4.37 Accessibility never named the app it had just
    brought forward, so every try looked like it had failed)."""
    if ax_front_pid() == key[0]:
        return True
    if key[1] == 0:  # the desktop: Finder in front by Accessibility only
        return False
    return top_window_pid({os.getpid(), *_skip}) == key[0]


def token(key) -> int:
    return key[0]


def bring_here(pid: int) -> None:
    """The helper's go (see curslink.files.focus.from_helper)."""
    from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication

    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if app is not None:
        app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
