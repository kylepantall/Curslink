"""Pointer playback on an agent: moves arriving in clumps come out evenly."""

from conftest import FakeSink
from curslink.net.client import AgentSession
from curslink.protocol import Message
from curslink.smoothing import MAX_DELAY_S, PointerSmoother


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def make():
    clock, moves = Clock(), []
    s = PointerSmoother(lambda x, y: moves.append((x, y)), clock=clock, threaded=False)
    return s, clock, moves


def test_moves_held_up_in_clumps_come_out_evenly():
    """Wi-Fi style: a report every 8 ms (1 px per ms), but every 250 ms the
    link holds them for 25 ms and they land at once. Once the smoother has
    seen that, the pointer keeps a steady pace instead of stalling and
    jumping 25 px."""
    s, clock, moves = make()
    s.reset(0, 0)
    events = []  # (arrival, ts, x)
    for i in range(375):  # 3 s
        ts = i * 0.008
        arrival = ts + 0.001
        if (ts % 0.25) < 0.025:
            arrival = ts - (ts % 0.25) + 0.026  # held until the clump is released
        events.append((arrival, ts, i * 8))
    events.sort()
    t, k, track = 0.0, 0, []
    while t < 3.0:
        clock.t = 100.0 + t
        while k < len(events) and events[k][0] <= t:
            a, ts, x = events[k]
            s.add(x, 0, (100.0 + ts) * 1000)
            k += 1
        s.step()
        if moves:
            track.append((t, moves[-1][0]))
        t += 0.004
    late = [x for tt, x in track if tt > 1.5]
    steps = [b - a for a, b in zip(late, late[1:])]
    assert min(steps) >= 0 and max(steps) <= 8   # 4 px per 4 ms tick, give or take
    assert s.delay_s <= MAX_DELAY_S


def test_reports_are_interpolated_between():
    s, clock, moves = make()
    s.reset(0, 0)
    for i in range(30):  # a 60 Hz trackpad: a report every 16 ms
        clock.t = 100.0 + i * 0.016
        s.add(i * 16, 0, clock.t * 1000)
        for _ in range(4):
            s.step()
            clock.t += 0.004
    # Positions between the reports, not only multiples of 16.
    assert any(x % 16 for x, _ in moves[10:])


def test_snap_goes_to_the_newest_position_at_once():
    s, clock, moves = make()
    s.reset(0, 0)
    s.add(0, 0, clock.t * 1000)
    s.add(50, 60, clock.t * 1000 + 30)
    s.snap()
    assert moves[-1] == (50, 60)


def test_delay_never_exceeds_the_cap():
    s, clock, moves = make()
    s.reset(0, 0)
    for i in range(100):  # wildly late, every other report
        clock.t = 100.0 + i * 0.01 + (0.3 if i % 2 else 0.0)
        s.add(i, 0, (100.0 + i * 0.01) * 1000)
        s.step()
    assert 0 < s.delay_s <= MAX_DELAY_S


def test_session_plays_timestamped_moves_and_snaps_before_a_click():
    sink = FakeSink()
    s, clock, moves = make()
    session = AgentSession(sink, lambda *a: None, smoother=s)
    session.handle(Message("enter", {"x": 1, "y": 2, "effect": "none"}))
    session.handle(Message("move", {"x": 5, "y": 5, "ts": clock.t * 1000}))
    session.handle(Message("move", {"x": 9, "y": 9, "ts": clock.t * 1000 + 20}))
    session.handle(Message("button", {"button": "left", "pressed": True}))
    assert moves == [(1, 2), (9, 9)]           # through the smoother, snapped first
    assert sink.calls == [("button", "left", True)]
    session.handle(Message("move", {"x": 3, "y": 4}))  # an old host: no ts
    assert sink.calls[-1] == ("move", 3, 4)


def test_pong_echoes_the_ping_time():
    session = AgentSession(FakeSink(), lambda *a: None)
    assert b'"ts":12.5' in session.handle(Message("ping", {"n": 1, "ts": 12.5}))
    assert b"ts" not in session.handle(Message("ping", {"n": 2}))


def test_host_logs_where_pointer_time_goes():
    from types import SimpleNamespace

    from curslink.app import CaptureStats, capture_report

    bridge = SimpleNamespace(stats=CaptureStats())
    capture = SimpleNamespace(tap_times=[0.0001, 0.0003])
    conn = SimpleNamespace(rtts=[0.002, 0.004, 0.050])
    server = SimpleNamespace(connections={"pc": conn})
    assert capture_report(bridge, capture, server, "pc", 10.0) is None  # mouse didn't move
    bridge.stats.reports = 500
    bridge.stats.sent(0.0002)
    capture.tap_times[:] = [0.0001, 0.0003]
    conn.rtts[:] = [0.002, 0.004, 0.050]
    line = capture_report(bridge, capture, server, "pc", 10.0)
    assert "500 mouse reports" in line and "50/s" in line and "round trip" in line
    assert bridge.stats.reports == 0 and capture.tap_times == [] and conn.rtts == []


def _drive(s, clock, start, seconds, late_of):
    """Reports every 8 ms for `seconds`, each arriving late_of(i) late."""
    events, arrival = [], 0.0
    n = int(seconds / 0.008)
    for i in range(n):
        ts = start + i * 0.008
        arrival = max(arrival, ts + late_of(i))  # in order, like TCP: a late one holds up the rest
        events.append((arrival, ts, i))
    t, k, delays = start, 0, []
    while t < start + seconds:
        clock.t = 100.0 + t
        while k < len(events) and events[k][0] <= t:
            a, ts, i = events[k]
            s.add(i, 0, (100.0 + ts) * 1000)
            k += 1
        s.step()
        delays.append(s.delay_s)
        t += 0.004
    return delays


def test_delay_adapts_to_the_link_and_never_jumps():
    """A clean link, then three seconds of bad Wi-Fi (every 10th report 80 ms
    late), then clean again: the delay grows to cover the bad spell and
    shrinks back after it, a little at a time rather than in jumps."""
    s, clock, moves = make()
    s.reset(0, 0)
    clean = _drive(s, clock, 0.0, 2.0, lambda i: 0.001)
    assert clean[-1] < 0.015
    bad = _drive(s, clock, 2.0, 3.0, lambda i: 0.08 if i % 10 == 0 else 0.001)
    assert 0.07 <= bad[-1] <= MAX_DELAY_S
    after = _drive(s, clock, 5.0, 6.0, lambda i: 0.001)
    assert after[-1] < 0.02
    delays = clean + bad + after
    steps = [abs(b - a) for a, b in zip(delays, delays[1:])]
    assert max(steps) <= 0.4 * 0.004 + 1e-9   # at most 1.6 ms per 4 ms tick
