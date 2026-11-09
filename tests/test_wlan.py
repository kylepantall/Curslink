"""Windows Wi-Fi low-latency mode (curslink.net.wlan), against a fake wlanapi."""

import sys

from curslink.net import wlan


class FakeWlan:
    def __init__(self, refuse=()):
        self.calls, self.refuse, self.closed = [], set(refuse), False

    def interfaces(self):
        return [("g1", "Intel Wi-Fi 6E AX211", True), ("g2", "Spare adapter", False)]

    def set_flag(self, guid, opcode, on):
        self.calls.append((guid, opcode, on))
        return 5 if opcode in self.refuse else 0

    def close(self):
        self.closed = True


def test_connected_adapters_get_streaming_mode_and_no_background_scans(caplog):
    fake, done = FakeWlan(), {}
    with caplog.at_level("INFO"):
        wlan.apply(fake, done)
        wlan.apply(fake, done)
    assert fake.calls == [("g1", wlan.MEDIA_STREAMING_MODE, True), ("g1", wlan.BACKGROUND_SCAN_ENABLED, False)] * 2
    assert sum("low-latency mode" in r.message for r in caplog.records) == 1  # logged once


def test_a_refused_setting_is_reported():
    fake, done = FakeWlan(refuse={wlan.BACKGROUND_SCAN_ENABLED}), {}
    wlan.apply(fake, done)
    assert done["Intel Wi-Fi 6E AX211"] == (True, False)


def test_holds_the_handle_until_stopped(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    fake = FakeWlan()
    held = wlan.LowLatencyWifi(reapply_s=0.01, factory=lambda: fake)
    held.start()
    import time
    for _ in range(100):
        if fake.calls:
            break
        time.sleep(0.01)
    assert fake.calls and not fake.closed
    held.stop()
    assert fake.closed


def test_nothing_happens_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    held = wlan.LowLatencyWifi(factory=lambda: (_ for _ in ()).throw(AssertionError("opened")))
    held.start()
    held.stop()
