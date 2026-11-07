"""What's being dragged on macOS, read from the system's drag pasteboard.

Whoever starts a drag (Finder, an app) writes what it drags to the shared
drag pasteboard, which bumps its change count. So "the change count moved
since the button went down, and it holds file URLs" means "a file is being
dragged right now", with no window under the pointer needed. That's how
the Mac host notices a file drag leaving it.
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)


def drag_pasteboard() -> tuple[int, list[str]] | None:
    """(change count, file paths on it) of the drag pasteboard, or None
    off macOS or if it can't be read."""
    if sys.platform != "darwin":
        return None
    try:
        import AppKit

        name = getattr(AppKit, "NSPasteboardNameDrag", None) or getattr(AppKit, "NSDragPboard", None) \
            or "Apple CFPasteboard drag"
        pb = AppKit.NSPasteboard.pasteboardWithName_(name)
        count = int(pb.changeCount())
        opts = {getattr(AppKit, "NSPasteboardURLReadingFileURLsOnlyKey", "NSPasteboardURLReadingFileURLsOnlyKey"): True}
        urls = pb.readObjectsForClasses_options_([AppKit.NSURL], opts) or []
        paths = [str(u.path()) for u in urls if u.isFileURL() and u.path()]
        return count, paths
    except Exception:  # pragma: no cover - macOS only
        log.warning("couldn't read the drag pasteboard", exc_info=True)
        return None


def available() -> bool:
    return drag_pasteboard() is not None
