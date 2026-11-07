"""The Windows input helper: the wire, the agent's side and the service's supervisor."""

import io

from curslink.winhelper import pipe_name, wire
from curslink.winhelper.client import RETRY_S, HelperSink
from curslink.winhelper.install import parse_state
from curslink.winhelper.service import BACKOFF_S, STABLE_S, Supervisor


class Sink:
    def __init__(self):
        self.calls = []

    def move_to(self, x, y):
        self.calls.append(("move", x, y))

    def button(self, b, p):
        self.calls.append(("button", b, p))

    def scroll(self, dx, dy):
        self.calls.append(("scroll", dx, dy))

    def key(self, k, p):
        self.calls.append(("key", k, p))


def test_wire_round_trip_and_rejects_junk():
    for fields, want in ((("m", 10, 20), ("m", 10, 20)), (("b", "x1", True), ("b", "x1", True)),
                         (("s", 0.5, -1), ("s", 0.5, -1.0)), (("k", "ctrl_l", False), ("k", "ctrl_l", False)),
                         (("hello", 1), ("hello", 1))):
        assert wire.decode(wire.encode(*fields)) == want
    for junk in (b"", b"not json", b"{}", b'["m", 1]', b'["b", "nose", true]', b'["k", "", true]',
                 b'["m", true, 2]', b'["rm", "-rf"]'):
        assert wire.decode(junk) is None
    sink = Sink()
    for line in (b'["m",1,2]', b'["b","left",true]', b'["s",0,1]', b'["k","a",false]'):
        wire.apply(sink, wire.decode(line))
    assert sink.calls == [("move", 1, 2), ("button", "left", True), ("scroll", 0.0, 1.0), ("key", "a", False)]


class Pipe(io.BytesIO):
    def __init__(self, fail_after=None):
        super().__init__()
        self.lines = []
        self.fail_after = fail_after

    def write(self, data):
        if self.fail_after is not None and len(self.lines) >= self.fail_after:
            raise OSError("pipe closed")
        self.lines.append(data)
        return len(data)


def test_agent_sends_through_the_helper_and_falls_back_without_it():
    t = [0.0]
    pipes = []
    up = {"on": False}

    def opener(path):
        assert path == pipe_name(3)
        if not up["on"]:
            raise OSError("not there")
        pipes.append(Pipe(fail_after=2))
        return pipes[-1], 1

    local = Sink()
    sink = HelperSink(local, 3, {"cmd": "ctrl"}, opener=opener, clock=lambda: t[0])
    sink.move_to(1, 1)                      # no helper: injected here
    up["on"] = True
    sink.move_to(2, 2)                      # too soon to look again: still here
    t[0] += RETRY_S
    sink.move_to(3, 3)                      # found it
    sink.key("cmd", True)                   # mapped before it's sent
    assert local.calls == [("move", 1, 1), ("move", 2, 2)]
    assert [wire.decode(x) for x in pipes[0].lines] == [("m", 3, 3), ("k", "ctrl", True)]
    sink.key("cmd", False)                  # the helper went away mid-way: injected here
    assert local.calls[-1] == ("key", "cmd", False) and not sink.connected


def test_supervisor_keeps_one_injector_in_the_console_session():
    t = [0.0]
    console = {"s": None}
    started, stopped, dead = [], [], set()

    def start(session, cmd):
        started.append(session)
        return f"p{len(started)}", 100 + len(started)

    sup = Supervisor("helper inject", lambda: console["s"], start, lambda p: p in dead, stopped.append,
                     clock=lambda: t[0])
    sup.tick()
    assert started == []                    # nobody at the console
    console["s"] = 1
    sup.tick()
    sup.tick()
    assert started == [1]
    console["s"] = 2                        # fast user switching
    sup.tick()
    assert stopped == ["p1"] and started == [1, 2]
    dead.add("p2")                          # it crashed straight away: back off
    t[0] += 1
    sup.tick()
    assert sup.child is None and started == [1, 2]
    t[0] += BACKOFF_S[1]
    sup.tick()
    assert started == [1, 2, 2]
    t[0] += STABLE_S                        # this one lasted: restarted at once when it stops
    dead.add("p3")
    sup.tick()
    assert started == [1, 2, 2, 2]
    sup.shutdown()
    assert stopped[-1] == "p4"


def test_service_state_from_sc_query():
    assert parse_state("SERVICE_NAME: CurslinkHelper\n        STATE              : 4  RUNNING\n") == "running"
    assert parse_state("        STATE              : 1  STOPPED\n") == "stopped"


def test_status_off_windows_is_missing(monkeypatch):
    import curslink.winhelper.install as inst

    monkeypatch.setattr(inst.sys, "platform", "linux")
    assert inst.status() == "missing"
