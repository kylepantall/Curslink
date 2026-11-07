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
    line = capsys.readouterr().out.strip()
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
