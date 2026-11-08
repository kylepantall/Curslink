"""Keep Curslink's own overlay windows out of screen captures.

Mirror mode captures part of a screen and shows it on another; the edge
indicator's bar of light and label (and the mirrors themselves) drawn there
would come along and show up twice. Windows: the window's display affinity
(Windows 10 2004+). macOS: the window's sharing type, and mirror mode's
capture also leaves out this process's windows (see curslink.mirror.helper).
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)

_WDA_EXCLUDEFROMCAPTURE = 0x11


def exclude_from_capture(widget) -> None:
    """Leave ``widget``'s window out of screen captures, where the OS can."""
    try:
        wid = int(widget.winId())
    except Exception:  # pragma: no cover - no native window
        return
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.SetWindowDisplayAffinity(ctypes.c_void_p(wid), _WDA_EXCLUDEFROMCAPTURE)
        except Exception:  # pragma: no cover - Windows only
            log.debug("couldn't keep a window out of captures", exc_info=True)
    elif sys.platform == "darwin":
        try:
            import objc

            window = objc.objc_object(c_void_p=wid).window()
            if window is not None:
                window.setSharingType_(0)  # NSWindowSharingNone
        except Exception:  # pragma: no cover - macOS only
            log.debug("couldn't keep a window out of captures", exc_info=True)
