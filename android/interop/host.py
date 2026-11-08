"""A real Curslink host for the Android core's interop test (InteropTest.kt).

Runs the Python host's own QR pairing, TLS-PSK server, UDP moves and relay,
so the Kotlin code is checked against the code it has to talk to, not a
copy of it. Talks to the test over stdout, one line per event:

    URL <curslink://pair?...>      scan this
    PAIRED <device id> <json>      the phone paired (json: what it said about itself)
    CONNECTED <device id> <json>   it connected (json: hello's screens, features)
    SENT <json>                    the input below went out (json: {"udp": bool})

On each connection it sends: enter, a move, a left click, a scroll, "a"
pressed and released, shift pressed (never released: the agent must let go
of it on leave), then leave.
"""

import asyncio
import json
import sys
import time

from curslink.net.relay import HostRelayLink, RelayServer, RelayTarget, paired_room_id
from curslink.net.server import HostServer
from curslink.pairing.handshake import DeviceInfo
from curslink.pairing.qr import QrPairing
from curslink.security import HelloVerifier, server_context

HOST = DeviceInfo("interop-host", "Interop Mac", "host")
RELAY_KEY = "interop-relay-key-0123456789"


def say(*parts) -> None:
    print(*parts, flush=True)


async def main() -> None:
    # A phone whose key the host has but that isn't in its devices (paired,
    # but the host never reloaded its config): it should be told why.
    keys: dict[str, bytes] = {"pixel-stranger": b"s" * 32}
    known: set[str] = set()
    links: list[HostRelayLink] = []
    loop = asyncio.get_running_loop()
    server: HostServer

    def on_connect(conn) -> None:
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

    server = HostServer(known, server_context(None, keys.get), on_connect=on_connect, host="127.0.0.1", port=0,
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
