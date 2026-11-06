"""Native window effects: Mica on Windows 11, glass (vibrancy) on macOS,
and hiding the Dock icon on macOS while only the menu bar icon is showing.

Everything here is best effort. Each call logs what it did and returns
False if it couldn't, and the app then draws solid backgrounds instead.
``CURSLINK_NO_EFFECTS=1`` turns the effects off.
"""

from __future__ import annotations

import logging
import os
import sys

log = logging.getLogger(__name__)


def effects_allowed() -> bool:
    return os.environ.get("CURSLINK_NO_EFFECTS", "").strip().lower() in ("", "0", "false", "no")


def can_glass() -> bool:
    if not effects_allowed():
        return False
    if sys.platform == "win32":
        return sys.getwindowsversion().build >= 22000  # Windows 11
    if sys.platform == "darwin":
        try:
            import AppKit  # noqa: F401
        except ImportError:
            return False
        return True
    return False


def prepare(window) -> None:
    """Call before the window is first shown, when glass will be used."""
    from PySide6.QtCore import Qt

    window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    if sys.platform == "darwin":
        window.setUnifiedTitleAndToolBarOnMac(True)


def apply_glass(window, dark: bool) -> bool:
    """Put the platform's backdrop behind ``window``. Call after it's shown
    (it needs the native window), and again when light or dark changes."""
    try:
        if sys.platform == "win32":
            return _windows(window, dark)
        if sys.platform == "darwin":
            return _mac(window, dark)
    except Exception:
        log.warning("couldn't apply the window backdrop", exc_info=True)
    return False


def set_dark_title_bar(window, dark: bool) -> None:
    """Windows 10/11: match the title bar to the theme (no-op elsewhere)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        hwnd = int(window.winId())
        value = ctypes.c_int(1 if dark else 0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
    except Exception:
        log.debug("couldn't set the title bar colour", exc_info=True)


# ------------------------------------------------------------------ Windows

def _windows(window, dark: bool) -> bool:
    import ctypes
    from ctypes import wintypes

    class MARGINS(ctypes.Structure):
        _fields_ = [("left", ctypes.c_int), ("right", ctypes.c_int), ("top", ctypes.c_int), ("bottom", ctypes.c_int)]

    dwm = ctypes.windll.dwmapi
    hwnd = wintypes.HWND(int(window.winId()))
    set_dark_title_bar(window, dark)
    margins = MARGINS(-1, -1, -1, -1)
    hr = dwm.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins))
    if hr != 0:
        log.warning("DwmExtendFrameIntoClientArea failed: 0x%08X", hr & 0xFFFFFFFF)
        return False
    backdrop = ctypes.c_int(2)  # DWMSBT_MAINWINDOW (Mica)
    hr = dwm.DwmSetWindowAttribute(hwnd, 38, ctypes.byref(backdrop), ctypes.sizeof(backdrop))
    if hr != 0:
        log.warning("Mica isn't available (DWMWA_SYSTEMBACKDROP_TYPE 0x%08X)", hr & 0xFFFFFFFF)
        return False
    log.info("window backdrop: Mica (%s)", "dark" if dark else "light")
    return True


# -------------------------------------------------------------------- macOS

_mac_views: dict[int, object] = {}


def _ns_window(window):
    import objc

    view = objc.objc_object(c_void_p=int(window.winId()))
    return view.window()


def _mac(window, dark: bool) -> bool:
    import AppKit

    ns_window = _ns_window(window)
    if ns_window is None:
        log.warning("no native window yet for the backdrop")
        return False
    ns_window.setTitlebarAppearsTransparent_(True)
    ns_window.setTitleVisibility_(AppKit.NSWindowTitleHidden)
    ns_window.setStyleMask_(ns_window.styleMask() | AppKit.NSWindowStyleMaskFullSizeContentView)
    ns_window.setOpaque_(False)
    ns_window.setBackgroundColor_(AppKit.NSColor.clearColor())
    content = ns_window.contentView()
    frame_view = content.superview()
    key = id(window)
    effect = _mac_views.get(key)
    if effect is None:
        effect = AppKit.NSVisualEffectView.alloc().initWithFrame_(frame_view.bounds())
        effect.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
        effect.setBlendingMode_(AppKit.NSVisualEffectBlendingModeBehindWindow)
        effect.setState_(AppKit.NSVisualEffectStateFollowsWindowActiveState)
        frame_view.addSubview_positioned_relativeTo_(effect, AppKit.NSWindowBelow, content)
        _mac_views[key] = effect
    effect.setMaterial_(AppKit.NSVisualEffectMaterialSidebar)
    appearance = AppKit.NSAppearance.appearanceNamed_(
        AppKit.NSAppearanceNameDarkAqua if dark else AppKit.NSAppearanceNameAqua)
    ns_window.setAppearance_(appearance)
    log.info("window backdrop: vibrancy (%s)", "dark" if dark else "light")
    return True


def set_dock_icon(visible: bool) -> None:
    """macOS: show the app in the Dock only while a window is open."""
    if sys.platform != "darwin":
        return
    try:
        import AppKit

        app = AppKit.NSApplication.sharedApplication()
        policy = (AppKit.NSApplicationActivationPolicyRegular if visible
                  else AppKit.NSApplicationActivationPolicyAccessory)
        if app.activationPolicy() != policy:
            app.setActivationPolicy_(policy)
            if visible:
                app.activateIgnoringOtherApps_(True)
            log.info("Dock icon %s", "shown" if visible else "hidden")
    except Exception:
        log.warning("couldn't change the Dock icon", exc_info=True)


def dock_icon_visible() -> bool:
    """macOS: whether the app is in the Dock right now (True elsewhere)."""
    if sys.platform != "darwin":
        return True
    try:
        import AppKit

        policy = AppKit.NSApplication.sharedApplication().activationPolicy()
        return policy == AppKit.NSApplicationActivationPolicyRegular
    except Exception:
        return True  # can't tell: assume it is, so a click still opens the window
