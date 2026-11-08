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
    except Exception:  # Accessibility missing, or the app has no AX windows
        log.debug("couldn't raise the window under the pointer", exc_info=True)
    app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)


class FocusFollower:
    """Called repeatedly while a drag is held: once the pointer has rested
    over another app's window for a moment, bring that window forward."""

    REST_S = 0.25

    def __init__(self, skip_pids=lambda: set()):
        self.skip_pids = skip_pids
        self._candidate: tuple[int, int] | None = None
        self._since = 0.0
        self._focused: tuple[int, int] | None = None

    def reset(self) -> None:
        self._candidate = self._focused = None

    def tick(self, now: float) -> None:
        if sys.platform != "darwin":
            return
        try:
            from curslink.inputs.pynput_backend import mac_cursor

            x, y = mac_cursor()
            skip = {os.getpid(), *self.skip_pids()}
            hit = window_under(x, y, skip)
            if hit is None:
                pid = finder_pid()  # the desktop
                key, name = ((pid, 0), "Finder (the desktop)") if pid else (None, "")
            else:
                key, name = (hit[0], hit[2]), hit[1]
            if key is None:
                return
            if key != self._candidate:
                self._candidate, self._since = key, now
                return
            if key == self._focused or now - self._since < self.REST_S:
                return
            self._focused = key
            log.info("drop: bringing %s forward under the pointer", name)
            raise_window(key[0], x, y)
        except Exception:  # pragma: no cover - macOS only
            log.debug("couldn't follow the pointer's window", exc_info=True)
