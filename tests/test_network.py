"""End to end over real sockets with real TLS-PSK, fake OS input."""

import asyncio
import ssl

import pytest

from conftest import FakeSink
from curslink.net.client import AgentClient, AgentSession
from curslink.net.server import HostServer
from curslink.protocol import Message, encode
from curslink.security import WeakSecretError, client_context, derive_key, server_context

SECRET = "correct horse battery staple"


async def start(secret_server=SECRET, secret_client=SECRET, device="laptop"):
    disconnected = []
    server = HostServer({"laptop"}, server_context(secret_server), host="127.0.0.1", port=0,
                        on_disconnect=disconnected.append)
    await server.start()
    sink, effects = FakeSink(), []
    session = AgentSession(sink, lambda e, x, y: effects.append((e, x, y)))
    client = AgentClient(device, "Laptop", "127.0.0.1", server.port, client_context(secret_client), session)
    task = asyncio.create_task(client.run_once())
    return server, client, task, sink, effects, disconnected


async def wait_for(cond, timeout=3.0):
    for _ in range(int(timeout / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met")


async def test_forwarding_end_to_end():
    server, client, task, sink, effects, disconnected = await start()
    await wait_for(lambda: server.is_connected("laptop"))
    server.send("laptop", "enter", x=10, y=20, effect="pop")
    server.send("laptop", "move", x=11, y=21)
    server.send("laptop", "key", key="a", pressed=True)
    server.send("laptop", "button", button="left", pressed=True)
    await wait_for(lambda: len(sink.calls) >= 4)
    assert sink.calls[:4] == [("move", 10, 20), ("move", 11, 21), ("key", "a", True), ("button", "left", True)]
    assert effects == [("pop", 10, 20)]
    # dropping the connection releases everything still held on the agent
    await server.close()
    await asyncio.wait_for(task, 3)
    assert ("key", "a", False) in sink.calls and ("button", "left", False) in sink.calls
    assert disconnected == ["laptop"]


async def test_look_reaches_an_agent_that_says_it_understands_it():
    server, client, task, sink, effects, disconnected = await start()
    looks = []
    client.session.on_look = looks.append
    await wait_for(lambda: server.is_connected("laptop"))
    assert server.supports("laptop", "look")
    server.send("laptop", "look", screen="main", step="Step 1 of 2")
    server.send("laptop", "look", hide=True)
    await wait_for(lambda: len(looks) >= 2)
    assert looks == [{"screen": "main", "step": "Step 1 of 2"}, {"hide": True}]
    await server.close()
    await asyncio.wait_for(task, 3)


async def test_wrong_secret_cannot_connect():
    server, client, task, sink, *_ = await start(secret_client="a different secret!")
    with pytest.raises((ssl.SSLError, ConnectionError, OSError)):
        await asyncio.wait_for(task, 3)
    assert not server.is_connected("laptop")
    await server.close()


async def test_unknown_device_rejected():
    server, client, task, *_ = await start(device="intruder")
    with pytest.raises(Exception):
        await asyncio.wait_for(task, 3)
    assert server.connections == {}
    await server.close()


def test_weak_secret_rejected():
    with pytest.raises(WeakSecretError):
        derive_key("short")


def test_agent_ignores_release_without_press():
    sink = FakeSink()
    session = AgentSession(sink, lambda *a: None)
    session.handle(Message("key", {"key": "ctrl_l", "pressed": False}))
    assert sink.calls == []
    assert session.handle(Message("ping", {"n": 3})) == encode("pong", n=3)


def test_an_agent_can_ask_for_slower_pings():
    from curslink.net.server import _ping_interval

    assert _ping_interval(None) == 0.5
    assert _ping_interval(5) == 5.0
    assert _ping_interval(0.01) == 0.5
    assert _ping_interval(3600) == 15.0
    assert _ping_interval(True) == 0.5
