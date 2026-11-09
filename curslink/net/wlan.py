"""Windows: hold the Wi-Fi adapter in its low-latency mode while Curslink runs.

Two things make a Windows laptop on Wi-Fi feel laggy as a Curslink device:

* power saving: between packets the adapter dozes and only wakes for the
  access point's beacons, so a move that arrives during a doze waits there
  (30-100 ms, more on some routers);
* background scans: every minute or so Windows looks for other networks,
  which takes the radio off our channel for 100 ms to over a second. These
  show in an agent's log as "max ... ms" spikes and a playback delay that
  climbs to its cap.

The WLAN API lets an app ask for "media streaming mode" (the adapter stays
awake, as for a video call) and for background scans to stop. Both last only
while the handle that asked is open, so Curslink keeps one open for as long
as it runs and Windows goes back to normal when it exits. No admin needed.
The cost: the adapter uses a little more power and won't hunt for a better
access point while Curslink runs. [network] optimise = false turns this off.
"""

from __future__ import annotations

import logging
import sys
import threading

log = logging.getLogger(__name__)

REAPPLY_S = 30.0  # again this often: an adapter that reconnects (or a new one) starts fresh

# WLAN_INTF_OPCODE values (wlanapi.h)
BACKGROUND_SCAN_ENABLED = 2
MEDIA_STREAMING_MODE = 3
CONNECTED = 1  # wlan_interface_state_connected


class _Wlan:
    """The few wlanapi.dll calls we need (Windows only)."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                        ("Data4", ctypes.c_ubyte * 8)]

        class Info(ctypes.Structure):
            _fields_ = [("guid", GUID), ("description", ctypes.c_wchar * 256), ("state", ctypes.c_uint)]

        class InfoList(ctypes.Structure):
            _fields_ = [("count", wintypes.DWORD), ("index", wintypes.DWORD)]

        self.ctypes, self.wintypes, self.GUID, self.Info, self.InfoList = ctypes, wintypes, GUID, Info, InfoList
        self.dll = ctypes.WinDLL("wlanapi.dll")
        self.handle = wintypes.HANDLE()
        negotiated = wintypes.DWORD()
        err = self.dll.WlanOpenHandle(2, None, ctypes.byref(negotiated), ctypes.byref(self.handle))
        if err:
            raise OSError(err, "WlanOpenHandle failed")

    def interfaces(self) -> list[tuple[object, str, bool]]:
        """(guid, description, connected) for each Wi-Fi adapter."""
        ctypes = self.ctypes
        listing = ctypes.POINTER(self.InfoList)()
        err = self.dll.WlanEnumInterfaces(self.handle, None, ctypes.byref(listing))
        if err:
            raise OSError(err, "WlanEnumInterfaces failed")
        try:
            count = listing.contents.count
            first = ctypes.addressof(listing.contents) + ctypes.sizeof(self.InfoList)
            infos = (self.Info * count).from_address(first)
            out = []
            for info in infos:
                guid = self.GUID.from_buffer_copy(info.guid)  # a copy: the list is freed below
                out.append((guid, info.description, info.state == CONNECTED))
            return out
        finally:
            self.dll.WlanFreeMemory(listing)

    def set_flag(self, guid, opcode: int, on: bool) -> int:
        ctypes = self.ctypes
        value = self.wintypes.BOOL(on)
        return self.dll.WlanSetInterface(self.handle, ctypes.byref(guid), opcode, ctypes.sizeof(value),
                                         ctypes.byref(value), None)

    def close(self) -> None:
        self.dll.WlanCloseHandle(self.handle, None)


def apply(wlan, done: dict) -> None:
    """Ask each connected adapter for streaming mode and no background scans.
    ``done``: description -> what was last logged, so each result is logged once."""
    for guid, name, connected in wlan.interfaces():
        if not connected:
            continue
        streaming = wlan.set_flag(guid, MEDIA_STREAMING_MODE, True)
        scans = wlan.set_flag(guid, BACKGROUND_SCAN_ENABLED, False)
        result = (streaming == 0, scans == 0)
        if done.get(name) != result:
            done[name] = result
            if all(result):
                log.info("wifi: %s held in low-latency mode (no power saving, no background scans) "
                         "while Curslink runs", name)
            else:
                log.info("wifi: %s: low-latency mode %s, background scans %s", name,
                         "on" if result[0] else f"refused ({streaming})",
                         "off" if result[1] else f"still on (refused, {scans})")


class LowLatencyWifi:
    """Holds the setting from :meth:`start` to :meth:`stop` (a no-op off Windows)."""

    def __init__(self, reapply_s: float = REAPPLY_S, factory=_Wlan):
        self.reapply_s = reapply_s
        self.factory = factory
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if sys.platform != "win32" or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="curslink-wifi", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            wlan = self.factory()
        except Exception as exc:  # no Wi-Fi service (a desktop on Ethernet), or no wlanapi
            log.info("wifi: can't ask for low-latency mode: %s", exc)
            return
        done: dict = {}
        try:
            while not self._stop.is_set():
                try:
                    apply(wlan, done)
                except Exception:
                    log.debug("wifi: couldn't apply low-latency mode", exc_info=True)
                self._stop.wait(self.reapply_s)
        finally:
            wlan.close()  # Windows puts the adapter back to normal

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
