from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from curslink.clipboard.sync import CHUNK, Clip, ClipboardSync, Receiver, clip_from_helper, helper_command, send_clip
from curslink.config import ConfigError, agent_config, host_config
from curslink.protocol import Message, decode, encode


class Wire:
    """A bulk sender that checks every frame against the protocol."""

    def __init__(self):
        self.frames: list[Message] = []

    async def __call__(self, t: str, **data) -> None:
        self.frames.append(decode(encode(t, **data)))


def deliver(wire: Wire, receiver: Receiver) -> None:
    for msg in wire.frames:
        receiver.handle(msg.t, dict(msg.data))


def test_text_round_trip(tmp_path):
    got = []
    wire = Wire()
    asyncio.run(send_clip(wire, Clip("text", text="héllo\nworld")))
    assert [m.t for m in wire.frames] == ["clip", "clip_data"]
    deliver(wire, Receiver(tmp_path, 1 << 20, lambda clip, cid: got.append(clip)))
    assert got[0].kind == "text" and got[0].text == "héllo\nworld"


def test_big_image_comes_in_chunks(tmp_path):
    png = bytes(range(256)) * (CHUNK // 256 * 3 + 7)
    got = []
    wire = Wire()
    asyncio.run(send_clip(wire, Clip("image", image=png)))
    assert len(wire.frames) == 1 + 4
    deliver(wire, Receiver(tmp_path, 1 << 20, lambda clip, cid: got.append(clip)))
    assert got[0].image == png
    assert Path(got[0].tops[0]).read_bytes() == png


def test_files_and_folders_round_trip(tmp_path):
    src = tmp_path / "src"
    (src / "folder" / "sub").mkdir(parents=True)
    (src / "a.txt").write_bytes(b"A" * (CHUNK + 5))
    (src / "folder" / "empty.txt").write_bytes(b"")
    (src / "folder" / "sub" / "b.bin").write_bytes(b"BB")
    clip = clip_from_helper({"kind": "files", "paths": [str(src / "a.txt"), str(src / "folder")]}, 1 << 20)
    assert clip is not None and clip.size == CHUNK + 7
    got = []
    wire = Wire()
    asyncio.run(send_clip(wire, clip))
    deliver(wire, Receiver(tmp_path / "staging", 1 << 20, lambda c, cid: got.append(c)))
    out = got[0]
    assert [Path(t).name for t in out.tops] == ["a.txt", "folder"]
    top = Path(out.tops[1])
    assert (top / "empty.txt").read_bytes() == b""
    assert (top / "sub" / "b.bin").read_bytes() == b"BB"
    assert Path(out.tops[0]).read_bytes() == b"A" * (CHUNK + 5)
    line = helper_command(out)
    assert line.startswith("clipset ") and json.loads(line[8:])["paths"] == out.tops


def test_too_big_is_not_sent_or_taken(tmp_path):
    assert clip_from_helper({"kind": "text", "text": "x" * 100}, 50) is None
    got = []
    wire = Wire()
    asyncio.run(send_clip(wire, Clip("text", text="x" * 100)))
    deliver(wire, Receiver(tmp_path, 50, lambda c, cid: got.append(c)))
    assert got == []


def test_unsafe_paths_are_refused(tmp_path):
    got = []
    r = Receiver(tmp_path, 1 << 20, lambda c, cid: got.append(c))
    r.handle("clip", {"id": "x", "kind": "files", "size": 1, "files": [{"path": "../evil", "size": 1}]})
    r.handle("clip_data", {"id": "x", "data": "QQ==", "end": True})
    assert got == [] and not (tmp_path.parent / "evil").exists()


def test_a_newer_clip_replaces_one_arriving(tmp_path):
    got = []
    r = Receiver(tmp_path, 1 << 20, lambda c, cid: got.append(c.text))
    r.handle("clip", {"id": "old", "kind": "text", "size": 6})
    r.handle("clip_data", {"id": "old", "data": "YWJj"})
    r.handle("clip", {"id": "new", "kind": "text", "size": 2})
    r.handle("clip_data", {"id": "old", "data": "ZGVm", "end": True})
    r.handle("clip_data", {"id": "new", "data": "aGk=", "end": True})
    assert got == ["hi"]


async def test_host_passes_an_agents_copy_to_the_others(tmp_path):
    lines = []
    sync = ClipboardSync(lines.append, 1 << 20, staging=tmp_path)
    a, b = Wire(), Wire()
    sync.add_peer("a", a)
    sync.add_peer("b", b)
    sent = Wire()
    await send_clip(sent, Clip("text", text="from a"))
    for m in sent.frames:
        sync.handle("a", m.t, dict(m.data))
    await asyncio.sleep(0.01)
    assert lines == ['clipset {"kind":"text","text":"from a"}']
    assert a.frames == []
    assert [m.t for m in b.frames] == ["clip", "clip_data"]

    # Copied on the host: to everyone.
    sync.helper_line('clip {"kind":"text","text":"here"}')
    await asyncio.sleep(0.01)
    assert len(a.frames) == 2 and len(b.frames) == 4
    sync.stop()
    assert sync.peers == {}


async def test_image_file_from_the_helper_is_removed(tmp_path):
    png = tmp_path / "copied.png"
    png.write_bytes(b"\x89PNG....")
    sync = ClipboardSync(lambda line: None, 1 << 20, staging=tmp_path / "s")
    wire = Wire()
    sync.add_peer("host", wire)
    sync.helper_line("clip " + json.dumps({"kind": "image", "path": str(png)}))
    await asyncio.sleep(0.01)
    assert not png.exists()
    assert wire.frames[0].get("kind") == "image" and wire.frames[0].get("size") == 8


def test_protocol_checks_clip_frames():
    from curslink.protocol import ProtocolError

    with pytest.raises(ProtocolError):
        decode(b'{"t":"clip","id":"x","kind":"html","size":1}')
    with pytest.raises(ProtocolError):
        decode(b'{"t":"clip_data","id":"x"}')


def _host_raw(**host):
    return {
        "host": {"device_id": "desk", **host},
        "secret": "s" * 32,
        "devices": [{"id": "desk", "name": "Desk", "screens": [{"id": "m", "x": 0, "y": 0, "width": 10,
                                                                 "height": 10}]}],
    }


def test_config():
    try:
        cfg = host_config(_host_raw())
    except ConfigError:  # the minimal layout above isn't what this checks
        pytest.skip("layout")
    assert cfg.clipboard is True and cfg.clipboard_max_mb == 25
    assert host_config(_host_raw(clipboard=False, clipboard_max_mb=5)).clipboard_max_mb == 5
    with pytest.raises(ConfigError):
        host_config(_host_raw(clipboard_max_mb=0))
    agent = agent_config({"agent": {"device_id": "lap", "clipboard": False}})
    assert agent.clipboard is False


# ----------------------------------------------------------------- the helper


@pytest.fixture
def qapp():
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication(["curslink-test"])


def test_helper_reports_copies_but_not_its_own(qapp, capsys, tmp_path):
    from PySide6.QtGui import QGuiApplication, QImage

    from curslink.clipboard.helper import ClipboardHelper

    helper = ClipboardHelper()
    helper.handle("clipwatch on")
    board = QGuiApplication.clipboard()
    board.setText("copied by an app")
    if board.text() != "copied by an app":
        pytest.skip("no clipboard on this platform")
    helper.read()
    out = capsys.readouterr().out.strip().splitlines()
    assert out and json.loads(out[-1].split(" ", 1)[1]) == {"kind": "text", "text": "copied by an app"}

    helper.handle('clipset {"kind":"text","text":"from the other one"}')
    assert board.text() == "from the other one"
    helper.read()
    assert capsys.readouterr().out == ""

    img = QImage(4, 3, QImage.Format.Format_ARGB32)
    img.fill(0xFF336699)
    path = tmp_path / "in.png"
    img.save(str(path), "PNG")
    helper.handle("clipset " + json.dumps({"kind": "image", "path": str(path)}))
    helper.read()
    # Logged, not reported as a copy made here.
    assert capsys.readouterr().out == "log clipboard: image 4x3 ready to paste\n"

    helper.set_at = 0  # long after: copying again is news
    board.setText("again")
    helper.read()
    assert "again" in capsys.readouterr().out
    helper.handle("clipwatch off")
    board.setText("ignored")
    helper.read()
    assert capsys.readouterr().out == ""


def test_private_copies_are_not_shared():
    from curslink.clipboard.helper import is_private

    assert is_private(['application/x-qt-mime-type-name;value="org.nspasteboard.ConcealedType"'])
    assert is_private(['application/x-qt-windows-mime;value="ExcludeClipboardContentFromMonitorProcessing"'])
    assert not is_private(["text/plain"])


def test_windows_yes_no_markers_read_their_value():
    """Snipping Tool and others write CanIncludeInClipboardHistory = 1 (yes):
    that's not private. Only 0 is."""
    from curslink.clipboard.helper import is_private, private_marker

    fmt = 'application/x-qt-windows-mime;value="CanIncludeInClipboardHistory"'
    assert not is_private(["image/png", fmt], lambda f: (1).to_bytes(4, "little"))
    assert private_marker([fmt], lambda f: (0).to_bytes(4, "little")) == "CanIncludeInClipboardHistory"
    assert is_private([fmt])               # value unknown: careful
    assert is_private([fmt], lambda f: b"")


async def test_both_ways_over_a_real_link(tmp_path):
    """Host and agent ClipboardSync over real TLS-PSK sockets, files included."""
    from conftest import FakeSink
    from curslink.net.client import AgentClient, AgentSession
    from curslink.net.server import HostServer
    from curslink.protocol import CLIPBOARD_FEATURE
    from curslink.security import client_context, server_context

    secret = "correct horse battery staple"
    host_lines, agent_lines = [], []
    host_sync = ClipboardSync(host_lines.append, 1 << 22, staging=tmp_path / "h")
    agent_sync = ClipboardSync(agent_lines.append, 1 << 22, staging=tmp_path / "a")
    server = HostServer({"laptop"}, server_context(secret), host="127.0.0.1", port=0)
    server.features.append(CLIPBOARD_FEATURE)
    server.on_clip = host_sync.handle
    server.on_connect = lambda conn: (host_sync.add_peer(conn.device_id, server.bulk_sender(conn.device_id))
                                      if CLIPBOARD_FEATURE in conn.features else None)
    await server.start()
    session = AgentSession(FakeSink(), lambda e, x, y: None)
    session.clipboard = agent_sync
    client = AgentClient("laptop", "Laptop", "127.0.0.1", server.port, client_context(secret), session)
    client.on_state = lambda state, detail: (agent_sync.add_peer("host", client.send_bulk)
                                             if state == "connected" and CLIPBOARD_FEATURE in client.host_features
                                             else None)
    task = asyncio.create_task(client.run_once())
    try:
        for _ in range(300):
            if "laptop" in host_sync.peers and "host" in agent_sync.peers:
                break
            await asyncio.sleep(0.01)
        assert "laptop" in host_sync.peers and "host" in agent_sync.peers

        host_sync.helper_line('clip {"kind":"text","text":"from the host"}')
        for _ in range(300):
            if agent_lines:
                break
            await asyncio.sleep(0.01)
        assert agent_lines == ['clipset {"kind":"text","text":"from the host"}']

        big = tmp_path / "big.bin"
        big.write_bytes(bytes(range(256)) * 4096)  # 1 MiB
        agent_sync.helper_line("clip " + json.dumps({"kind": "files", "paths": [str(big)]}))
        for _ in range(500):
            if host_lines:
                break
            await asyncio.sleep(0.01)
        paths = json.loads(host_lines[0][8:])["paths"]
        assert Path(paths[0]).read_bytes() == big.read_bytes()
    finally:
        await server.close()
        await asyncio.wait_for(task, 3)


async def test_a_text_only_peer_gets_no_pictures(tmp_path):
    sync = ClipboardSync(lambda line: None, 1 << 20, staging=tmp_path)
    phone, pc = Wire(), Wire()
    sync.add_peer("phone", phone, ("text",))
    sync.add_peer("pc", pc)
    png = tmp_path / "x.png"
    png.write_bytes(b"png")
    sync.helper_line("clip " + json.dumps({"kind": "image", "path": str(png)}))
    sync.helper_line('clip {"kind":"text","text":"hi"}')
    await asyncio.sleep(0.01)
    assert [m.get("kind") for m in phone.frames if m.t == "clip"] == ["text"]
    assert pc.frames[0].t == "clip"
