"""Carrying Isolink's floating chats between devices (curslink.isolink, curslink.popups)."""

import asyncio
import json

import pytest

from conftest import FakeCapture, FakeOverlay, FakeSink, FakeTransport
from curslink.gaze.zones import Target
from curslink.isolink import ChatDrag, IsolinkLink
from curslink.net.client import AgentSession
from curslink.popups import AgentPopups, PopupHandoff
from curslink.protocol import POPUPS_FEATURE, ProtocolError, decode, encode
from curslink.router import Router, RouterSettings


async def settle():
    for _ in range(20):
        await asyncio.sleep(0.01)


# ------------------------------------------------------------ Isolink Connect
class FakeIsolink:
    """Isolink's Connect socket: allows Curslink, answers windows.*."""

    def __init__(self, capabilities=("windows",), refuse=None):
        self.capabilities = list(capabilities)
        self.refuse = refuse
        self.hellos = []
        self.calls = []
        self.writers = []

    async def start(self):
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def _serve(self, reader, writer):
        self.writers.append(writer)
        while line := await reader.readline():
            f = json.loads(line)
            if f["m"] == "hello":
                self.hellos.append(f["p"])
                if self.refuse:
                    answer = {"id": f["id"], "e": {"code": self.refuse, "message": "no"}}
                else:
                    answer = {"id": f["id"], "r": {"protocol": 1, "mode": "host", "me": {"handle": "h_me", "me": True},
                                                   "capabilities": self.capabilities, "token": "secret"}}
            else:
                self.calls.append((f["m"], f["p"]))
                answer = {"id": f["id"], "r": True if f["m"] == "windows.open" else None}
            writer.write((json.dumps(answer) + "\n").encode())

    def event(self, ev, p):
        for w in self.writers:
            w.write((json.dumps({"ev": ev, "p": p}) + "\n").encode())

    async def close(self):
        for w in self.writers:
            w.close()
        self.server.close()


async def connected(link: IsolinkLink):
    link.start()
    for _ in range(200):
        if link.me is not None:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("never connected")


async def test_link_asks_for_windows_keeps_the_secret_and_hears_drags(tmp_path):
    isolink = FakeIsolink()
    port = await isolink.start()
    token = tmp_path / "isolink-token"
    link = IsolinkLink(token, port=port)
    changes = []
    link.on_change = lambda: changes.append(link.dragging)
    await connected(link)
    hello = isolink.hellos[0]
    assert hello["app"]["id"] == "io.github.orimovo.curslink" and hello["capabilities"] == ["windows"]
    assert "token" not in hello
    assert token.read_text() == "secret"
    assert link.ready and link.me == "h_me"

    isolink.event("windows.drag", {"chat": "h_chat", "held": True, "open": True, "width": 360, "grabX": 12})
    await settle()
    assert link.dragging == ChatDrag("h_chat", True, 360.0, 0.0, 12.0, 0.0)
    isolink.event("windows.drag", {"chat": "h_chat", "held": False})
    await settle()
    assert link.dragging is None

    assert await link.open_chat("h_chat", held=True, open_=True) is True
    await link.close_chat("h_chat")
    assert isolink.calls == [("windows.open", {"chat": "h_chat", "held": True, "open": True}),
                             ("windows.close", {"chat": "h_chat"})]

    isolink.event("capabilities.changed", {"capabilities": []})
    await settle()
    assert not link.ready
    await link.stop()
    await isolink.close()

    # Next time it shows the secret instead of being asked again.
    again = FakeIsolink()
    link = IsolinkLink(token, port=await again.start())
    await connected(link)
    assert again.hellos[0]["token"] == "secret"
    await link.stop()
    await again.close()


async def test_link_stops_trying_once_refused(tmp_path):
    isolink = FakeIsolink(refuse="denied")
    link = IsolinkLink(tmp_path / "t", port=await isolink.start())
    link.start()
    await settle()
    assert link._task.done() and link.me is None
    assert len(isolink.hellos) == 1
    await isolink.close()


async def test_link_waits_quietly_for_isolink(tmp_path):
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    link = IsolinkLink(tmp_path / "t", port=port)
    link.start()
    await settle()
    assert link.me is None and not link._task.done()
    await link.stop()


# ------------------------------------------------------------------- host side
class FakeLink(IsolinkLink):
    def __init__(self, me="h_me", windows=True):
        super().__init__(None)
        self.me, self.windows = me, windows
        self.opened, self.closed = [], []

    async def open_chat(self, chat, held, open_):
        self.opened.append((chat, held, open_))
        return True

    async def close_chat(self, chat):
        self.closed.append(chat)


class PopupsTransport(FakeTransport):
    def supports(self, device_id, feature):
        return device_id in self.connected and feature == POPUPS_FEATURE


@pytest.fixture
def popups(layout):
    transport = PopupsTransport({"laptop"})
    router = Router(layout, "desktop", FakeCapture(), FakeSink(), transport, FakeOverlay(),
                    RouterSettings(gaze_switching=True))
    link, injected, pressed = FakeLink(), [], []
    handoff = PopupHandoff(router, transport, link, injected.append, lambda: pressed.append(True))
    return handoff, router, transport, link, injected, pressed


async def test_a_chat_dragged_over_the_edge_opens_on_the_laptop(popups):
    handoff, router, transport, link, injected, _ = popups
    handoff.on_isolink("laptop", {"me": "h_me", "windows": True, "dragging": None})
    link.dragging = ChatDrag("h_chat", open=True)
    router.on_pointer(3839, 500)
    assert router.active_device == "laptop"
    await settle()
    assert injected == ["let_go"]
    (dev, _, data), = transport.of_type("popup_in")
    assert dev == "laptop" and data == {"chat": "h_chat", "open": True}
    assert link.closed == []  # not until it opened there
    handoff.on_result("laptop", {"chat": "h_chat", "ok": True})
    await settle()
    assert link.closed == ["h_chat"]
    # The laptop's press is let go of with the real release.
    router.on_button("left", False)
    assert transport.of_type("button")[-1][2] == {"button": "left", "pressed": False}


async def test_a_chat_stays_when_the_other_isolink_is_another_account(popups):
    handoff, router, transport, link, injected, _ = popups
    handoff.on_isolink("laptop", {"me": "h_someone_else", "windows": True})
    link.dragging = ChatDrag("h_chat", open=False)
    router.on_pointer(3839, 500)
    await settle()
    assert router.active_device == "laptop"
    assert not transport.of_type("popup_in") and injected == []


async def test_a_chat_stays_when_the_laptop_has_no_isolink(popups):
    handoff, router, transport, link, injected, _ = popups
    link.dragging = ChatDrag("h_chat", open=False)
    router.on_pointer(3839, 500)
    await settle()
    assert not transport.of_type("popup_in") and injected == []


async def test_a_chat_dragged_on_the_laptop_comes_home(popups):
    handoff, router, transport, link, injected, pressed = popups
    router.can_drag_out = lambda device_id: True  # the laptop takes file drags too
    handoff_files = []
    router.drag_out = handoff_files.append
    PopupHandoff.__init__(handoff, router, transport, link, injected.append, lambda: pressed.append(True))
    router.on_pointer(3839, 500)
    router.on_button("left", True)
    handoff.on_isolink("laptop", {"me": "h_me", "windows": True,
                                  "dragging": {"chat": "h_chat", "open": True}})
    router.go_home("edge")
    await settle()
    assert handoff_files == [], "a chat isn't a file: the laptop lets go as usual"
    assert transport.of_type("button")[-1][2] == {"button": "left", "pressed": False}
    assert pressed == [True] and link.opened == [("h_chat", True, True)]
    (dev, _, data), = transport.of_type("popup_close")
    assert dev == "laptop" and data == {"chat": "h_chat"}


async def test_looking_away_mid_drag_carries_the_chat(popups):
    handoff, router, transport, link, injected, pressed = popups
    router.clock = lambda: 1000.0
    router.on_pointer(3839, 500)
    router.on_button("left", True)
    handoff.on_isolink("laptop", {"me": "h_me", "windows": True, "dragging": {"chat": "h_chat"}})
    router._moved_at = 0
    router.on_gaze(Target("desktop", "left"))
    await settle()
    assert router.active_device == "desktop"
    assert link.opened == [("h_chat", True, False)]


# ------------------------------------------------------------------ agent side
async def test_agent_opens_a_carried_chat_held_and_says_so():
    link, sent, sink = FakeLink(), [], FakeSink()
    session = AgentSession(sink, lambda *a: None)

    def press():
        session.buttons.add("left")
        sink.button("left", True)

    session.popups = AgentPopups(link, lambda t, **d: sent.append((t, d)), press)
    session.popups.report()
    assert sent[-1] == ("isolink", {"me": "h_me", "windows": True, "dragging": None})
    session.handle(decode(encode("popup_in", chat="h_chat", open=True)))
    await settle()
    assert sink.calls == [("button", "left", True)]
    assert link.opened == [("h_chat", True, True)]
    assert sent[-1] == ("popup_result", {"chat": "h_chat", "ok": True})
    # The host's button-up lets go of it.
    session.handle(decode(encode("button", button="left", pressed=False)))
    assert sink.calls[-1] == ("button", "left", False)
    session.handle(decode(encode("popup_close", chat="h_other")))
    await settle()
    assert link.closed == ["h_other"]


def test_popup_frames_are_checked():
    decode(encode("isolink", me=None, windows=False, dragging=None))
    with pytest.raises(ProtocolError):
        decode(b'{"t":"popup_in","chat":""}')
    with pytest.raises(ProtocolError):
        decode(b'{"t":"isolink","me":3}')
