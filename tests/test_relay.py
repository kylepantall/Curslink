"""Host and agents meeting through a relay, over real sockets and real TLS."""

import asyncio

import pytest

from conftest import FakeSink
from curslink.config import ConfigError, agent_config, host_config, load_raw, relay_server_config
from curslink.net import relay as relay_mod
from curslink.net.client import AgentClient, AgentSession
from curslink.net.relay import (
    HostRelayLink,
    RelayError,
    RelayServer,
    RelayTarget,
    decode_pairing_code,
    encode_pairing_code,
    parse_address,
    relay_connect,
    room_id,
)
from curslink.net.server import HostServer
from curslink.security import client_context, derive_key, server_context
from test_config import EXAMPLE
from test_network import wait_for

SECRET = "correct horse battery staple"
RELAY_KEY = "relay-key-for-tests-0123456789"


async def setup(devices=("laptop",), pair_timeout_s=5.0):
    relay = RelayServer(RELAY_KEY, host="127.0.0.1", port=0, pair_timeout_s=pair_timeout_s)
    await relay.start()
    target = RelayTarget(f"127.0.0.1:{relay.port}", RELAY_KEY, room_id(derive_key(SECRET)))
    tls = server_context(SECRET)
    server = HostServer(set(devices), tls)  # LAN listener never started
    link = HostRelayLink(target, tls, server.handle_streams)
    link.start()
    return relay, target, server, link


def make_agent(device, target, secret=SECRET, host=""):
    sink = FakeSink()
    session = AgentSession(sink, lambda *a: None)
    client = AgentClient(device, device, host, 1, client_context(secret), session, relay=target)
    return client, sink


async def teardown(relay, server, link, *tasks):
    for t in tasks:
        t.cancel()
    await link.close()
    await server.close()
    await relay.close()


async def test_input_flows_end_to_end_through_relay():
    relay, target, server, link = await setup(devices=("laptop", "tablet"))
    laptop, laptop_sink = make_agent("laptop", target)
    tablet, tablet_sink = make_agent("tablet", target)
    tasks = [asyncio.create_task(laptop.run_forever()), asyncio.create_task(tablet.run_forever())]
    await wait_for(lambda: server.is_connected("laptop") and server.is_connected("tablet"), 5)
    assert relay.active_pipes == 2
    server.send("laptop", "move", x=5, y=6)
    server.send("tablet", "key", key="b", pressed=True)
    await wait_for(lambda: laptop_sink.calls and tablet_sink.calls)
    assert laptop_sink.calls[0] == ("move", 5, 6)
    assert tablet_sink.calls[0] == ("key", "b", True)
    assert laptop.via.startswith("relay ")
    await teardown(relay, server, link, *tasks)


async def test_relay_only_sees_ciphertext(monkeypatch):
    seen = bytearray()
    real_pipe = relay_mod._pipe

    async def spy(reader, writer):
        real_write = writer.write

        def write(data):
            seen.extend(data)
            real_write(data)

        writer.write = write
        await real_pipe(reader, writer)

    monkeypatch.setattr(relay_mod, "_pipe", spy)
    relay, target, server, link = await setup()
    client, sink = make_agent("laptop", target)
    task = asyncio.create_task(client.run_forever())
    await wait_for(lambda: server.is_connected("laptop"), 5)
    server.send("laptop", "key", key="super-secret-keystroke", pressed=True)
    await wait_for(lambda: sink.calls)
    assert seen and b"super-secret-keystroke" not in seen and b"laptop" not in seen
    await teardown(relay, server, link, task)


async def test_wrong_relay_key_is_refused():
    relay = RelayServer(RELAY_KEY, host="127.0.0.1", port=0)
    await relay.start()
    with pytest.raises(RelayError):
        await relay_connect(f"127.0.0.1:{relay.port}", "not-the-relay-key-at-all", "ab" * 32, "agent")
    await relay.close()


async def test_agent_with_other_secret_never_meets_host():
    relay, target, server, link = await setup(pair_timeout_s=0.3)
    stranger = RelayTarget(target.address, RELAY_KEY, room_id(derive_key("some other pairing secret")))
    client, _ = make_agent("laptop", stranger, secret="some other pairing secret")
    with pytest.raises(RelayError, match="not connected"):
        await asyncio.wait_for(client.run_once(), 5)
    assert not server.is_connected("laptop")
    await teardown(relay, server, link)


async def test_agent_waits_for_host_to_arrive():
    relay = RelayServer(RELAY_KEY, host="127.0.0.1", port=0)
    await relay.start()
    target = RelayTarget(f"127.0.0.1:{relay.port}", RELAY_KEY, room_id(derive_key(SECRET)))
    client, sink = make_agent("laptop", target)
    task = asyncio.create_task(client.run_once())
    await wait_for(lambda: relay.agents)
    tls = server_context(SECRET)
    server = HostServer({"laptop"}, tls)
    link = HostRelayLink(target, tls, server.handle_streams)
    link.start()
    await wait_for(lambda: server.is_connected("laptop"), 5)
    await teardown(relay, server, link, task)


async def test_agent_falls_back_from_lan_to_relay():
    relay, target, server, link = await setup()
    # nothing listens on 127.0.0.1:1, so the LAN attempt fails fast
    client, _ = make_agent("laptop", target, host="127.0.0.1")
    task = asyncio.create_task(client.run_forever())
    await wait_for(lambda: server.is_connected("laptop"), 5)
    assert client.via.startswith("relay ")
    await teardown(relay, server, link, task)


def test_pairing_code_round_trip():
    code = encode_pairing_code("relay.example.com:24852", RELAY_KEY, SECRET)
    assert decode_pairing_code(code) == {
        "relay": "relay.example.com:24852",
        "relay_key": RELAY_KEY,
        "secret": SECRET,
    }
    with pytest.raises(ValueError):
        decode_pairing_code(code[:-6])


def test_parse_address():
    assert parse_address("relay.example.com") == ("relay.example.com", 24852)
    assert parse_address("relay.example.com:443") == ("relay.example.com", 443)
    assert parse_address("[2001:db8::1]:9000") == ("2001:db8::1", 9000)
    assert parse_address("2001:db8::1") == ("2001:db8::1", 24852)
    for bad in ("", "host:notaport", "host:70000"):
        with pytest.raises(ValueError):
            parse_address(bad)


def test_config_from_pairing_code(monkeypatch):
    monkeypatch.delenv("CURSLINK_SECRET", raising=False)
    raw = load_raw(EXAMPLE)
    raw.pop("secret", None)
    raw["agent"].pop("host", None)
    raw["relay"] = {"code": encode_pairing_code("relay.example.com", RELAY_KEY, SECRET)}
    host = host_config(raw)
    agent = agent_config(raw)
    assert host.secret == agent.secret == SECRET
    assert host.relay == agent.relay
    assert agent.relay.address == "relay.example.com" and agent.host == ""


def test_config_needs_lan_or_relay():
    raw = load_raw(EXAMPLE)
    raw["agent"].pop("host", None)
    raw["pairing"] = {"enabled": False}  # with pairing on, the host is found on the LAN
    with pytest.raises(ConfigError):
        agent_config(raw)
    raw["host"]["lan"] = False
    with pytest.raises(ConfigError):
        host_config(raw)
    raw["relay"] = {"address": "relay.example.com"}
    with pytest.raises(ConfigError, match="both"):
        host_config(raw)


def test_relay_server_config(monkeypatch):
    monkeypatch.delenv("CURSLINK_RELAY_KEY", raising=False)
    with pytest.raises(ConfigError):
        relay_server_config({"relay": {"key": "short"}})
    cfg = relay_server_config({"relay": {"key": RELAY_KEY, "port": 9000}})
    assert cfg.port == 9000 and cfg.key == RELAY_KEY


def test_cli_relay_init_and_invite(tmp_path, capsys, monkeypatch):
    from curslink import cli

    monkeypatch.delenv("CURSLINK_RELAY_KEY", raising=False)
    path = tmp_path / "relay.toml"
    assert cli.main(["relay", "--init", "-c", str(path)]) == 0
    cfg = relay_server_config(load_raw(path))
    assert cli.main(["relay", "--init", "-c", str(path)]) == 1  # never overwrites
    capsys.readouterr()
    assert cli.main(["invite", "--relay", "relay.example.com", "--relay-key", cfg.key]) == 0
    code = next(w for w in capsys.readouterr().out.split() if w.startswith("curslink1-"))
    decoded = decode_pairing_code(code)
    assert decoded["relay_key"] == cfg.key and len(decoded["secret"]) >= 32


# --------------------------------------------- paired devices over a relay
async def test_paired_agents_meet_the_host_in_per_pair_rooms(tmp_path):
    """No shared secret anywhere: each paired agent gets its own relay room."""
    from curslink.app import HostRelays, resolve_host
    from curslink.config import AgentConfig
    from curslink.pairing.store import PairingStore
    from curslink.security import HelloVerifier

    relay = RelayServer(RELAY_KEY, host="127.0.0.1", port=0)
    await relay.start()
    base = RelayTarget(f"127.0.0.1:{relay.port}", RELAY_KEY, "")
    host_store = PairingStore(tmp_path / "host.json")
    host_store.add("laptop", "Laptop", "agent", b"L" * 32)
    tls = server_context(None, host_store.key)
    server = HostServer({"laptop", "tablet"}, tls, verify_hello=HelloVerifier(None, host_store.key))
    relays = HostRelays(base, "desktop", False, host_store, tls, server.handle_streams, refresh_s=0.05)
    relays.start()

    tasks = []
    sinks = {}
    for device, key in (("laptop", b"L" * 32), ("tablet", b"T" * 32)):
        store = PairingStore(tmp_path / f"{device}.json")
        store.add("desktop", "Desktop", "host", key)
        cfg = AgentConfig(device_id=device, secret=None, relay=base)
        cfg.pairing.enabled = False
        sinks[device] = FakeSink()
        client = AgentClient(device, device, "", 1, None, AgentSession(sinks[device], lambda *a: None),
                             resolve=lambda cfg=cfg, store=store: resolve_host(cfg, store))
        tasks.append(asyncio.create_task(client.run_forever()))

    await wait_for(lambda: server.is_connected("laptop"), 5)
    assert not server.is_connected("tablet")  # not paired with this host yet
    # pairing the tablet later opens its room without a restart
    host_store.add("tablet", "Tablet", "agent", b"T" * 32)
    await wait_for(lambda: server.is_connected("tablet"), 5)
    assert len(relays.links) == 2
    server.send("tablet", "move", x=1, y=2)
    await wait_for(lambda: sinks["tablet"].calls)
    for t in tasks:
        t.cancel()
    await relays.close()
    await server.close()
    await relay.close()


def test_paired_rooms_differ_per_pair_and_host():
    from curslink.net.relay import paired_room_id

    assert paired_room_id(b"a" * 32, "desk") != paired_room_id(b"b" * 32, "desk")
    assert paired_room_id(b"a" * 32, "desk") != paired_room_id(b"a" * 32, "office")
    assert paired_room_id(b"a" * 32, "desk") != room_id(b"a" * 32)
