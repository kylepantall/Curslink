"""macOS: keep Curslink's helper processes out of the Dock.

The app, its linking service and its helpers (the switch animation, the
pairing window, the permission checks) are all the same executable inside
Curslink.app, so macOS gives each one that touches AppKit, Tk or the camera
its own Dock icon. Only the app should have one.
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)

# NSApplicationActivationPolicy
ACCESSORY = 1   # may show windows (the pairing window), no Dock icon
PROHIBITED = 2  # no windows of its own, no Dock icon


def hide(policy: int = PROHIBITED) -> None:
    """Call early in a helper process. For a Tk helper, call it after
    ``tk.Tk()``, so Tk has made its own NSApplication first."""
    if sys.platform != "darwin":
        return
    try:
        import AppKit

        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(policy)
        if policy == ACCESSORY:
            app.activateIgnoringOtherApps_(True)  # bring its window to the front
    except Exception:
        log.debug("couldn't keep this process out of the Dock", exc_info=True)
