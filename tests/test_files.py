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


async def test_files_are_sent_only_once_let_go(new_drag, tmp_path):
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
    assert transport.bulk == []                                    # carried: only the card
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
    assert handoff.carry_id is None and transport.bulk == []


async def test_a_file_dragged_home_from_an_agent(new_drag, tmp_path):
    handoff, router, transport, helper, injected = new_drag
    router.on_pointer(3839, 500)
    router.on_button("left", True)       # Explorer drag starts on the laptop
    router.go_home("edge")               # ...and comes over the edge
    assert transport.of_type("drag_out")
    card = {"name": "photo.jpg", "kind": "JPG image", "size": 3, "count": 1, "ext": "JPG",
            "items": [{"name": "photo.jpg", "dir": False}]}
    handoff.on_drag_out("laptop", {"id": "t9", "card": card})
    assert not transport.of_type("fetch")                              # nothing moves while it's held
    assert any(h.startswith("card ") for h in helper)                  # no real drags here: the card
    assert handoff.on_button("left", False) is True                    # dropped here
    assert transport.of_type("fetch")[-1][2] == {"id": "t9"}          # ...so now it's fetched
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


def native(tmp_path, press=True, end_watch=False):
    helper, calls = [], []
    popups = ReceivePopups(helper.append)
    receiver = FileReceiver(tmp_path / "Downloads", popups)
    drop = NativeDrop(helper.append, receiver, popups,
                      press=(lambda: calls.append("press")) if press else None,
                      release=lambda: calls.append("release"), cancel=lambda: calls.append("cancel"),
                      root=tmp_path / "drops", end_watch=end_watch)
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


async def test_a_drag_that_misses_the_let_go_is_ended_then_saved(tmp_path, monkeypatch):
    """0.4.46 on a Mac: the real drag sometimes kept going after the drop,
    and every drag after it fell back to Downloads. Now the helper is told
    to end it, then the files are saved to Downloads."""
    import curslink.files.native as native_mod

    monkeypatch.setattr(native_mod, "END_WAIT_S", 0.01)
    monkeypatch.setattr(native_mod, "FOCUS_SETTLE_S", 0.0)
    drop, receiver, helper, calls = native(tmp_path, end_watch=True)
    drop.begin("s1", CARD)
    drop.on_helper("nativedrag-ready s1")
    drop.on_helper("nativedrag-started s1 card")
    feed(receiver, "s1")
    drop.let_go("s1", inject=False)                 # the real button-up went by...
    await asyncio.sleep(0.05)                       # ...but no nativedrag-ended
    assert "nativedrag end" in helper
    assert "s1" not in drop.drops
    assert (tmp_path / "Downloads" / "movie.mp4").read_bytes() == b"hi"
    drop.on_helper("nativedrag-ended s1 copy")      # a late end changes nothing


async def test_a_stuck_drag_is_ended_before_the_next_one(tmp_path):
    drop, receiver, helper, calls = native(tmp_path, end_watch=True)
    drop.begin("s2", CARD)
    drop.on_helper("nativedrag-ready s2")
    drop.on_helper("nativedrag-started s2 card")
    feed(receiver, "s2")
    drop.let_go("s2", inject=False)
    drop.begin("s3", CARD)                          # before the watch fires
    assert "nativedrag end" in helper
    assert (tmp_path / "Downloads" / "movie.mp4").is_file()
    assert set(drop.drops) == {"s3"}


async def test_a_drag_that_ends_is_left_alone(tmp_path, monkeypatch):
    import curslink.files.native as native_mod

    monkeypatch.setattr(native_mod, "END_WAIT_S", 0.01)
    drop, receiver, helper, calls = native(tmp_path, end_watch=True)
    drop.begin("s4", CARD)
    drop.on_helper("nativedrag-ready s4")
    drop.on_helper("nativedrag-started s4 card")
    feed(receiver, "s4")
    drop.let_go("s4", inject=False)
    drop.on_helper("nativedrag-ended s4 copy")
    await asyncio.sleep(0.2)
    assert "nativedrag end" not in helper
    assert not (tmp_path / "Downloads" / "movie.mp4").exists()


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


async def test_agent_knows_the_file_before_it_crosses(tmp_path):
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
    for x in (400, 500, 650, 780):                              # dragged towards the right edge
        out.on_pointer(x, 300)
    assert helper[-1].startswith("strips ") and len(json.loads(helper[-1][7:])) == 4
    out.on_helper("drag " + json.dumps({"paths": [str(f)]}))   # the drag reached an edge
    out.start()
    await asyncio.sleep(0.6)
    assert sent[0][0] == "drag_out" and sent[0][1]["card"]["name"] == "x.pdf"
    assert order == ["drag_out", "nudge-release"]               # answered first, no probing nudge
    assert "strips off" in helper


async def test_holding_the_button_puts_no_strips_up_until_a_drag_nears_an_edge():
    # 0.4.62 put the strips (always-on-top windows) up 0.1 s into every
    # press, and on Windows they made the pointer lag while the button was held.
    helper = []

    async def nothing(t, **d):
        pass

    out = AgentDragOut(helper.append, lambda t, **d: None, nothing, "PC", nudge=lambda r: None,
                       explorer=lambda: [], screens=lambda: [{"x": 0, "y": 0, "width": 800, "height": 600}])
    out.on_button("left", True)
    out.on_pointer(400, 300)
    await asyncio.sleep(0.2)
    assert helper == []                                         # held still: nothing
    for x in (420, 500, 600):                                   # dragging, but mid-screen
        out.on_pointer(x, 300)
    assert helper == []
    out.on_pointer(700, 300)                                    # 99 px from the right edge
    assert helper[-1].startswith("strips ")
    out.on_pointer(720, 300)
    assert len(helper) == 1                                     # once, not per move
    out.on_pointer(400, 300)                                    # back mid-screen
    assert helper[-1] == "strips off"
    out.on_button("left", False)
    assert helper.count("strips off") == 1


async def test_a_plain_click_puts_no_strips_up():
    helper = []

    async def nothing(t, **d):
        pass

    out = AgentDragOut(helper.append, lambda t, **d: None, nothing, "PC", nudge=lambda r: None,
                       explorer=lambda: [], screens=lambda: [{"x": 0, "y": 0, "width": 800, "height": 600}])
    for _ in range(3):
        out.on_button("left", True)
        out.on_pointer(790, 300)                                # right at the edge, not moving
        out.on_pointer(791, 300)
        out.on_button("left", False)
    assert helper == []


def test_edge_distance():
    from curslink.files.dragout import edge_distance, outer_sides

    sides = outer_sides([{"x": 0, "y": 0, "width": 800, "height": 600},
                         {"x": 800, "y": 0, "width": 800, "height": 600}])
    assert edge_distance(sides, 799, 300) == 299                # the bottom: the shared side doesn't count
    assert edge_distance(sides, 1590, 300) == 9
    assert edge_distance(sides, 10, 300) == 10
    assert edge_distance(sides, 400, 595) == 4


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


async def test_agent_without_screen_info_asks_the_helper_for_edges():
    helper = []

    async def nothing(t, **d):
        pass

    out = AgentDragOut(helper.append, lambda t, **d: None, nothing, "PC", nudge=lambda r: None, explorer=lambda: [])
    out.on_button("left", True)
    out.on_pointer(100, 100)
    assert helper == []
    out.on_pointer(130, 100)                                    # a drag: no layout, so any drag
    assert helper == ["strips edges"]


def test_helper_edge_strips(capsys):
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    from curslink.files.overlay import FilesOverlay

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    files = FilesOverlay(lambda x, y: (x, y))
    files.handle("strips edges")
    assert len(files.strips) == 4 * len(app.screens())
    assert "drop targets on the screen edges" in capsys.readouterr().out
    first = list(files.strips)
    files.handle("strips off")
    assert files.strips == [] and not any(w.isVisible() for w in first)
    files.handle("strips edges")                                # the same windows, shown again
    assert {id(w) for w in files.strips} == {id(w) for w in first} and all(w.isVisible() for w in first)
    files.handle("strips off")


async def test_a_held_drag_keeps_checking_the_window_under_it(tmp_path, monkeypatch):
    monkeypatch.setattr("curslink.files.native.FOLLOW_EVERY_S", 0.01)

    class Follow:
        ticks, resets = 0, 0

        def tick(self, now):
            self.ticks += 1

        def reset(self):
            self.resets += 1

        def ready(self):
            return True

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


def test_focus_follower_keeps_trying_until_the_window_is_in_front():
    # 0.4.35: "you have to focus the app yourself sometimes" - one go that
    # Windows or macOS turned down was never tried again.
    from curslink.files.focus import FocusFollower

    state = {"front": None, "refuse": 2}
    raised, asked = [], []

    class Impl:
        @staticmethod
        def probe(skip):
            return 42, "Mail", 10, 10

        @staticmethod
        def bring(key, x, y):
            raised.append(key)
            if state["refuse"]:
                state["refuse"] -= 1
            else:
                state["front"] = key

        @staticmethod
        def is_front(key):
            return state["front"] == key

    f = FocusFollower(impl=Impl, ask_helper=asked.append)
    f.tick(0.0)
    assert not f.ready()
    f.tick(0.25)
    assert raised == [42] and asked == [42]        # refused: the helper tries too
    f.tick(0.35)
    assert raised == [42]                          # not straight away
    f.tick(0.6)
    f.tick(0.95)
    assert raised == [42, 42, 42] and f.ready()
    f.tick(1.5)
    assert raised == [42, 42, 42]                  # in front: left alone
    state["front"] = None                          # something took it back
    f.tick(1.6)
    assert raised == [42, 42, 42, 42]


def test_focus_follower_brings_forward_at_once_on_let_go():
    from curslink.files.focus import FocusFollower

    state = {"front": 1}
    raised = []

    class Impl:
        @staticmethod
        def probe(skip):
            return 7, "Notes", 0, 0

        @staticmethod
        def bring(key, x, y):
            raised.append(key)
            state["front"] = key

        @staticmethod
        def is_front(key):
            return state["front"] == key

    f = FocusFollower(impl=Impl)
    assert f.bring_now() is True and raised == [7]
    assert f.bring_now() is False and raised == [7]


async def test_a_drop_waits_for_the_window_under_it_to_come_forward(tmp_path, monkeypatch):
    monkeypatch.setattr("curslink.files.native.FOCUS_SETTLE_S", 0.02)

    class Follow:
        front = False
        brought = 0

        def tick(self, now):
            pass

        def reset(self):
            pass

        def ready(self):
            return self.front

        def bring_now(self):
            if self.front:
                return False
            self.brought += 1
            self.front = True
            return True

    drop, receiver, helper, calls = native(tmp_path)
    drop.follow = follow = Follow()
    drop.begin("w1", CARD)
    drop.on_helper("nativedrag-started w1")
    feed(receiver, "w1")                           # every byte is here...
    assert drop.holding()                          # ...but the window isn't in front
    drop.let_go("w1", inject=False)
    await asyncio.sleep(0)
    assert follow.brought == 1 and "release" not in calls
    await asyncio.sleep(0.05)
    assert calls[-1] == "release"


def test_helper_focus_lines_bring_the_window_forward(monkeypatch):
    import curslink.files.focus as focus

    got = []

    class Impl:
        @staticmethod
        def bring_here(token):
            got.append(token)

    monkeypatch.setattr(focus, "_platform", lambda: Impl)
    focus.from_helper("focus 1234\n")
    focus.from_helper("focus nope\n")
    assert got == [1234]


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


def test_drag_debug_lines_say_where_the_cursor_is_and_whats_in_front(monkeypatch, caplog):
    import logging

    import curslink.files.dragdebug as dd

    monkeypatch.setattr(dd, "_last", {})
    monkeypatch.setattr(dd, "snapshot", lambda: {
        "cursor": (120.4, 80.6),
        "front": {"app": "explorer.exe", "pid": 10, "title": "Downloads", "cls": "CabinetWClass",
                  "rect": (0, 0, 800, 600)},
        "under": {"app": "chrome.exe", "pid": 11, "title": "Upload", "rect": (100, 50, 900, 700)},
        "windows": [{"app": "explorer.exe", "pid": 10, "title": "Downloads"}, {"app": "chrome.exe", "pid": 11}],
    })
    with caplog.at_level(logging.INFO, logger="curslink.dragdebug"):
        dd.trace("let-go", id="t1")
        dd.trace("let-go", id="t1")                 # straight after: once is enough
    lines = [r.getMessage() for r in caplog.records if r.name == "curslink.dragdebug"]
    assert len(lines) == 1
    line = lines[0]
    assert line.startswith("drag-debug let-go id=t1 | cursor=(120,81)")
    assert "front=explorer.exe#10 'Downloads' <CabinetWClass> 0,0 800x600" in line
    assert "under=chrome.exe#11 'Upload'" in line
    assert "windows=[explorer.exe#10 'Downloads'; chrome.exe#11]" in line


def test_drag_debug_never_breaks_a_drag(monkeypatch, caplog):
    import logging

    import curslink.files.dragdebug as dd

    def broken():
        raise OSError("no screen")

    monkeypatch.setattr(dd, "_last", {})
    monkeypatch.setattr(dd, "snapshot", broken)
    with caplog.at_level(logging.INFO, logger="curslink.dragdebug"):
        dd.trace("focus")
    assert "error=OSError: no screen" in caplog.text


def test_focus_tries_leave_a_debug_line(monkeypatch, caplog):
    import logging

    import curslink.files.dragdebug as dd
    from curslink.files.focus import FocusFollower

    monkeypatch.setattr(dd, "_last", {})
    monkeypatch.setattr(dd, "snapshot", lambda: {"cursor": (1, 2)})

    class Impl:
        @staticmethod
        def probe(skip):
            return 5, "Mail", 1, 2

        @staticmethod
        def bring(key, x, y):
            pass

        @staticmethod
        def is_front(key):
            return False

    f = FocusFollower(impl=Impl)
    with caplog.at_level(logging.INFO, logger="curslink.dragdebug"):
        f.tick(0.0)
        f.tick(0.3)
    assert "drag-debug hover target='Mail' key=5 in_front=False" in caplog.text
    assert "drag-debug focus target='Mail' key=5 tried=1 front_after=False" in caplog.text


def test_drag_debug_snapshot_runs_on_this_computer():
    # Real calls on the Windows and macOS runners; nothing to find on Linux.
    import sys

    import curslink.files.dragdebug as dd

    snap = dd.snapshot()
    assert isinstance(snap, dict) and "error" not in snap
    dd.format_snapshot(snap)
    if sys.platform in ("win32", "darwin"):
        assert isinstance(snap.get("windows"), list)


async def test_slow_file_reads_keep_the_loop_free_and_say_so(monkeypatch):
    import threading
    import time as _time

    import curslink.files.transfer as tr

    monkeypatch.setattr(tr, "OPEN_SLOW_S", 0.05)
    gate = threading.Event()
    slow, ticks = [], []

    def blocked(path):  # like open() waiting on a permission prompt
        gate.wait(2)
        return path

    async def ticker():
        for _ in range(5):
            ticks.append(_time.monotonic())
            await asyncio.sleep(0.02)

    t = asyncio.ensure_future(ticker())
    got = asyncio.ensure_future(tr.off_loop_watched("/Users/k/Desktop/a.txt", slow.append, blocked, "x"))
    await asyncio.sleep(0.12)
    assert slow == ["/Users/k/Desktop/a.txt"]  # said once it was slow
    assert len(ticks) >= 4                     # the loop kept going meanwhile
    gate.set()
    assert await got == "x"
    await t


async def test_a_slow_read_brings_input_home_to_answer_the_prompt(new_drag):
    handoff, router, transport, helper, injected = new_drag
    router.on_pointer(3839, 500)
    router.on_button("left", True)
    assert not router.is_local
    handoff._slow_read("/Users/k/Desktop/a.txt")
    assert router.is_local


def test_the_agent_tells_the_drag_out_where_a_held_pointer_is():
    from curslink.net.client import AgentSession
    from curslink.protocol import Message

    class Sink:
        def move_to(self, x, y): pass
        def button(self, b, p): pass

    class Watch:
        busy = False

        def __init__(self):
            self.seen = []

        def on_button(self, *a): pass

        def on_pointer(self, x, y):
            self.seen.append((x, y))

    session = AgentSession(Sink(), lambda *a: None)
    session.drag_out = watch = Watch()
    session.handle(Message("enter", {"x": 5, "y": 6}))
    session.handle(Message("move", {"x": 10, "y": 10, "ts": 1.0}))
    assert watch.seen == []                                     # no button held
    session.handle(Message("button", {"button": "left", "pressed": True}))
    session.handle(Message("move", {"x": 20, "y": 10, "ts": 2.0}))
    assert watch.seen == [(20, 10)]
