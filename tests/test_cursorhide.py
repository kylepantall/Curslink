from curslink import cursorhide
from curslink.cursorhide import Backend, CursorHider
from curslink.mirror import CursorPark
from curslink.net.client import AgentSession
from curslink.protocol import Message


class FakeBackend(Backend):
    ok = True

    def __init__(self):
        self.calls = []

    def hide(self):
        self.calls.append("hide")
        return True

    def show(self):
        self.calls.append("show")


class Loop:
    def __init__(self):
        self.pending = []

    def call_later(self, delay, fn):
        self.pending.append(fn)

    def run(self):
        pending, self.pending = self.pending, []
        for fn in pending:
            fn()


class Sink:
    def __init__(self):
        self.moves = []

    def move_to(self, x, y):
        self.moves.append((x, y))

    def button(self, *a): pass

    def scroll(self, *a): pass

    def key(self, *a): pass


def make(pos=(10.0, 10.0)):
    where = {"at": pos}
    backend, loop = FakeBackend(), Loop()
    hider = CursorHider(backend, lambda: where["at"], loop.call_later)
    return hider, backend, loop, where


def test_hides_once_and_shows_once():
    hider, backend, _loop, _ = make()
    hider.hide()
    hider.hide()
    assert hider.hidden
    hider.show()
    hider.show()
    assert backend.calls == ["hide", "show"]


def test_own_mouse_shows_it_again():
    hider, backend, loop, where = make()
    hider.hide()
    loop.run()
    assert hider.hidden  # nothing moved
    where["at"] = (11.0, 11.0)  # within the slop
    loop.run()
    assert hider.hidden
    where["at"] = (60.0, 10.0)  # someone used this computer's mouse
    loop.run()
    assert not hider.hidden and backend.calls == ["hide", "show"]
    assert loop.pending == []  # stops looking


def test_moves_of_our_own_are_not_the_mouse():
    hider, _backend, loop, where = make()
    hider.hide()
    where["at"] = (500.0, 500.0)
    hider.moved()
    loop.run()
    assert hider.hidden


def test_unsupported_platform_does_nothing():
    hider = CursorHider(Backend(), lambda: None, None)
    hider.hide()
    assert not hider.hidden and not hider.can_hide


def test_agent_hides_on_leave_and_shows_on_enter():
    sink = Sink()
    session = AgentSession(sink, lambda *a: None)
    hider, backend, _loop, _ = make()
    session.cursor = hider
    session.handle(Message("enter", {"x": 5, "y": 6}))
    assert not hider.hidden
    session.handle(Message("leave", {"x": 0, "y": 6}))
    assert hider.hidden
    session.handle(Message("enter", {"x": 7, "y": 8}))
    assert not hider.hidden and sink.moves[-1] == (7, 8)
    assert backend.calls == ["hide", "show"]


def test_mirror_park_leaves_a_hidden_cursor_alone():
    sink = Sink()
    session = AgentSession(sink, lambda *a: None)
    hider, _backend, _loop, _ = make()
    session.cursor = hider
    session.handle(Message("enter", {"x": 5, "y": 6}))
    session.handle(Message("leave", {}))
    timers = []
    park = CursorPark(session, lambda delay, fn: timers.append(fn))
    park.shows([{"id": "a", "screen_os": [0, 0, 100, 50]}])
    for fn in timers:
        fn()
    assert sink.moves == [(5, 6)]  # not moved aside: it's hidden anyway


def test_restore_windows_is_a_no_op_elsewhere(monkeypatch):
    monkeypatch.setattr(cursorhide.sys, "platform", "linux")
    cursorhide.restore_windows()


def test_leave_moves_the_cursor_aside_then_hides_it():
    sink = Sink()
    session = AgentSession(sink, lambda *a: None)
    hider, _backend, loop, _ = make()
    session.cursor = hider
    session.handle(Message("enter", {"x": 5, "y": 6}))
    session.handle(Message("leave", {"x": 0, "y": 0, "park": [1279, 400]}))
    assert sink.moves[-1] == (1279, 400) and hider.hidden


def test_no_park_while_a_drag_leaving_is_ending():
    class Busy:
        busy = True

        def on_button(self, *a): pass

    sink = Sink()
    session = AgentSession(sink, lambda *a: None)
    session.drag_out = Busy()
    session.handle(Message("enter", {"x": 5, "y": 6}))
    session.handle(Message("leave", {"park": [1279, 400]}))
    assert sink.moves == [(5, 6)]
