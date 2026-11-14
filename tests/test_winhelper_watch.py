"""The input helper hearing the mouse and keyboard for a Windows host (wire
v2): Windows keeps input over apps that run as administrator from the
host's own hooks, so the pointer couldn't leave the screen over them."""
import io
import types

from curslink.inputs.pynput_backend import HELPER_FILL_S, PynputCapture
from curslink.winhelper import events_pipe_name, pipe_name, wire
from curslink.winhelper.client import HelperWatch
from curslink.winhelper.watch import HookWatcher, key_report, mouse_report


def ev(x=0, y=0, flags=0, mouse_data=0, vk=0):
    return types.SimpleNamespace(pt=types.SimpleNamespace(x=x, y=y), flags=flags, mouseData=mouse_data, vkCode=vk)


def test_wire_v2_watch_and_events():
    assert wire.decode(wire.encode("watch", "grab")) == ("watch", "grab")
    assert wire.decode(b'["watch","everything"]') is None
    for fields, want in ((("pm", 10, -20, 1), ("pm", 10, -20, True)), (("pb", "x2", False, 0), ("pb", "x2", False, False)),
                         (("ps", 0, -1.0, 1), ("ps", 0.0, -1.0, True)), (("pk", 65, True, 1), ("pk", 65, True, True))):
        assert wire.decode_event(wire.encode(*fields)) == want
    for junk in (b"", b"[]", b'["pm",1,2]', b'["pm",1,2,true]', b'["pb","nose",true,1]', b'["pk",0,true,1]',
                 b'["pk",300,true,1]', b'["m",1,2,0]'):
        assert wire.decode_event(junk) is None


def test_old_helpers_ignore_watch():
    # A v1 helper's decode never knew "watch": the line was dropped, nothing injected.
    assert wire.apply(types.SimpleNamespace(), ("watch", "moves")) is None


def test_mouse_reports_by_mode():
    assert mouse_report(0x200, ev(5, 6), "off") is None
    assert mouse_report(0x200, ev(5, 6), "moves") == ("pm", 5, 6, 0)
    assert mouse_report(0x201, ev(), "moves") is None  # only moves while input is here
    assert mouse_report(0x200, ev(5, 6), "grab") == ("pm", 5, 6, 1)
    assert mouse_report(0x201, ev(), "grab") == ("pb", "left", True, 1)
    assert mouse_report(0x205, ev(), "grab") == ("pb", "right", False, 1)
    assert mouse_report(0x20B, ev(mouse_data=2 << 16), "grab") == ("pb", "x2", True, 1)
    assert mouse_report(0x20A, ev(mouse_data=(-120 & 0xFFFF) << 16), "grab") == ("ps", 0, -1.0, 1)
    assert mouse_report(0x20E, ev(mouse_data=240 << 16), "grab") == ("ps", 2.0, 0, 1)
    assert mouse_report(0x200, ev(5, 6, flags=1), "grab") is None  # injected: not the user's


def test_key_reports_only_while_grabbing():
    assert key_report(0x100, ev(vk=65), "moves") is None
    assert key_report(0x100, ev(vk=65), "grab") == ("pk", 65, True, 1)
    assert key_report(0x105, ev(vk=0x12), "grab") == ("pk", 0x12, False, 1)
    assert key_report(0x100, ev(vk=65, flags=0x10), "grab") is None


class Listener:
    def __init__(self):
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def test_the_helper_holds_input_back_only_with_the_host_reading():
    made = []
    w = HookWatcher(1, open_events=lambda: None, listeners=lambda w: made.append((Listener(), Listener())) or made[-1])
    w._thread = object()  # no pump thread in tests
    w.set_mode("grab")
    assert made and all(x.started for x in made[0])
    assert w.on_mouse(0x200, ev(1, 2)) is False  # nobody reading: let it through
    w._writer = io.BytesIO()
    assert w.on_mouse(0x200, ev(1, 2)) is True
    assert w.on_key(0x100, ev(vk=65)) is True
    assert w._queue.get_nowait() == ("pm", 1, 2, 1)
    w.set_mode("moves")
    assert w.on_mouse(0x200, ev(3, 4)) is False  # reported, never held back while input is here
    assert w.on_mouse(0x201, ev()) is False
    w.set_mode("off")
    assert all(x.stopped for x in made[0])
    assert w.on_mouse(0x200, ev(3, 4)) is False


class CmdPipe(io.BytesIO):
    def __init__(self):
        super().__init__()
        self.lines = []

    def write(self, data):
        self.lines.append(wire.decode(data))
        return len(data)


def test_the_host_asks_the_helper_to_listen_and_reads_its_events():
    cmd = CmdPipe()
    got, connected, opened = [], [], []
    events = io.BytesIO(b'["pm",10,20,0]\njunk\n["pk",65,true,1]\n')

    def open_events(path):
        opened.append(path)
        return events

    hw = HelperWatch(3, got.append, lambda: connected.append(True), opener=lambda path: (cmd, 2),
                     open_events=open_events, sleep=lambda s: None)
    hw.watch_once()
    assert opened == [events_pipe_name(3)] and hw.path == pipe_name(3)
    assert cmd.lines[0] == ("watch", "moves") and connected == [True]
    assert got == [("pm", 10, 20, False), ("pk", 65, True, True)]
    assert not hw.listening


def test_an_old_helper_is_left_alone(caplog):
    cmd = CmdPipe()
    hw = HelperWatch(3, lambda c: None, lambda: None, opener=lambda path: (cmd, 1),
                     open_events=lambda p: (_ for _ in ()).throw(AssertionError("no events pipe on v1")))
    hw.watch_once()
    hw.watch_once()
    assert cmd.lines == []
    assert len([r for r in caplog.records if "older Curslink" in r.getMessage()]) == 1


class Handler:
    def __init__(self):
        self.calls = []

    def on_pointer(self, x, y):
        self.calls.append(("pointer", x, y))

    def on_motion(self, dx, dy):
        self.calls.append(("motion", dx, dy))

    def on_button(self, b, p):
        self.calls.append(("button", b, p))

    def on_scroll(self, dx, dy):
        self.calls.append(("scroll", dx, dy))

    def on_key(self, k, p):
        self.calls.append(("key", k, p))


def capture():
    c = PynputCapture(hide_cursor=False)
    c._mac = False  # the helper is Windows only; on a Mac the event tap reads the wheel instead
    c._handler = Handler()
    return c


def test_helper_moves_fill_in_only_when_our_hook_is_deaf():
    c = capture()
    c._on_move(100, 100)  # our own hook hears the mouse
    c._helper_event(("pm", 100, 100, False))
    assert c._handler.calls == [("pointer", 100, 100)]
    c._own_heard_at -= HELPER_FILL_S * 2  # an admin app: our hook hears nothing
    c._helper_event(("pm", 2879, 400, False))
    assert c._handler.calls[-1] == ("pointer", 2879, 400)


def test_helper_events_from_before_a_switch_are_dropped():
    c = capture()
    c._own_heard_at = -1e9
    c._helper_event(("pm", 5, 5, True))   # seen while grabbing; input is here now
    c._helper_event(("pb", "left", True, True))
    assert c._handler.calls == []


def test_while_grabbed_helper_buttons_wheel_and_keys_go_to_the_other_computer():
    c = capture()
    c._grabbed = True
    c.loop_alive = lambda: True
    c._helper_event(("pb", "left", True, True))
    c._helper_event(("ps", 0.0, -1.0, True))
    keys = []
    c._key_for_vk = lambda vk, pressed: f"key{vk}"
    c._on_key = lambda key, pressed, injected: keys.append((key, pressed, injected))
    c._helper_event(("pk", 65, True, True))
    assert c._handler.calls == [("button", "left", True), ("scroll", 0.0, -1.0)]
    assert keys == [("key65", True, False)]
