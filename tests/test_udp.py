"""Pointer moves by UDP beside the TLS connection (curslink.net.udp)."""

import asyncio
import os

from conftest import FakeSink
from curslink.net import udp
from curslink.net.client import AgentClient, AgentSession
from curslink.net.server import HostServer
from curslink.protocol import Message
from curslink.security import client_context, server_context

SECRET = "correct horse battery staple"


async def wait_for(cond, timeout=3.0):
    for _ in range(int(timeout / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met")


async def start(host_udp=True, agent_udp=True):
    server = HostServer({"laptop"}, server_context(SECRET), host="127.0.0.1", port=0, udp=host_udp)
    await server.start()
    sink = FakeSink()
    session = AgentSession(sink, lambda e, x, y: None)
    client = AgentClient("laptop", "Laptop", "127.0.0.1", server.port, client_context(SECRET), session,
                         udp=agent_udp)
    task = asyncio.create_task(client.run_once())
    await wait_for(lambda: server.is_connected("laptop"))
    return server, client, task, sink


async def stop(server, task):
    await server.close()
    await asyncio.wait_for(task, 3)


def test_sealed_datagrams_open_only_with_their_key():
    keys, conn_id = udp.Keys(os.urandom(32)), os.urandom(8)
    packet = udp.seal(keys, conn_id, udp.MOVE, 7, 10.5, -3.0, 1234.25)
    assert udp.peek_id(packet) == conn_id
    assert udp.open_packet(keys, packet) == (udp.MOVE, 7, (10.5, -3.0, 1234.25))
    assert udp.open_packet(udp.Keys(os.urandom(32)), packet) is None
    tampered = bytearray(packet)
    tampered[udp.HEADER.size] ^= 1
    assert udp.open_packet(keys, bytes(tampered)) is None
    assert udp.open_packet(keys, packet[:-1]) is None
    # the position isn't readable on the wire
    assert udp.BODIES[udp.MOVE].pack(10.5, -3.0, 1234.25) not in packet


async def test_moves_go_by_udp_once_the_path_is_up():
    server, client, task, sink = await start()
    path = server.connections["laptop"].udp
    assert path is not None
    await wait_for(lambda: path.usable(server.udp.clock()))
    server.send("laptop", "enter", x=0, y=0, effect="none")
    await wait_for(lambda: client.session.active)
    for i in range(1, 6):
        server.send("laptop", "move", x=i, y=i * 2, ts=1000.0 + i)
    await wait_for(lambda: ("move", 5, 10) in sink.calls)
    assert client.udp.moves == 5 and path.sent == 5
    await stop(server, task)


async def test_a_click_after_a_lost_datagram_still_lands_where_the_pointer_is():
    server, client, task, sink = await start()
    path = server.connections["laptop"].udp
    await wait_for(lambda: path.usable(server.udp.clock()))
    server.send("laptop", "enter", x=0, y=0, effect="none")
    await wait_for(lambda: client.session.active)
    client.udp.on_move = lambda *a: None  # every datagram lost
    server.send("laptop", "move", x=40, y=50, ts=2000.0)
    server.send("laptop", "button", button="left", pressed=True)
    await wait_for(lambda: ("button", "left", True) in sink.calls)
    assert sink.calls[-2:] == [("move", 40, 50), ("button", "left", True)]
    await stop(server, task)


async def test_no_udp_for_an_agent_that_did_not_ask():
    server, client, task, sink = await start(agent_udp=False)
    assert server.connections["laptop"].udp is None and client.udp is None
    server.send("laptop", "enter", x=0, y=0, effect="none")
    server.send("laptop", "move", x=3, y=4, ts=10.0)
    await wait_for(lambda: ("move", 3, 4) in sink.calls)
    await stop(server, task)


async def test_host_with_udp_off_offers_none():
    server, client, task, sink = await start(host_udp=False)
    assert server.udp is None and client.udp is None
    await stop(server, task)


async def test_moves_fall_back_to_tcp_when_the_path_goes_quiet():
    server, client, task, sink = await start()
    path = server.connections["laptop"].udp
    await wait_for(lambda: path.usable(server.udp.clock()))
    client.udp.close()  # probes stop
    path.heard_at -= udp.ALIVE_S
    server.send("laptop", "enter", x=0, y=0, effect="none")
    server.send("laptop", "move", x=7, y=8, ts=3000.0)
    await wait_for(lambda: ("move", 7, 8) in sink.calls)
    assert path.sent == 0
    await stop(server, task)


def test_session_drops_late_and_early_moves():
    sink = FakeSink()
    session = AgentSession(sink, lambda e, x, y: None)
    session.udp_move(1, 1, 100.0)  # before enter: ignored
    assert sink.calls == []
    session.handle(Message("enter", {"x": 0, "y": 0, "effect": "none"}))
    session.udp_move(5, 5, 200.0)
    session.udp_move(4, 4, 150.0)  # arrived after a newer one
    session.handle(Message("move", {"x": 6, "y": 6, "ts": 200.0}))  # TCP copy of the newest: fine
    assert sink.calls == [("move", 0, 0), ("move", 5, 5), ("move", 6, 6)]
    session.new_connection()  # a restarted host's clock starts again
    session.udp_move(9, 9, 1.0)
    assert sink.calls[-1] == ("move", 9, 9)


async def test_forged_probes_are_ignored():
    server, client, task, sink = await start(agent_udp=False)
    path = server.udp.add("laptop")
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(asyncio.DatagramProtocol,
                                                       remote_addr=("127.0.0.1", server.udp.port))
    forged = udp.seal(udp.Keys(os.urandom(32)), path.conn_id, udp.PROBE, 1, 1)
    transport.sendto(forged)
    await asyncio.sleep(0.1)
    assert path.addr is None
    transport.close()
    await stop(server, task)


async def test_a_picked_port_taken_for_udp_is_swapped_for_another(monkeypatch):
    from curslink.net import server as server_mod

    loop = asyncio.get_running_loop()
    real = loop.create_datagram_endpoint
    tries = []

    async def flaky(*a, **kw):
        tries.append(kw.get("local_addr"))
        if len(tries) == 1:
            raise OSError(10048, "only one usage of each socket address")  # Windows: UDP twin taken
        return await real(*a, **kw)

    monkeypatch.setattr(loop, "create_datagram_endpoint", flaky)
    server = server_mod.HostServer({"laptop"}, server_context(SECRET), host="127.0.0.1", port=0)
    await server.start()
    assert len(tries) == 2 and server.udp is not None and server.udp.port == server.port
    await server.close()
