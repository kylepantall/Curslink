"""Held-button drags driven from another computer: a Mac agent posts real
drag events, and a Windows host holds a drop's button-up like a Mac does."""

import sys
import types

import pytest

import curslink.inputs.pynput_backend as pb


def fake_quartz(monkeypatch, held=()):
    posted = []
    q = types.SimpleNamespace(
        kCGEventMouseMoved=5, kCGEventLeftMouseDragged=6, kCGEventRightMouseDragged=7,
        kCGEventOtherMouseDragged=27, kCGMouseButtonLeft=0, kCGMouseButtonRight=1, kCGMouseButtonCenter=2,
        kCGEventSourceStateCombinedSessionState=0, kCGHIDEventTap=0,
    )
    q.CGEventSourceButtonState = lambda state, number: number in held
    q.CGEventCreateMouseEvent = lambda src, kind, at, button: {"kind": kind, "at": at, "button": button}
    q.CGEventSetIntegerValueField = lambda ev, field, value: ev.__setitem__(field, value)
    q.CGEventPost = lambda tap, ev: posted.append(ev)
    monkeypatch.setitem(sys.modules, "Quartz", q)
    return posted


def mac_sink(monkeypatch, post_moves=True):
    calls = []
    monkeypatch.setattr(pb.sys, "platform", "darwin")
    monkeypatch.setattr(pb, "_mac_warp", lambda x, y: calls.append(("warp", x, y)))
    monkeypatch.setattr(pb, "_mac_attach_cursor", lambda attached: calls.append(("attach", attached)))
    sink = pb.PynputSink.__new__(pb.PynputSink)  # no pynput controllers needed
    sink.post_moves = post_moves
    return sink, calls


def test_a_mac_agent_drags_while_the_left_button_is_held(monkeypatch):
    # 0.4.75: every move was a warp, so a window being dragged on the Mac
    # stayed put until let go, and Finder never began a file drag.
    posted = fake_quartz(monkeypatch, held={0})
    sink, calls = mac_sink(monkeypatch)
    sink.move_to(100, 200)
    sink.move_to(110, 195)
    assert [e["kind"] for e in posted] == [6, 6]  # left mouse dragged
    assert posted[1]["at"] == (110.0, 195.0)
    assert (posted[1][pb._MAC_DELTA_X], posted[1][pb._MAC_DELTA_Y]) == (10, -5)
    assert calls == []  # no warps


def test_a_mac_agent_hovers_when_no_button_is_held(monkeypatch):
    posted = fake_quartz(monkeypatch)
    sink, _ = mac_sink(monkeypatch)
    sink.move_to(5, 6)
    assert posted[0]["kind"] == 5  # mouse moved


def test_a_mac_agent_right_drag(monkeypatch):
    posted = fake_quartz(monkeypatch, held={1})
    sink, _ = mac_sink(monkeypatch)
    sink.move_to(5, 6)
    assert posted[0]["kind"] == 7


def test_parking_and_coming_home_still_warp_on_a_mac(monkeypatch):
    posted = fake_quartz(monkeypatch)
    sink, calls = mac_sink(monkeypatch)
    sink.park(400, 300)
    assert calls == [("warp", 400, 300)] and posted == []
    home, calls = mac_sink(monkeypatch, post_moves=False)  # the host's own sink
    home.move_to(10, 20)
    assert calls == [("warp", 10, 20), ("attach", True)] and posted == []


def test_the_agent_parks_with_park_when_input_leaves():
    from curslink.net.client import AgentSession
    from curslink.protocol import encode, decode

    class Sink:
        def __init__(self):
            self.calls = []

        def move_to(self, x, y):
            self.calls.append(("move", x, y))

        def park(self, x, y):
            self.calls.append(("park", x, y))

        def button(self, *a):
            pass

        def key(self, *a):
            pass

    sink = Sink()
    session = AgentSession(sink, lambda *a, **k: None)
    session.handle(decode(encode("leave", park=[7, 8])))
    assert sink.calls == [("park", 7, 8)]


# ------------------------------------------------------------ Windows host

class Data:
    def __init__(self, flags=0):
        self.flags = flags


class Suppressed(Exception):
    pass


def win_capture(monkeypatch, holding=True):
    capture = pb.PynputCapture.__new__(pb.PynputCapture)
    capture._grabbed = False
    capture._held_up = False
    capture._held_at = 0.0
    capture.hold_left_up = lambda: holding
    buttons = []
    capture._handler = types.SimpleNamespace(on_button=lambda b, p: buttons.append((b, p)))

    def suppress():
        raise Suppressed()

    capture._mouse_listener = types.SimpleNamespace(suppress_event=suppress)
    released = []
    monkeypatch.setattr(pb, "_drag_release", lambda x, y: released.append((x, y)))
    monkeypatch.setattr(pb, "_win_cursor_pos", lambda: (50, 60))
    capture._mac = False
    return capture, buttons, released


def test_a_windows_host_holds_the_button_up_while_a_drop_waits(monkeypatch):
    # Before 0.4.76 a Windows host had no real drag of files from a Mac:
    # apps took no drop and showed no drop feedback.
    capture, buttons, released = win_capture(monkeypatch)
    with pytest.raises(Suppressed):
        capture._win_mouse_filter(pb._WM_LBUTTONUP, Data())
    assert capture._held_up and buttons == [("left", False)]  # the drop is still seen
    capture.release_held()
    assert released == [(50, 60)] and not capture._held_up


def test_a_windows_host_lets_injected_and_ordinary_button_ups_by(monkeypatch):
    capture, buttons, _ = win_capture(monkeypatch)
    assert capture._win_mouse_filter(pb._WM_LBUTTONUP, Data(flags=1)) is True  # our own release
    capture, buttons, _ = win_capture(monkeypatch, holding=False)
    assert capture._win_mouse_filter(pb._WM_LBUTTONUP, Data()) is True
    assert capture._win_mouse_filter(0x0200, Data()) is True  # a move
    assert buttons == [] and not capture._held_up


def test_a_new_click_lets_go_of_a_held_button_on_windows(monkeypatch):
    capture, _, released = win_capture(monkeypatch)
    with pytest.raises(Suppressed):
        capture._win_mouse_filter(pb._WM_LBUTTONUP, Data())
    capture.hold_left_up = lambda: False
    assert capture._win_mouse_filter(pb._WM_LBUTTONDOWN, Data()) is True
    assert released and not capture._held_up


@pytest.mark.parametrize("platform, real", [("darwin", True), ("win32", True), ("linux", False)])
def test_an_agent_drags_files_from_the_host_for_real_on_a_mac_too(monkeypatch, tmp_path, platform, real):
    """Kyle, 0.4.76: files dragged from the PC to a Mac agent only showed a
    card and went to Downloads, and no window came forward under them."""
    import sys
    import types

    from conftest import FakeSink
    from curslink import app
    from curslink.net.client import AgentSession

    monkeypatch.setattr(sys, "platform", platform)
    sink = FakeSink()
    session = AgentSession(sink, lambda *a: None)
    cfg = types.SimpleNamespace(files=types.SimpleNamespace(folder=tmp_path))
    overlay = types.SimpleNamespace(command=lambda line: None, pid=None)
    native = app.receive_files(cfg, session, overlay)
    assert (native.press is not None) is real
    assert (native.follow is not None) is real
    assert native.end_watch is (platform == "darwin")
