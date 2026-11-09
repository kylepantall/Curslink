"""A real Curslink host for the Android core's interop test (InteropTest.kt).

Runs the Python host's own QR pairing, TLS-PSK server, UDP moves and relay,
so the Kotlin code is checked against the code it has to talk to, not a
copy of it. Talks to the test over stdout, one line per event:

    URL <curslink://pair?...>      scan this
    PAIRED <device id> <json>      the phone paired (json: what it said about itself)
    CONNECTED <device id> <json>   it connected (json: hello's screens, features)
    SENT <json>                    the input below went out (json: {"udp": bool})
    GONE <device id> <paused>      it disconnected (paused: it said bye, paused)
    DROPPED <json>                 a file dragged from here was let go of on the
                                   phone and it said so (json: its file_result)
    RELAYED <json>                 a file dragged on a PC agent, carried to the
                                   phone and let go of there, went PC -> host ->
                                   phone (json: {"result", "pc_heard"})

On each connection it sends: enter, a move, a left click, a scroll, "a"
pressed and released, shift pressed (never released: the agent must let go
of it on leave), then leave.

On the first connection of a phone that takes files it then plays two file
drags with the host's real DragHandoff and Router: one from this computer
(card, drop, then the bytes), and one from an in-process Python PC agent
that hands its held drag over as the pointer crosses to the phone, so the
bytes go PC -> host -> phone only once it's let go of there.
"""

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

from curslink.net.relay import HostRelayLink, RelayServer, RelayTarget, paired_room_id
from curslink.net.server import HostServer
from curslink.pairing.handshake import DeviceInfo
from curslink.pairing.qr import QrPairing
from curslink.security import HelloVerifier, server_context

HOST = DeviceInfo("interop-host", "Interop Mac", "host")
RELAY_KEY = "interop-relay-key-0123456789"
PC_ID, PC_KEY = "interop-pc", b"p" * 32
# What the phone should end up with (the test checks the bytes).
FROM_HOST = b"from the host " * 3000
FROM_PC = b"from the pc " * 9000


class Grab:
    def set_grab(self, grabbed) -> None:
        pass


class Sink:
    def move_to(self, x, y) -> None:
        pass

    def key(self, key, pressed) -> None:
        pass

    def button(self, button, pressed) -> None:
        pass

    def scroll(self, dx, dy) -> None:
        pass


async def wait_for(check, seconds: float = 10.0) -> bool:
    for _ in range(int(seconds * 50)):
        if check():
            return True
        await asyncio.sleep(0.02)
    return False


async def play_files(server, phone: dict, folder: Path) -> None:
    """The two file drags (see the module docstring)."""
    from curslink.files import dragout
    from curslink.files.dragout import AgentDragOut
    from curslink.files.handoff import DragHandoff
    from curslink.files.meta import describe
    from curslink.files.transfer import FileReceiver
    from curslink.layout import Device, Layout, Screen
    from curslink.net.client import AgentClient, AgentSession
    from curslink.router import Router
    from curslink.security import client_context

    dragout.PROBE_WAIT_S = 0.05
    w, h = int(phone["width"]), int(phone["height"])
    layout = Layout.from_devices([
        Device(HOST.device_id, "Mac", (Screen("main", HOST.device_id, 0, 0, 1920, 1080),)),
        Device(PC_ID, "PC", (Screen("main", PC_ID, 1920, 0, 1920, 1080),)),
        Device(phone["id"], "Phone", (Screen("main", phone["id"], 3840, 0, w, h),)),
    ])
    router = Router(layout, HOST.device_id, Grab(), Sink(), server)
    results: dict[str, dict] = {}
    handoff = DragHandoff(router, server, lambda line: None, lambda kind: None, HOST.name,
                          emit=lambda kind, **d: results.__setitem__(d.get("id"), d) if d.get("state") != "sending" else None)
    server.on_file_result = handoff.on_result
    server.on_drag_out = handoff.on_drag_out
    server.on_files = handoff.on_files

    # 1. Dragged on this computer, over to the phone, let go of there.
    a = folder / "from-host.txt"
    a.write_bytes(FROM_HOST)
    handoff.on_button("left", True)
    handoff.items = describe([str(a)])
    router.switch_to(layout.screen(phone["id"], "main"), 3840 + w / 2, h / 2, "gaze")
    await asyncio.sleep(0.3)
    handoff.on_button("left", False)
    await wait_for(lambda: any(r.get("state") == "sent" for r in results.values()))
    say("DROPPED", json.dumps(next((r for r in results.values()), {}), sort_keys=True))
    results.clear()
    router.go_home("hotkey")

    # 2. Dragged on the PC (held), carried over its edge to the phone, let go of there.
    b = folder / "from-pc.bin"
    b.write_bytes(FROM_PC)
    session = AgentSession(Sink(), lambda *effect: None)
    session.files = FileReceiver(folder / "pc-in", lambda k, i: None)
    pc = AgentClient(PC_ID, "Interop PC", "127.0.0.1", server.port, client_context(device_id=PC_ID, key=PC_KEY),
                     session, auth_key=PC_KEY, udp=False)
    session.drag_out = AgentDragOut(lambda line: None, pc.send, pc.send_bulk, "Interop PC",
                                    nudge=lambda release: None, explorer=lambda: [str(b)])
    task = asyncio.create_task(pc.run_once())
    try:
        await wait_for(lambda: server.is_connected(PC_ID))
        router.switch_to(layout.screen(PC_ID, "main"), 3000, 500, "gaze")
        router.on_button("left", True)              # a drag starts in Explorer...
        await asyncio.sleep(0.2)
        router.switch_to(layout.screen(phone["id"], "main"), 3840 + w / 2, h / 2, "edge")   # ...and goes on
        await wait_for(lambda: handoff.remote is not None)
        await asyncio.sleep(0.3)
        handoff.on_button("left", False)            # let go on the phone
        await wait_for(lambda: bool(results) and session.drag_out.pending is None)
        say("RELAYED", json.dumps({"result": next((r for r in results.values()), {}),
                                   "pc_heard": session.drag_out.pending is None,
                                   "pc_kept": (folder / "pc-in").exists()}, sort_keys=True))
    finally:
        task.cancel()


def say(*parts) -> None:
    print(*parts, flush=True)


async def main() -> None:
    # A phone whose key the host has but that isn't in its devices (paired,
    # but the host never reloaded its config): it should be told why.
    keys: dict[str, bytes] = {"pixel-stranger": b"s" * 32, PC_ID: PC_KEY}
    known: set[str] = {PC_ID}
    played_files: set[str] = set()
    folder = Path(tempfile.mkdtemp(prefix="curslink-interop-"))
    links: list[HostRelayLink] = []
    loop = asyncio.get_running_loop()
    server: HostServer

    def on_connect(conn) -> None:
        if conn.device_id == PC_ID:
            return
        say("CONNECTED", conn.device_id, json.dumps({"screens": conn.screens, "features": sorted(conn.features),
                                                    "platform": conn.platform}))
        asyncio.create_task(play(conn))

    async def play(conn) -> None:
        d = conn.device_id
        if "udp" in conn.features:
            for _ in range(60):  # until the agent's probes get through
                if conn.udp is not None and conn.udp.usable(time.monotonic()):
                    break
                await asyncio.sleep(0.05)
        udp = conn.udp is not None and conn.udp.usable(time.monotonic())
        server.send(d, "enter", x=10, y=20, effect="pop")
        await asyncio.sleep(0.2)
        server.send(d, "move", x=30, y=40, ts=round(time.monotonic() * 1000, 2))
        await asyncio.sleep(0.2)
        server.send(d, "button", button="left", pressed=True)
        server.send(d, "button", button="left", pressed=False)
        server.send(d, "scroll", dx=0, dy=-1.5)
        server.send(d, "key", key="a", pressed=True)
        server.send(d, "key", key="a", pressed=False)
        server.send(d, "key", key="shift", pressed=True)
        server.send(d, "leave", effect="blink", x=30, y=40)
        say("SENT", json.dumps({"udp": udp}))
        if "files" in conn.features and d not in played_files:
            played_files.add(d)
            screen = (conn.screens or [{}])[0]
            await asyncio.sleep(0.3)
            await play_files(server, {"id": d, "width": screen.get("width", 400), "height": screen.get("height", 800)},
                             folder)

    def on_disconnect(device_id: str) -> None:
        say("GONE", device_id, json.dumps(device_id in server.paused))

    server = HostServer(known, server_context(None, keys.get), on_connect=on_connect, on_disconnect=on_disconnect,
                        host="127.0.0.1", port=0,
                        verify_hello=HelloVerifier(None, keys.get))
    await server.start()
    relay = RelayServer(RELAY_KEY, host="127.0.0.1", port=0)
    await relay.start()

    def paired(result, address) -> None:
        keys[result.peer.device_id] = result.key
        known.add(result.peer.device_id)
        target = RelayTarget(f"127.0.0.1:{relay.port}", RELAY_KEY, paired_room_id(result.key, HOST.device_id))
        link = HostRelayLink(target, server_context(None, keys.get), server.handle_streams)
        loop.call_soon(link.start)
        links.append(link)
        say("PAIRED", result.peer.device_id, json.dumps(result.extra))

    qr = QrPairing(HOST, paired, host="127.0.0.1")
    await qr.start()
    say("URL", qr.url(["127.0.0.1"], server.port, f"127.0.0.1:{relay.port}", RELAY_KEY))
    # Until the test closes our stdin.
    await loop.run_in_executor(None, sys.stdin.read)


if __name__ == "__main__":
    asyncio.run(main())
