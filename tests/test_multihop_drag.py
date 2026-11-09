"""A held file drag going through several devices before it's let go of.

The layout is a row: the host ("mac") on the left, a PC agent in the middle
and a phone on the right. The phone takes files (the Android app lists
"files", "drop" and "file_ack") but can't hand a drag over itself. Whatever
route a drag takes, only the device it's let go of on gets the files, and
no file bytes move until then.
"""

import asyncio
import json
import os

import pytest

from conftest import FakeCapture, FakeOverlay, FakeSink, FakeTransport
from curslink.files import meta
from curslink.files.dragout import AgentDragOut
from curslink.files.handoff import DragHandoff
from curslink.files.transfer import FileReceiver
from curslink.gaze.zones import Target
from curslink.layout import Device, Layout, Screen
from curslink.net.client import AgentClient, AgentSession
from curslink.net.server import HostServer
from curslink.protocol import (DROP_FEATURE, FILE_ACK_FEATURE, FILES_FEATURE, FILES_OUT_FEATURE, decode,
                               encode)
from curslink.router import Router, RouterSettings
from curslink.security import client_context, server_context

SECRET = "correct horse battery staple"
PC_FEATURES = {FILES_FEATURE, DROP_FEATURE, FILE_ACK_FEATURE, FILES_OUT_FEATURE}
PHONE_FEATURES = {FILES_FEATURE, DROP_FEATURE, FILE_ACK_FEATURE}


def row() -> Layout:
    return Layout.from_devices([
        Device("mac", "Mac", (Screen("main", "mac", 0, 0, 1920, 1080),)),
        Device("pc", "PC", (Screen("main", "pc", 1920, 0, 1920, 1080),)),
        Device("phone", "Phone", (Screen("main", "phone", 3840, 0, 400, 860),)),
    ])


class Agents(FakeTransport):
    """Agents with their own features; bulk frames recorded per device."""

    def __init__(self, features: dict[str, set[str]]):
        super().__init__(features)
        self.features = features
        self.bulk = []

    def supports(self, device_id, feature):
        return device_id in self.connected and feature in self.features.get(device_id, ())

    def bulk_sender(self, device_id):
        async def send(t, **data):
            if device_id not in self.connected:
                raise ConnectionError(f"{device_id} disconnected")
            self.bulk.append((device_id, t, data))

        return send

    def to(self, device_id, t=None):
        return [d for dev, tt, d in self.sent if dev == device_id and (t is None or tt == t)]


@pytest.fixture
def desk(tmp_path):
    transport = Agents({"pc": PC_FEATURES, "phone": PHONE_FEATURES})
    router = Router(row(), "mac", FakeCapture(), FakeSink(), transport, FakeOverlay(),
                    RouterSettings(gaze_switching=True))
    helper, injected = [], []
    handoff = DragHandoff(router, transport, helper.append, injected.append, "Mac")
    handoff.receiver.folder = tmp_path / "Downloads"
    handoff.native.folder = tmp_path / "Downloads"
    handoff.native.root = tmp_path / "drops"
    return handoff, router, transport, helper, injected


async def settle():
    for _ in range(30):
        await asyncio.sleep(0.01)


def to_pc(router):
    router.on_pointer(1919, 500)          # over the Mac's right edge
    assert router.active_device == "pc"


def to_phone(router):
    router.on_motion(2000, 0)             # across the PC to its right edge
    router.on_motion(50, 0)
    assert router.active_device == "phone"


CARD = {"name": "photo.jpg", "kind": "JPG image", "size": 3, "count": 1, "ext": "JPG",
        "items": [{"name": "photo.jpg", "dir": False}]}


def frames(t, **d):
    return decode(encode(t, **d))


async def pc_sends(handoff, tid, data=b"abc"):
    """The PC answers the fetch: its file_* frames, as the host gets them."""
    import base64

    for msg in (frames("file_begin", id=tid, count=1, total=len(data), card=CARD, **{"from": "PC"}),
                frames("file_item", id=tid, i=0, path="photo.jpg", size=len(data)),
                frames("file_data", id=tid, i=0, data=base64.b64encode(data).decode()),
                frames("file_end", id=tid)):
        reply = handoff.on_files("pc", msg)
        if asyncio.iscoroutine(reply):
            await reply


# ------------------------------------------------- from the host, through the PC


async def test_a_mac_drag_goes_through_the_pc_and_drops_on_the_phone(desk, tmp_path):
    handoff, router, transport, helper, injected = desk
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF" * 1000)
    handoff.on_button("left", True)
    handoff.on_helper("drag " + json.dumps({"paths": [str(f)]}))
    await settle()
    to_pc(router)
    (on_pc,) = transport.to("pc", "drag")
    assert on_pc["card"]["name"] == "report.pdf"
    to_phone(router)
    assert transport.to("pc", "drag")[-1] == {"hide": True, "id": on_pc["id"]}     # the PC takes its card down
    (on_phone,) = transport.to("phone", "drag")
    assert on_phone["card"]["name"] == "report.pdf" and on_phone["id"] != on_pc["id"]
    await settle()
    assert transport.bulk == []                          # held all the way: nothing sent yet
    assert handoff.on_button("left", False) is True      # let go on the phone
    assert transport.to("phone", "drag")[-1] == {"drop": True, "id": on_phone["id"]}
    await settle()
    sent = [(dev, t) for dev, t, _ in transport.bulk]
    assert sent[0] == ("phone", "file_begin") and sent[-1] == ("phone", "file_end")
    assert {dev for dev, _ in sent} == {"phone"}         # the PC never got a byte
    assert transport.bulk[0][2]["ack"] is True           # the phone acks, so little is in flight
    assert injected == ["release"]                       # the Mac's own drag ends on the shield


async def test_a_mac_drag_can_wander_and_come_back_before_dropping(desk, tmp_path):
    handoff, router, transport, helper, injected = desk
    f = tmp_path / "a.txt"
    f.write_text("hi")
    handoff.on_button("left", True)
    handoff.on_helper("drag " + json.dumps({"paths": [str(f)]}))
    await settle()
    to_pc(router)
    to_phone(router)
    router.switch_to(router.layout.screen("pc", "main"), 3000, 500, "gaze")   # a look back at the PC
    assert transport.to("phone", "drag")[-1].get("hide") is True
    assert transport.to("pc", "drag")[-1]["card"]["name"] == "a.txt"
    assert handoff.on_button("left", False) is True      # dropped on the PC this time
    await settle()
    assert {dev for dev, _, _ in transport.bulk} == {"pc"}


# --------------------------------------------- from the PC, straight to the phone


def drag_on_pc(router, transport):
    to_pc(router)
    router.on_button("left", True)        # an Explorer drag starts on the PC
    assert transport.to("pc", "button")[-1] == {"button": "left", "pressed": True}


async def test_a_pc_drag_goes_straight_to_the_phone(desk):
    handoff, router, transport, helper, injected = desk
    drag_on_pc(router, transport)
    to_phone(router)
    assert transport.to("pc", "drag_out") == [{}]                 # asked, not let go of
    assert {"button": "left", "pressed": False} not in transport.to("pc", "button")
    handoff.on_drag_out("pc", {"id": "t1", "card": CARD})
    assert transport.to("phone", "drag") == [{"card": CARD, "id": "t1"}]
    assert not transport.to("pc", "fetch")                         # nothing moves while it's held
    assert handoff.on_button("left", False) is True
    assert transport.to("phone", "drag")[-1] == {"drop": True, "id": "t1"}
    assert transport.to("pc", "fetch") == [{"id": "t1"}]
    await pc_sends(handoff, "t1")
    passed = [(dev, t) for dev, t, _ in transport.bulk]
    assert passed == [("phone", "file_begin"), ("phone", "file_item"), ("phone", "file_data"), ("phone", "file_end")]
    handoff.on_result("phone", {"id": "t1", "ok": True, "saved": ["photo.jpg"], "folder": "Download/Curslink"})
    assert transport.to("pc", "file_result") == [{"id": "t1", "ok": True, "saved": ["photo.jpg"],
                                                  "folder": "Download/Curslink"}]
    assert handoff.relays == {}


async def test_the_answer_can_come_after_the_drop(desk):
    handoff, router, transport, helper, injected = desk
    drag_on_pc(router, transport)
    to_phone(router)
    assert handoff.on_button("left", False) is True                # let go before the PC said
    handoff.on_drag_out("pc", {"id": "t2", "card": CARD})
    assert transport.to("phone", "drag")[-1] == {"drop": True, "id": "t2"}
    assert transport.to("pc", "fetch") == [{"id": "t2"}]


async def test_gaze_can_carry_a_pc_drag_to_the_phone(desk):
    handoff, router, transport, helper, injected = desk
    drag_on_pc(router, transport)
    router._moved_at = -100.0                                       # the mouse has rested
    router.on_gaze(Target("phone", "main"))
    assert router.active_device == "phone"
    assert transport.to("pc", "drag_out") == [{}]


async def test_a_pc_drag_to_a_device_that_cant_take_files_is_let_go_of(tmp_path):
    transport = Agents({"pc": PC_FEATURES, "phone": set()})
    router = Router(row(), "mac", FakeCapture(), FakeSink(), transport, FakeOverlay())
    DragHandoff(router, transport, lambda line: None, lambda kind: None, "Mac")
    drag_on_pc(router, transport)
    to_phone(router)
    assert not transport.to("pc", "drag_out")
    assert transport.to("pc", "button")[-1] == {"button": "left", "pressed": False}


# ------------------------------------------------ from the PC, through the Mac


async def test_a_pc_drag_goes_through_the_mac_to_the_phone(desk):
    handoff, router, transport, helper, injected = desk
    drag_on_pc(router, transport)
    router.go_home("edge")                                          # over to the Mac
    handoff.on_drag_out("pc", {"id": "t3", "card": CARD})
    assert any(h.startswith("card ") for h in helper)               # the card follows the Mac's pointer
    router.switch_to(router.layout.screen("phone", "main"), 4000, 400, "gaze")   # a look at the phone
    assert helper[-1] == "card hide"                                # gone from the Mac
    assert transport.to("pc", "fetch") == []                        # still not fetched...
    assert transport.to("phone", "drag") == [{"card": CARD, "id": "t3"}]
    assert handoff.on_button("left", False) is True
    assert transport.to("pc", "fetch") == [{"id": "t3"}]            # ...until it's let go of on the phone
    assert transport.to("phone", "drag")[-1] == {"drop": True, "id": "t3"}
    await pc_sends(handoff, "t3")
    assert {dev for dev, _, _ in transport.bulk} == {"phone"}
    assert not (handoff.native.folder / "photo.jpg").exists()       # nothing kept on the Mac


async def test_a_pc_drag_through_the_phone_dropped_on_the_mac(desk):
    handoff, router, transport, helper, injected = desk
    drag_on_pc(router, transport)
    to_phone(router)
    handoff.on_drag_out("pc", {"id": "t4", "card": CARD})
    router.go_home("gaze")
    assert transport.to("phone", "drag")[-1] == {"hide": True, "id": "t4"}
    assert handoff.incoming is not None
    assert handoff.on_button("left", False) is True                 # dropped on the Mac
    assert transport.to("pc", "fetch") == [{"id": "t4"}]
    await pc_sends(handoff, "t4")
    assert (handoff.native.folder / "photo.jpg").read_bytes() == b"abc"
    assert transport.bulk == []


# --------------------------------------------------------------- going wrong


async def test_the_phone_going_away_mid_transfer_tells_the_pc(desk):
    handoff, router, transport, helper, injected = desk
    drag_on_pc(router, transport)
    to_phone(router)
    handoff.on_drag_out("pc", {"id": "t5", "card": CARD})
    handoff.on_button("left", False)
    transport.connected.discard("phone")
    handoff.on_disconnect("phone")
    assert transport.to("pc", "fetch")[-1] == {"id": "t5", "cancel": True}
    assert handoff.relays == {}
    await pc_sends(handoff, "t5")                                   # late frames go nowhere
    assert transport.bulk == []


async def test_a_stalled_phone_stops_the_relay(desk, monkeypatch):
    handoff, router, transport, helper, injected = desk
    monkeypatch.setattr("curslink.files.handoff.RELAY_STALL_S", 0.05)

    def stuck(device_id):
        async def send(t, **data):
            await asyncio.sleep(10)

        return send

    transport.bulk_sender = stuck
    drag_on_pc(router, transport)
    to_phone(router)
    handoff.on_drag_out("pc", {"id": "t6", "card": CARD})
    handoff.on_button("left", False)
    await handoff.on_files("pc", frames("file_begin", id="t6", count=1, total=3, card=CARD, **{"from": "PC"}))
    assert transport.to("pc", "fetch")[-1] == {"id": "t6", "cancel": True}
    assert transport.to("phone", "file_cancel")[0]["id"] == "t6"


async def test_a_new_press_forgets_a_drag_that_was_never_let_go_of(desk):
    handoff, router, transport, helper, injected = desk
    drag_on_pc(router, transport)
    to_phone(router)
    handoff.on_drag_out("pc", {"id": "t7", "card": CARD})
    assert handoff.on_button("left", True) is False                 # (its button-up was lost)
    assert transport.to("pc", "fetch") == [{"id": "t7", "cancel": True}]
    assert transport.to("phone", "drag")[-1] == {"hide": True, "id": "t7"}


async def test_frames_from_the_wrong_agent_are_not_passed_on(desk):
    handoff, router, transport, helper, injected = desk
    drag_on_pc(router, transport)
    to_phone(router)
    handoff.on_drag_out("pc", {"id": "t8", "card": CARD})
    handoff.on_button("left", False)
    assert handoff.on_files("phone", frames("file_begin", id="t8", count=1, total=3, card=CARD,
                                            **{"from": "x"})) is None
    assert transport.bulk == []


# ---------------------------------------------------- the real thing, over TLS


async def connect(server, device_id, session, setup=lambda client: None):
    client = AgentClient(device_id, device_id.title(), "127.0.0.1", server.port, client_context(SECRET), session)
    setup(client)  # before hello, which lists what the session can do
    task = asyncio.create_task(client.run_once())
    for _ in range(300):
        if server.is_connected(device_id):
            break
        await asyncio.sleep(0.01)
    assert server.is_connected(device_id)
    return client, task


async def test_end_to_end_pc_drag_dropped_on_a_phone_through_the_host(tmp_path, monkeypatch):
    """Three real links: the PC hands a held drag to the host, the phone
    shows it, and only when it's let go of there do the bytes go PC ->
    host -> phone."""
    import curslink.files.dragout as dragout

    monkeypatch.setattr(dragout, "PROBE_WAIT_S", 0.05)
    server = HostServer({"pc", "phone"}, server_context(SECRET), host="127.0.0.1", port=0)
    router = Router(row(), "mac", FakeCapture(), FakeSink(), server, FakeOverlay())
    handoff = DragHandoff(router, server, lambda line: None, lambda kind: None, "Mac")
    server.on_file_result = handoff.on_result
    server.on_drag_out = handoff.on_drag_out
    server.on_files = handoff.on_files
    await server.start()

    f = tmp_path / "holiday.mov"
    f.write_bytes(os.urandom(700_000))   # many frames, so the relay has to keep pace
    pc_session = AgentSession(FakeSink(), lambda *effect: None)
    pc_session.files = FileReceiver(tmp_path / "pc", lambda k, i: None)

    def hand_over(client):
        pc_session.drag_out = AgentDragOut(lambda line: None, client.send, client.send_bulk, "PC",
                                           nudge=lambda release: None, explorer=lambda: [str(f)])

    pc, pc_task = await connect(server, "pc", pc_session, hand_over)
    phone_cards = []
    phone_session = AgentSession(FakeSink(), lambda *effect: None)
    phone_session.files = FileReceiver(tmp_path / "phone", lambda k, i: None)
    phone_session.on_drag = phone_cards.append
    phone, phone_task = await connect(server, "phone", phone_session)
    try:
        assert server.supports("pc", FILES_OUT_FEATURE) and server.supports("phone", FILES_FEATURE)
        to_pc(router)
        router.on_button("left", True)
        await asyncio.sleep(0.05)
        to_phone(router)
        for _ in range(300):
            if phone_cards:
                break
            await asyncio.sleep(0.01)
        assert phone_cards[0]["card"]["name"] == "holiday.mov"
        assert not (tmp_path / "phone").exists()                    # held: nothing sent yet
        assert handoff.on_button("left", False) is True
        for _ in range(500):
            if (tmp_path / "phone" / "holiday.mov").exists() and pc_session.drag_out.pending is None:
                break
            await asyncio.sleep(0.01)
        assert (tmp_path / "phone" / "holiday.mov").read_bytes() == f.read_bytes()
        assert pc_session.drag_out.pending is None                  # the PC heard it was saved
        assert ("button", "left", False) not in pc_session.sink.calls   # never dropped on the PC
        assert not (tmp_path / "pc").exists()
    finally:
        await server.close()
        await asyncio.wait_for(asyncio.gather(pc_task, phone_task, return_exceptions=True), 3)


async def test_end_to_end_mac_drag_through_the_pc_to_the_phone(tmp_path):
    server = HostServer({"pc", "phone"}, server_context(SECRET), host="127.0.0.1", port=0)
    router = Router(row(), "mac", FakeCapture(), FakeSink(), server, FakeOverlay())
    injected = []
    handoff = DragHandoff(router, server, lambda line: None, injected.append, "Mac")
    server.on_file_result = handoff.on_result
    await server.start()
    f = tmp_path / "slides.key"
    f.write_bytes(os.urandom(300_000))
    seen = {"pc": [], "phone": []}
    sessions = {}
    tasks = []
    for name in ("pc", "phone"):
        s = AgentSession(FakeSink(), lambda *effect: None)
        s.files = FileReceiver(tmp_path / name, lambda k, i: None)
        s.on_drag = seen[name].append
        sessions[name] = s
        tasks.append((await connect(server, name, s))[1])
    try:
        handoff.on_button("left", True)
        handoff.items = meta.describe([str(f)])
        to_pc(router)
        to_phone(router)
        for _ in range(300):
            if any("card" in d for d in seen["phone"]):
                break
            await asyncio.sleep(0.01)
        cards = [d for d in seen["pc"] if "card" in d]
        assert cards[0]["card"]["name"] == "slides.key" and seen["pc"][-1].get("hide") is True
        assert handoff.on_button("left", False) is True
        for _ in range(500):
            if (tmp_path / "phone" / "slides.key").exists():
                break
            await asyncio.sleep(0.01)
        assert (tmp_path / "phone" / "slides.key").read_bytes() == f.read_bytes()
        assert not (tmp_path / "pc").exists()
    finally:
        await server.close()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)
