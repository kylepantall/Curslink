"""Dragging files to another device: description, transfer, the handoff."""

import asyncio
import json
import os

import pytest

from conftest import FakeCapture, FakeOverlay, FakeSink, FakeTransport
from curslink.files import meta
from curslink.files.handoff import DragHandoff, edge_strips
from curslink.files.transfer import FileReceiver, Outgoing, TransferError, safe_parts, send_files, unique
from curslink.net.client import AgentClient, AgentSession
from curslink.net.server import HostServer
from curslink.protocol import FILES_FEATURE, decode, encode
from curslink.router import Router, RouterSettings
from curslink.security import client_context, server_context

SECRET = "correct horse battery staple"


# ------------------------------------------------------------------- meta


def test_kind_and_size_read_like_a_file_manager():
    assert meta.kind_of("report.pdf") == "PDF document"
    assert meta.kind_of("photo.PNG") == "PNG image"
    assert meta.kind_of("notes") == "File"
    assert meta.kind_of("stuff", is_dir=True) == "Folder"
    assert meta.human_size(1) == "1 byte"
    assert meta.human_size(999) == "999 bytes"
    assert meta.human_size(1536) == "1.5 KB"
    assert meta.human_size(2_400_000) == "2.4 MB"
    assert meta.human_size(250_000_000) == "250 MB"


def test_card_describes_the_first_item_and_counts_the_rest(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"x" * 1500)
    folder = tmp_path / "pics"
    folder.mkdir()
    (folder / "one.jpg").write_bytes(b"y" * 500)
    items = meta.describe([str(tmp_path / "a.pdf"), str(folder), str(tmp_path / "missing")])
    assert [i.name for i in items] == ["a.pdf", "pics"]
    assert items[1].is_dir and items[1].size == 500
    card = meta.card(items)
    assert card["name"] == "a.pdf" and card["ext"] == "PDF" and card["count"] == 2
    assert card["size"] == 2000 and card["size_text"] == "2.0 KB"


# -------------------------------------------------------------- transfer


def test_paths_from_the_sender_are_made_safe():
    assert safe_parts("pics/one.jpg") == ["pics", "one.jpg"]
    assert safe_parts('a:b?.txt') == ["a_b_.txt"]
    assert safe_parts("CON.txt") == ["_CON.txt"]
    for bad in ("../evil", "/etc/passwd", "a/../../b", "a//b", "", "a\\b"):
        with pytest.raises(TransferError):
            safe_parts(bad)


def test_unique_never_overwrites(tmp_path):
    (tmp_path / "report.pdf").write_text("old")
    (tmp_path / "report (1).pdf").write_text("old")
    assert unique(tmp_path, "report.pdf").name == "report (2).pdf"
    assert unique(tmp_path, "new.txt").name == "new.txt"


def loopback(receiver: FileReceiver, replies: list):
    """A send() that hands each frame straight to the receiver, through the codec."""

    async def send(t, **data):
        reply = receiver.handle(decode(encode(t, **data)))
        if reply:
            replies.append(decode(reply))

    return send


async def test_files_and_folders_arrive_whole(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "pics" / "sub").mkdir(parents=True)
    big = os.urandom(100_000)
    (src / "big.bin").write_bytes(big)
    (src / "pics" / "a.jpg").write_bytes(b"a")
    (src / "pics" / "sub" / "b.txt").write_bytes(b"")
    dst.mkdir()
    (dst / "big.bin").write_bytes(b"already here")
    events, replies = [], []
    receiver = FileReceiver(dst, lambda kind, info: events.append((kind, info)))
    out = Outgoing("t1", "laptop", meta.describe([str(src / "big.bin"), str(src / "pics")]))
    await send_files(loopback(receiver, replies), out, "Desktop")
    assert (dst / "big (1).bin").read_bytes() == big
    assert (dst / "big.bin").read_bytes() == b"already here"
    assert (dst / "pics" / "a.jpg").read_bytes() == b"a"
    assert (dst / "pics" / "sub" / "b.txt").read_bytes() == b""
    assert not any(p.name.startswith(".curslink-") for p in dst.iterdir())
    assert replies[-1].t == "file_result" and replies[-1].get("ok") is True
    assert replies[-1].get("saved") == ["big (1).bin", "pics"]
    kinds = [k for k, _ in events]
    assert kinds[0] == "start" and kinds[-1] == "done" and "progress" in kinds
    assert events[0][1]["from"] == "Desktop" and events[0][1]["count"] == 3


async def test_a_cancelled_transfer_leaves_nothing_behind(tmp_path):
    dst = tmp_path / "dst"
    events, replies = [], []
    receiver = FileReceiver(dst, lambda kind, info: events.append(kind))
    send = loopback(receiver, replies)
    await send("file_begin", id="t2", count=1, total=10, card={}, **{"from": "Desktop"})
    await send("file_item", id="t2", i=0, path="half.bin", size=10)
    await send("file_data", id="t2", i=0, data="AAAA")
    await send("file_cancel", id="t2", reason="gone")
    assert list(dst.iterdir()) == []
    assert events[-1] == "failed" and not replies


async def test_lying_sender_is_refused(tmp_path):
    receiver = FileReceiver(tmp_path, lambda kind, info: None)
    replies = []
    send = loopback(receiver, replies)
    await send("file_begin", id="t3", count=1, total=2, card={}, **{"from": "x"})
    await send("file_item", id="t3", i=0, path="../escape", size=2)
    assert replies[-1].get("ok") is False
    assert not (tmp_path.parent / "escape").exists()
    assert list(tmp_path.iterdir()) == []


async def test_sender_stops_when_the_receiver_refuses(tmp_path):
    (tmp_path / "f").write_bytes(b"z" * 200_000)
    out = Outgoing("t4", "laptop", meta.describe([str(tmp_path / "f")]))
    sent = []

    async def send(t, **data):
        sent.append(t)
        if t == "file_data":
            out.refused = "disk full"

    with pytest.raises(TransferError):
        await send_files(send, out, "Desktop")
    assert sent.count("file_data") == 1 and "file_end" not in sent


async def test_end_to_end_over_tls_with_input_in_between(tmp_path):
    results = []
    server = HostServer({"laptop"}, server_context(SECRET), host="127.0.0.1", port=0)
    server.on_file_result = lambda device, data: results.append((device, data))
    await server.start()
    sink = FakeSink()
    session = AgentSession(sink, lambda e, x, y: None)
    session.files = FileReceiver(tmp_path / "in", lambda k, i: None)
    client = AgentClient("laptop", "Laptop", "127.0.0.1", server.port, client_context(SECRET), session)
    task = asyncio.create_task(client.run_once())
    for _ in range(300):
        if server.is_connected("laptop"):
            break
        await asyncio.sleep(0.01)
    assert server.supports("laptop", FILES_FEATURE)
    payload = os.urandom(1_000_000)
    (tmp_path / "movie.mp4").write_bytes(payload)
    out = Outgoing("t5", "laptop", meta.describe([str(tmp_path / "movie.mp4")]))
    sending = asyncio.create_task(send_files(server.bulk_sender("laptop"), out, "Desktop"))
    await asyncio.sleep(0)
    server.send("laptop", "move", x=5, y=6)
    await sending
    for _ in range(300):
        if results:
            break
        await asyncio.sleep(0.01)
    assert results and results[0][1]["ok"] is True
    assert (tmp_path / "in" / "movie.mp4").read_bytes() == payload
    assert ("move", 5, 6) in sink.calls
    await server.close()
    await asyncio.wait_for(task, 3)


# --------------------------------------------------------------- handoff


class FilesTransport(FakeTransport):
    def __init__(self, connected=()):
        super().__init__(connected)
        self.bulk = []

    def supports(self, device_id, feature):
        return device_id in self.connected and feature == FILES_FEATURE

    def bulk_sender(self, device_id):
        async def send(t, **data):
            self.bulk.append((device_id, t, data))

        return send


@pytest.fixture
def drag(layout):
    capture, transport = FakeCapture(), FilesTransport({"laptop"})
    router = Router(layout, "desktop", capture, FakeSink(), transport, FakeOverlay(),
                    RouterSettings(gaze_switching=True))
    helper, injected = [], []
    handoff = DragHandoff(router, transport, helper.append, injected.append, "Desktop")
    return handoff, router, transport, helper, injected


def test_strips_go_only_where_an_edge_leads_somewhere(layout):
    strips = edge_strips(layout, "desktop", 64)
    sides = {(s["x"], s["side"]) for s in strips}
    assert (1920, "right") in sides           # the laptop is to the right
    assert (0, "top") in sides                # the tablet is above the left monitor
    assert (0, "left") not in sides and (1920, "left") not in sides


async def settle():
    for _ in range(50):
        await asyncio.sleep(0.01)


async def test_drag_over_the_edge_carries_and_drops_the_file(drag, tmp_path):
    handoff, router, transport, helper, injected = drag
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF" * 100)
    handoff.on_button("left", True)
    handoff.on_pointer(3000, 500)
    handoff.on_pointer(3830, 500)
    assert helper[-1].startswith("strips ")
    handoff.on_helper("drag " + json.dumps({"paths": [str(f)]}))
    await settle()
    router.on_pointer(3839, 500)
    assert router.active_device == "laptop"
    (dev, _, data), = transport.of_type("drag")
    assert dev == "laptop" and data["card"]["name"] == "report.pdf" and data["card"]["kind"] == "PDF document"
    assert "strips off" in helper and helper[-1] == "shield on"
    # let go on the laptop: the drop isn't forwarded as a click, the file is sent
    assert handoff.on_button("left", False) is True
    await settle()
    assert transport.of_type("drag")[-1][2] == {"hide": True}
    sent = [t for _, t, _ in transport.bulk]
    assert sent[0] == "file_begin" and sent[-1] == "file_end"
    assert injected == ["release"]


async def test_late_report_from_the_strip_still_carries(drag, tmp_path):
    handoff, router, transport, helper, injected = drag
    f = tmp_path / "a.txt"
    f.write_text("hi")
    handoff.on_button("left", True)
    router.on_pointer(3839, 500)        # crossed before the helper spoke
    assert not transport.of_type("drag")
    handoff.on_helper("drag " + json.dumps({"paths": [str(f)]}))
    await settle()
    assert transport.of_type("drag")[0][2]["card"]["name"] == "a.txt"


async def test_looking_away_mid_drag_probes_for_a_file(drag, tmp_path):
    handoff, router, transport, helper, injected = drag
    f = tmp_path / "a.txt"
    f.write_text("hi")
    handoff.on_button("left", True)
    router.switch_to(router.layout.screen("laptop", "main"), 4000, 500, "gaze")
    assert helper[-1] == "shield on"
    await settle()
    assert injected == ["nudge"]
    handoff.on_helper("drag " + json.dumps({"paths": [str(f)]}))
    await settle()
    assert transport.of_type("drag")[0][2]["card"]["name"] == "a.txt"


async def test_coming_home_with_the_drag_hands_it_back(drag, tmp_path):
    handoff, router, transport, helper, injected = drag
    f = tmp_path / "a.txt"
    f.write_text("hi")
    handoff.on_button("left", True)
    handoff.on_helper("drag " + json.dumps({"paths": [str(f)]}))
    await settle()
    router.on_pointer(3839, 500)
    router.go_home("gaze")
    assert helper[-1] == "shield off" and handoff.carrying is None
    assert handoff.on_button("left", False) is False   # an ordinary local drop
    assert not transport.bulk and not injected


async def test_plain_clicks_and_non_file_drags_change_nothing(drag):
    handoff, router, transport, helper, injected = drag
    handoff.on_button("left", True)
    handoff.on_button("left", False)
    assert helper == []
    handoff.on_button("left", True)
    handoff.on_pointer(100, 100)
    handoff.on_pointer(140, 100)
    handoff.on_helper('drag {"paths": []}')
    router.on_pointer(3839, 500)
    await settle()
    assert not transport.of_type("drag")
    router.go_home("hotkey")
    assert handoff.on_button("left", False) is False


async def test_agent_without_files_gets_no_card(layout, tmp_path):
    transport = FilesTransport({"laptop"})
    transport.supports = lambda device_id, feature: False
    router = Router(layout, "desktop", FakeCapture(), FakeSink(), transport, FakeOverlay())
    handoff = DragHandoff(router, transport, lambda line: None, lambda kind: None, "Desktop")
    (tmp_path / "a").write_text("x")
    handoff.on_button("left", True)
    handoff.on_helper("drag " + json.dumps({"paths": [str(tmp_path / "a")]}))
    await settle()
    router.on_pointer(3839, 500)
    assert not transport.of_type("drag")
    assert handoff.on_button("left", False) is False


# ------------------------------------------------------- the helper's windows


def test_helper_windows(capsys, tmp_path):
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtCore import QMimeData, QPoint, Qt, QUrl
    from PySide6.QtGui import QDragEnterEvent, QDropEvent

    from curslink.files.overlay import FilesOverlay, card_lines, toast_lines

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    files = FilesOverlay(lambda x, y: (x, y))
    g = app.primaryScreen().geometry()
    assert files.handle("strips " + json.dumps([{"x": g.x(), "y": g.y(), "w": g.width(), "h": g.height(),
                                                 "side": "right"}]))
    (strip,) = files.strips
    assert strip.geometry().width() == 3 and strip.geometry().right() == g.right()
    assert strip.acceptDrops()

    mime = QMimeData()
    path = str(tmp_path / "report.pdf")
    mime.setUrls([QUrl.fromLocalFile(path)])
    enter = QDragEnterEvent(QPoint(1, 1), Qt.DropAction.CopyAction | Qt.DropAction.MoveAction, mime,
                            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    strip.dragEnterEvent(enter)
    assert enter.isAccepted() and enter.dropAction() == Qt.DropAction.CopyAction
    line = capsys.readouterr().out.strip().splitlines()[-1]
    assert line.startswith("drag ") and json.loads(line[5:]) == {"paths": [path]}
    drop = QDropEvent(QPoint(1, 1), Qt.DropAction.CopyAction | Qt.DropAction.MoveAction, mime,
                      Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    strip.dropEvent(drop)
    assert not drop.isAccepted()  # the source app must do nothing with it

    files.handle("strips off")
    assert files.strips == []
    files.handle("shield on")
    assert len(files.shield) == len(app.screens())
    files.handle("shield off")
    assert files.shield == []

    info = {"name": "report.pdf", "kind": "PDF document", "size": 2_400_000, "count": 1, "ext": "PDF"}
    files.handle("card " + json.dumps(info))
    assert files.card is not None and files.card.isVisible()
    assert card_lines(info) == ("report.pdf", "PDF document · 2.4 MB")
    assert card_lines({**info, "count": 3}) == ("report.pdf and 2 more", "3 items · 2.4 MB")
    files.handle("card hide")
    assert files.card is None

    toast = {"id": "t", "state": "receiving", "from": "Desktop", "count": 1, "total": 1000, "received": 250,
             "card": info}
    files.handle("toast " + json.dumps(toast))
    first = files.toast
    files.handle("toast " + json.dumps({**toast, "received": 500}))
    assert files.toast is first
    assert toast_lines(toast)[0] == "Receiving report.pdf" and toast_lines(toast)[2] == 0.25
    done = toast_lines({**toast, "state": "done", "folder": "/Users/k/Downloads"})
    assert done[:2] == ("Saved report.pdf", "In Downloads · 1.0 KB")
    first.close()


def test_config_switches():
    from curslink.config import agent_config, host_config

    agent = agent_config({"agent": {"device_id": "laptop", "files": {"folder": "~/Inbox"}}})
    assert agent.files.enabled and agent.files.folder.name == "Inbox" and "~" not in str(agent.files.folder)
    off = agent_config({"agent": {"device_id": "laptop", "files": {"enabled": False}}})
    assert not off.files.enabled
    raw = {"host": {"device_id": "d"}, "devices": [{"id": "d", "name": "D", "screens": [
        {"id": "s", "x": 0, "y": 0, "width": 10, "height": 10}]}]}
    assert host_config(raw).files is True
    assert host_config({**raw, "files": {"enabled": False}}).files is False


# ------------------------------------------------- macOS: the drag pasteboard


class Pasteboard:
    def __init__(self):
        self.count, self.paths = 5, ["/old/stale.txt"]

    def __call__(self):
        return self.count, list(self.paths)


@pytest.fixture
def mac_drag(layout):
    capture, transport = FakeCapture(), FilesTransport({"laptop"})
    router = Router(layout, "desktop", capture, FakeSink(), transport, FakeOverlay(),
                    RouterSettings(gaze_switching=True))
    helper, injected, pb = [], [], Pasteboard()
    handoff = DragHandoff(router, transport, helper.append, injected.append, "Mac", drag_source=pb)
    return handoff, router, transport, helper, injected, pb


async def test_mac_reads_the_drag_from_the_pasteboard(mac_drag, tmp_path):
    handoff, router, transport, helper, injected, pb = mac_drag
    f = tmp_path / "report.pdf"
    f.write_bytes(b"x" * 10)
    handoff.on_button("left", True)
    handoff.on_pointer(3000, 500)
    handoff.on_pointer(3830, 500)
    assert not any(h.startswith("strips") for h in helper)  # no drop-target windows on a Mac
    pb.count, pb.paths = 6, [str(f)]                        # Finder started a drag
    router.on_pointer(3839, 500)
    await settle()
    (dev, _, data), = transport.of_type("drag")
    assert dev == "laptop" and data["card"]["name"] == "report.pdf"
    assert handoff.on_button("left", False) is True
    await settle()
    assert injected == ["release"] and transport.bulk[0][1] == "file_begin"


async def test_mac_ignores_a_stale_pasteboard(mac_drag):
    handoff, router, transport, helper, injected, pb = mac_drag
    handoff.on_button("left", True)                         # e.g. dragging a window
    router.on_pointer(3839, 500)
    await asyncio.sleep(0.3)
    assert not transport.of_type("drag") and not injected
    router.go_home("hotkey")
    assert handoff.on_button("left", False) is False


async def test_mac_gaze_needs_no_nudge(mac_drag, tmp_path):
    handoff, router, transport, helper, injected, pb = mac_drag
    f = tmp_path / "a.txt"
    f.write_text("hi")
    handoff.on_button("left", True)
    pb.count, pb.paths = 9, [str(f)]
    router.switch_to(router.layout.screen("laptop", "main"), 4000, 500, "gaze")
    await settle()
    assert injected == []
    assert transport.of_type("drag")[0][2]["card"]["name"] == "a.txt"


# ------------------------------------------- files the other way, and real drops

from curslink.files.dragout import AgentDragOut  # noqa: E402
from curslink.files.native import NativeDrop  # noqa: E402
from curslink.files.popups import ReceivePopups  # noqa: E402
from curslink.protocol import DROP_FEATURE, FILES_OUT_FEATURE  # noqa: E402


class NewAgents(FilesTransport):
    """Agents on this version: take prefetched drops and hand drags back."""

    def supports(self, device_id, feature):
        return device_id in self.connected and feature in (FILES_FEATURE, DROP_FEATURE, FILES_OUT_FEATURE)


@pytest.fixture
def new_drag(layout, tmp_path):
    capture, transport = FakeCapture(), NewAgents({"laptop"})
    router = Router(layout, "desktop", capture, FakeSink(), transport, FakeOverlay(),
                    RouterSettings(gaze_switching=True))
    helper, injected = [], []
    handoff = DragHandoff(router, transport, helper.append, injected.append, "Desktop")
    handoff.receiver.folder = tmp_path / "Downloads"
    handoff.native.folder = tmp_path / "Downloads"
    handoff.native.root = tmp_path / "drops"
    return handoff, router, transport, helper, injected


def test_router_hands_a_drag_back_instead_of_letting_go(layout):
    transport = FakeTransport({"laptop"})
    router = Router(layout, "desktop", FakeCapture(), FakeSink(), transport, FakeOverlay(),
                    RouterSettings(gaze_switching=True))
    asked = []
    router.can_drag_out = lambda device_id: True
    router.drag_out = asked.append
    router.on_pointer(3839, 500)
    router.on_button("left", True)
    router.go_home("edge")
    assert asked == ["laptop"]
    assert not [s for s in transport.of_type("button") if s[2]["pressed"] is False]


def test_router_still_lets_go_for_agents_that_cant(layout):
    transport = FakeTransport({"laptop"})
    router = Router(layout, "desktop", FakeCapture(), FakeSink(), transport, FakeOverlay())
    router.on_pointer(3839, 500)
    router.on_button("left", True)
    router.go_home("edge")
    assert transport.of_type("button")[-1][2] == {"button": "left", "pressed": False}


async def test_files_start_sending_when_the_drag_crosses(new_drag, tmp_path):
    handoff, router, transport, helper, injected = new_drag
    f = tmp_path / "big.bin"
    f.write_bytes(b"x" * 100)
    handoff.on_button("left", True)
    handoff.on_helper("drag " + json.dumps({"paths": [str(f)]}))
    await settle()
    router.on_pointer(3839, 500)
    await settle()
    (_, _, data), = transport.of_type("drag")
    tid = data["id"]
    assert data["card"]["items"] == [{"name": "big.bin", "dir": False}]
    assert [t for _, t, _ in transport.bulk][0] == "file_begin"   # before any drop
    assert handoff.on_button("left", False) is True
    assert transport.of_type("drag")[-1][2] == {"drop": True, "id": tid}
    await settle()
    assert [t for _, t, _ in transport.bulk].count("file_begin") == 1  # not sent twice
    assert injected == ["release"]


async def test_carrying_it_home_stops_the_transfer(new_drag, tmp_path):
    handoff, router, transport, helper, injected = new_drag
    f = tmp_path / "a.txt"
    f.write_text("hi")
    handoff.on_button("left", True)
    handoff.on_helper("drag " + json.dumps({"paths": [str(f)]}))
    await settle()
    router.on_pointer(3839, 500)
    tid = transport.of_type("drag")[0][2]["id"]
    router.go_home("hotkey")
    assert transport.of_type("drag")[-1][2] == {"hide": True, "id": tid}
    assert handoff.carry_out is None


async def test_a_file_dragged_home_from_an_agent(new_drag, tmp_path):
    handoff, router, transport, helper, injected = new_drag
    router.on_pointer(3839, 500)
    router.on_button("left", True)       # Explorer drag starts on the laptop
    router.go_home("edge")               # ...and comes over the edge
    assert transport.of_type("drag_out")
    card = {"name": "photo.jpg", "kind": "JPG image", "size": 3, "count": 1, "ext": "JPG",
            "items": [{"name": "photo.jpg", "dir": False}]}
    handoff.on_drag_out("laptop", {"id": "t9", "card": card})
    assert transport.of_type("fetch")[-1][2] == {"id": "t9"}          # fetched straight away
    assert any(h.startswith("card ") for h in helper)                  # no real drags here: the card
    assert handoff.on_button("left", False) is True                    # dropped here
    send = lambda t, **d: handoff.on_files("laptop", decode(encode(t, **d)))  # noqa: E731
    send("file_begin", id="t9", count=1, total=3, card=card, **{"from": "Laptop"})
    send("file_item", id="t9", i=0, path="photo.jpg", size=3)
    send("file_data", id="t9", i=0, data="YWJj")
    reply = decode(send("file_end", id="t9"))
    assert reply.get("ok") is True
    assert (tmp_path / "Downloads" / "photo.jpg").read_bytes() == b"abc"
    assert not (tmp_path / "drops" / "t9").exists()


async def test_agent_frames_nobody_asked_for_are_ignored(new_drag):
    handoff, *_ = new_drag
    assert handoff.on_files("laptop", decode(encode("file_begin", id="zz", count=1, total=1, card={},
                                                    **{"from": "x"}))) is None


async def test_not_a_file_and_carrying_on_cancel(new_drag):
    handoff, router, transport, helper, injected = new_drag
    router.on_pointer(3839, 500)
    router.on_button("left", True)
    router.go_home("edge")
    handoff.on_drag_out("laptop", {"none": True})
    assert handoff.incoming is None and not transport.of_type("fetch")
    assert handoff.on_button("left", False) is False


# ---------------------------------------------------------- the real drop


def native(tmp_path, press=True):
    helper, calls = [], []
    popups = ReceivePopups(helper.append)
    receiver = FileReceiver(tmp_path / "Downloads", popups)
    drop = NativeDrop(helper.append, receiver, popups,
                      press=(lambda: calls.append("press")) if press else None,
                      release=lambda: calls.append("release"), cancel=lambda: calls.append("cancel"),
                      root=tmp_path / "drops")
    return drop, receiver, helper, calls


CARD = {"name": "movie.mp4", "size": 4, "count": 2, "items": [{"name": "movie.mp4", "dir": False},
                                                              {"name": "pics", "dir": True}]}


def feed(receiver, tid, done=True):
    replies = []
    for t, d in (("file_begin", dict(id=tid, count=2, total=4, card=CARD, **{"from": "Mac"})),
                 ("file_item", dict(id=tid, i=0, path="movie.mp4", size=2)),
                 ("file_data", dict(id=tid, i=0, data="aGk=")),
                 ("file_item", dict(id=tid, i=1, path="pics/a.png", size=2)),
                 ("file_data", dict(id=tid, i=1, data="b2s="))) + ((("file_end", dict(id=tid)),) if done else ()):
        r = receiver.handle(decode(encode(t, **d)))
        if r:
            replies.append(decode(r))
    return replies


async def test_a_real_drag_waits_for_the_bytes(tmp_path):
    drop, receiver, helper, calls = native(tmp_path)
    drop.begin("d1", CARD)
    staging = tmp_path / "drops" / "d1"
    assert (staging / "movie.mp4").is_file() and (staging / "pics").is_dir()   # placeholders
    line = helper[-1]
    assert line.startswith("nativedrag ")
    assert json.loads(line[11:])["paths"] == [str(staging / "movie.mp4"), str(staging / "pics")]
    assert drop.on_helper("nativedrag-ready d1")
    await settle()
    assert calls == ["press"]
    drop.on_helper("nativedrag-started d1")
    assert drop.holding()
    feed(receiver, "d1", done=False)
    assert not any(h.startswith("toast") for h in helper)   # quiet while carried
    drop.let_go("d1", inject=False)                          # let go too soon: wait
    assert any(h.startswith("toast") for h in helper) and "release" not in calls
    feed_end = receiver.handle(decode(encode("file_end", id="d1")))
    assert decode(feed_end).get("ok") is True
    await settle()
    assert calls[-1] == "release" and not drop.holding()
    assert (staging / "movie.mp4").read_bytes() == b"hi" and (staging / "pics" / "a.png").read_bytes() == b"ok"


async def test_files_that_arrive_first_drop_at_once(tmp_path):
    drop, receiver, helper, calls = native(tmp_path)
    drop.begin("d2", CARD)
    drop.on_helper("nativedrag-ready d2")
    drop.on_helper("nativedrag-started d2")
    feed(receiver, "d2")
    assert not drop.holding()
    drop.let_go("d2", inject=True)
    await settle()
    assert calls == ["press", "release"]


async def test_no_real_drag_saves_to_downloads(tmp_path):
    drop, receiver, helper, calls = native(tmp_path, press=False)
    drop.begin("d3", CARD)
    assert helper[-1].startswith("card ")
    drop.let_go("d3")
    feed(receiver, "d3")
    assert (tmp_path / "Downloads" / "movie.mp4").read_bytes() == b"hi"
    assert (tmp_path / "Downloads" / "pics" / "a.png").exists()
    assert not (tmp_path / "drops" / "d3").exists()


async def test_a_drag_that_never_starts_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr("curslink.files.native.START_TIMEOUT_S", 0.05)
    drop, receiver, helper, calls = native(tmp_path)
    drop.begin("d4", CARD)
    await asyncio.sleep(0.1)
    assert "nativedrag cancel" in helper and helper[-1].startswith("card ")
    feed(receiver, "d4")
    drop.let_go("d4")
    assert (tmp_path / "Downloads" / "movie.mp4").exists()


async def test_cancel_ends_the_drag_without_dropping(tmp_path):
    drop, receiver, helper, calls = native(tmp_path)
    drop.begin("d5", CARD)
    drop.on_helper("nativedrag-started d5")
    drop.cancel()
    await settle()
    assert calls[-1] == "cancel" and not (tmp_path / "drops" / "d5").exists()


# ------------------------------------------------- the agent handing a drag back


async def test_agent_hands_its_drag_to_the_host(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("hello")
    helper, sent, bulk, nudges = [], [], [], []

    async def send_bulk(t, **d):
        bulk.append((t, d))

    out = AgentDragOut(helper.append, lambda t, **d: sent.append((t, d)), send_bulk, "PC",
                       nudge=nudges.append, explorer=lambda: [])
    out.start()
    await asyncio.sleep(0.1)
    assert helper[0] == "shield on" and nudges == [False]
    out.on_helper("drag " + json.dumps({"paths": [str(f)]}))
    await asyncio.sleep(0.5)
    assert nudges == [False, True]            # then Escape and let go here
    assert helper[-1] == "shield off"
    (t, d), = sent
    assert t == "drag_out" and d["card"]["name"] == "notes.txt"
    out.fetch({"id": d["id"]})
    await settle()
    assert [t for t, _ in bulk][0] == "file_begin" and bulk[-1][0] == "file_end"


async def test_agent_falls_back_to_explorers_selection(tmp_path):
    f = tmp_path / "x.pdf"
    f.write_text("x")
    sent = []

    async def nothing(t, **d):
        pass

    out = AgentDragOut(lambda line: None, lambda t, **d: sent.append((t, d)), nothing, "PC",
                       nudge=lambda release: None, explorer=lambda: [str(f)])
    import curslink.files.dragout as dragout
    old, dragout.PROBE_WAIT_S = dragout.PROBE_WAIT_S, 0.05
    try:
        out.start()
        await asyncio.sleep(0.6)
    finally:
        dragout.PROBE_WAIT_S = old
    assert sent[0][0] == "drag_out" and sent[0][1]["card"]["name"] == "x.pdf"


async def test_agent_says_when_its_not_a_file():
    sent = []

    async def nothing(t, **d):
        pass

    import curslink.files.dragout as dragout
    old, dragout.PROBE_WAIT_S = dragout.PROBE_WAIT_S, 0.05
    try:
        out = AgentDragOut(lambda line: None, lambda t, **d: sent.append((t, d)), nothing, "PC",
                           nudge=lambda release: None, explorer=lambda: [])
        out.start()
        await asyncio.sleep(0.6)
    finally:
        dragout.PROBE_WAIT_S = old
    assert sent == [("drag_out", {"none": True})]


def test_helper_drag_source(capsys):
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    from curslink.files.overlay import FilesOverlay, card_pixmap

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    assert not card_pixmap(CARD).isNull()
    files = FilesOverlay(lambda x, y: (x, y))
    files.handle("nativedrag " + json.dumps({"id": "n1", "paths": ["/tmp/a"], "card": CARD}))
    assert files.source is not None and files.source.isVisible()
    assert "nativedrag-ready n1" in capsys.readouterr().out
    files.handle("nativedrag cancel")
    assert files.source is None
    app.processEvents()


async def test_end_to_end_agent_to_host_over_tls(tmp_path):
    host_in = FileReceiver(tmp_path / "host", lambda k, i: None)
    server = HostServer({"laptop"}, server_context(SECRET), host="127.0.0.1", port=0)
    server.on_drag_out = lambda device, data: server.send(device, "fetch", id=data["id"]) if data.get("id") else None
    server.on_files = lambda device, msg: host_in.handle(msg)
    await server.start()
    f = tmp_path / "report.docx"
    f.write_bytes(os.urandom(200_000))
    session = AgentSession(FakeSink(), lambda e, x, y: None)
    session.files = FileReceiver(tmp_path / "agent", lambda k, i: None)
    client = AgentClient("laptop", "Laptop", "127.0.0.1", server.port, client_context(SECRET), session)
    session.drag_out = AgentDragOut(lambda line: None, client.send, client.send_bulk, "Laptop",
                                    nudge=lambda release: None, explorer=lambda: [str(f)])
    import curslink.files.dragout as dragout
    old, dragout.PROBE_WAIT_S = dragout.PROBE_WAIT_S, 0.05
    task = asyncio.create_task(client.run_once())
    try:
        for _ in range(300):
            if server.is_connected("laptop"):
                break
            await asyncio.sleep(0.01)
        assert server.supports("laptop", FILES_OUT_FEATURE) and server.supports("laptop", DROP_FEATURE)
        server.send("laptop", "button", button="left", pressed=True)
        server.send("laptop", "drag_out")
        for _ in range(300):
            if (tmp_path / "host" / "report.docx").exists():
                break
            await asyncio.sleep(0.01)
        assert (tmp_path / "host" / "report.docx").read_bytes() == f.read_bytes()
        assert ("button", "left", False) not in session.sink.calls   # the agent never dropped it itself
    finally:
        dragout.PROBE_WAIT_S = old
        await server.close()
        await asyncio.wait_for(task, 3)


# ------------------------------------------- speed: little in flight, fast answers


def test_receiver_acks_only_when_asked(tmp_path):
    receiver = FileReceiver(tmp_path / "in")
    begin = dict(id="a1", count=1, total=4, **{"from": "Mac"})
    assert receiver.handle(decode(encode("file_begin", ack=True, **begin))) is None
    receiver.handle(decode(encode("file_item", id="a1", i=0, path="x.bin", size=4)))
    ack = decode(receiver.handle(decode(encode("file_data", id="a1", i=0, data="aGk="))))
    assert ack.t == "file_ack" and ack.data == {"id": "a1", "n": 2}
    receiver.handle(decode(encode("file_begin", **dict(begin, id="a2"))))
    receiver.handle(decode(encode("file_item", id="a2", i=0, path="y.bin", size=4)))
    assert receiver.handle(decode(encode("file_data", id="a2", i=0, data="aGk="))) is None


async def test_sender_keeps_only_its_window_in_flight(tmp_path, monkeypatch):
    monkeypatch.setattr("curslink.files.transfer.CHUNK", 1000)
    f = tmp_path / "big.bin"
    f.write_bytes(b"z" * 10_000)
    out = Outgoing("w1", "pc", meta.describe([str(f)]), window=3000)
    frames = []

    async def send(t, **d):
        frames.append(t)

    task = asyncio.get_running_loop().create_task(send_files(send, out, "Mac"))
    await settle()
    assert frames.count("file_data") == 3 and not task.done()   # waiting for acks
    assert decode(encode("file_begin", id="x", ack=True)).get("ack") is True
    out.ack(2000)
    await settle()
    assert frames.count("file_data") == 5
    out.set_window(100_000)                                    # dropped: open up
    await settle()
    assert task.done() and frames[-1] == "file_end" and frames.count("file_data") == 10


def test_outer_sides_skip_edges_between_screens():
    from curslink.files.dragout import outer_sides

    screens = [{"x": 0, "y": 0, "width": 1920, "height": 1080}, {"x": 1920, "y": 0, "width": 1280, "height": 1024}]
    sides = {(s["x"], s["side"]) for s in outer_sides(screens)}
    assert (0, "right") not in sides and (1920, "left") not in sides
    assert {(0, "left"), (0, "top"), (0, "bottom"), (1920, "right"), (1920, "top"), (1920, "bottom")} <= sides


async def test_agent_knows_the_file_before_it_crosses(tmp_path, monkeypatch):
    import curslink.files.dragout as dragout

    monkeypatch.setattr(dragout, "WATCH_AFTER_S", 0.01)
    f = tmp_path / "x.pdf"
    f.write_text("x")
    sent, helper, order = [], [], []

    async def nothing(t, **d):
        pass

    def nudge(release):
        order.append("nudge-release" if release else "nudge")

    out = AgentDragOut(helper.append, lambda t, **d: (sent.append((t, d)), order.append(t)), nothing, "PC",
                       nudge=nudge, explorer=lambda: [], screens=lambda: [{"x": 0, "y": 0, "width": 800, "height": 600}])
    out.on_button("left", True)
    await asyncio.sleep(0.05)
    assert helper[-1].startswith("strips ") and len(json.loads(helper[-1][7:])) == 4
    out.on_helper("drag " + json.dumps({"paths": [str(f)]}))   # the drag reached an edge
    out.start()
    await asyncio.sleep(0.6)
    assert sent[0][0] == "drag_out" and sent[0][1]["card"]["name"] == "x.pdf"
    assert order == ["drag_out", "nudge-release"]               # answered first, no probing nudge
    assert "strips off" in helper


async def test_a_plain_click_takes_the_strips_down(monkeypatch):
    import curslink.files.dragout as dragout

    monkeypatch.setattr(dragout, "WATCH_AFTER_S", 0.01)
    helper = []

    async def nothing(t, **d):
        pass

    out = AgentDragOut(helper.append, lambda t, **d: None, nothing, "PC", nudge=lambda r: None,
                       explorer=lambda: [], screens=lambda: [{"x": 0, "y": 0, "width": 800, "height": 600}])
    out.on_button("left", True)
    out.on_button("left", False)                                # quicker than the watch
    await asyncio.sleep(0.05)
    assert helper == []
    out.on_button("left", True)
    await asyncio.sleep(0.05)
    out.on_button("left", False)
    assert helper[-1] == "strips off"


async def test_the_card_shows_at_once_and_gives_way_to_the_real_drag(tmp_path):
    drop, receiver, helper, calls = native(tmp_path)
    drop.begin("c1", CARD)
    assert helper[-2].startswith("card ") and helper[-1].startswith("nativedrag ")
    drop.on_helper("nativedrag-started c1")
    assert helper[-1] == "card hide"


async def test_a_mac_drag_keeps_the_card_following_the_pointer(tmp_path):
    drop, receiver, helper, calls = native(tmp_path)
    drop.begin("m1", CARD)
    drop.on_helper("nativedrag-started m1 card")
    assert "card hide" not in helper
    drop.on_helper("nativedrag-ended m1 copy")
    assert helper[-1] == "card hide"


async def test_agent_without_screen_info_asks_the_helper_for_edges(monkeypatch):
    import curslink.files.dragout as dragout

    monkeypatch.setattr(dragout, "WATCH_AFTER_S", 0.01)
    helper = []

    async def nothing(t, **d):
        pass

    out = AgentDragOut(helper.append, lambda t, **d: None, nothing, "PC", nudge=lambda r: None, explorer=lambda: [])
    out.on_button("left", True)
    await asyncio.sleep(0.05)
    assert helper == ["strips edges"]


def test_helper_edge_strips(capsys):
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    from curslink.files.overlay import FilesOverlay

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    files = FilesOverlay(lambda x, y: (x, y))
    files.handle("strips edges")
    assert len(files.strips) == 4 * len(app.screens())
    assert "drop targets on the screen edges" in capsys.readouterr().out
    files.handle("strips off")
    assert files.strips == []


async def test_a_held_drag_keeps_checking_the_window_under_it(tmp_path, monkeypatch):
    monkeypatch.setattr("curslink.files.native.FOLLOW_EVERY_S", 0.01)

    class Follow:
        ticks, resets = 0, 0

        def tick(self, now):
            self.ticks += 1

        def reset(self):
            self.resets += 1

    follow = Follow()
    drop, receiver, helper, calls = native(tmp_path)
    drop.follow = follow
    drop.begin("f1", CARD)
    await asyncio.sleep(0.05)
    assert follow.ticks == 0                       # not before the real drag starts
    drop.on_helper("nativedrag-started f1 card")
    await asyncio.sleep(0.06)
    assert follow.resets == 1 and follow.ticks >= 3
    drop.on_helper("nativedrag-ended f1 copy")
    await asyncio.sleep(0.03)
    seen = follow.ticks
    await asyncio.sleep(0.05)
    assert follow.ticks == seen                    # stops with the drag


def test_focus_follower_waits_for_the_pointer_to_rest():
    from curslink.files.focus import FocusFollower

    under = {"hit": ((42, 7), "Mail", 10, 10)}
    raised = []

    class Impl:
        @staticmethod
        def probe(skip):
            return under["hit"]

        @staticmethod
        def bring(key, x, y):
            raised.append(key[0])

    f = FocusFollower(impl=Impl)
    f.tick(0.0)
    f.tick(0.1)
    assert raised == []                            # only just arrived
    f.tick(0.3)
    f.tick(0.5)
    assert raised == [42]                          # once, after resting
    under["hit"] = ((43, 9), "Notes", 10, 10)
    f.tick(0.6)
    f.tick(0.9)
    assert raised == [42, 43]
    under["hit"] = None                            # the taskbar, or ours
    f.tick(1.5)
    assert raised == [42, 43]


def test_every_drag_frame_the_host_sends_is_valid():
    # 0.4.34: an agent rejected drag {drop, id} as malformed and dropped the link.
    from curslink.protocol import ProtocolError

    for data in ({"card": CARD, "id": "t1"}, {"drop": True, "id": "t1"}, {"hide": True, "id": "t1"},
                 {"hide": True}, {"card": CARD}):
        assert decode(encode("drag", **data)).t == "drag"
    with pytest.raises(ProtocolError):
        decode(encode("drag", drop=True))
    with pytest.raises(ProtocolError):
        decode(encode("drag", id="t1"))


async def test_carried_drag_frames_survive_the_wire(new_drag, tmp_path):
    handoff, router, transport, helper, injected = new_drag
    f = tmp_path / "big.bin"
    f.write_bytes(b"x" * 1000)
    handoff.items = meta.describe([str(f)])
    handoff.carry("laptop")
    handoff.drop()
    await settle()
    for device, t, data in transport.sent:
        decode(encode(t, **data))  # raises if the agent would refuse it
