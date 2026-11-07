"""Pairing: the code-confirmed handshake, the store, and paired TLS connections."""

import asyncio
import json
import os
import sys

import pytest

from conftest import FakeSink
from curslink import cli
from curslink.net.client import AgentClient, AgentSession
from curslink.net.server import HostServer
from curslink.pairing import handshake
from curslink.pairing.handshake import DeviceInfo, PairingError, initiate
from curslink.pairing.service import PairingListener, dialog_command
from curslink.pairing.store import PairingStore
from curslink.security import HelloVerifier, client_context, server_context

HOST = DeviceInfo("desktop", "Main PC", "host")
AGENT = DeviceInfo("laptop", "Work laptop", "agent")


async def pair(tmp_path, typed=None, me=HOST, them=AGENT, listener_kwargs=None):
    """Runs one pairing from ``me`` to a listener for ``them``. ``typed(code)`` is what
    the user types on the recipient (default: the right code)."""
    shown = []
    their_store = PairingStore(tmp_path / f"{them.device_id}.json")

    async def ask(peer):
        await asyncio.sleep(0)
        code = shown[0] if shown else ""
        return typed(code) if typed else code

    listener = PairingListener(them, their_store, ask, host="127.0.0.1", port=0, **(listener_kwargs or {}))
    await listener.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", listener.port)
        try:
            result = await initiate(reader, writer, me, lambda code, peer: shown.append(code))
        finally:
            writer.close()
        await asyncio.sleep(0.05)
        return result, their_store, shown, listener
    finally:
        await listener.close()


async def test_pairing_gives_both_sides_the_same_key(tmp_path):
    result, their_store, shown, _ = await pair(tmp_path)
    assert result.peer == AGENT
    assert len(shown) == 1 and len(shown[0]) == 7  # "123 456"
    stored = their_store.get("desktop")
    assert stored.key == result.key and stored.role == "host" and stored.name == "Main PC"


async def test_wrong_code_is_refused_and_nothing_is_stored(tmp_path):
    def wrong(code):
        digits = code.replace(" ", "")
        return f"{(int(digits) + 1) % 10**6:06d}"

    with pytest.raises(PairingError, match="wrong code"):
        await pair(tmp_path, typed=wrong)
    assert PairingStore(tmp_path / "laptop.json").peers() == []


async def test_declined(tmp_path):
    with pytest.raises(PairingError, match="declined"):
        await pair(tmp_path, typed=lambda code: None)


async def test_two_agents_cannot_pair(tmp_path):
    other = DeviceInfo("tablet", "Tablet", "agent")
    with pytest.raises(PairingError, match="both devices are agents"):
        await pair(tmp_path, me=other)


async def test_repeated_failures_lock_pairing(tmp_path, monkeypatch):
    store = PairingStore(tmp_path / "laptop.json")
    listener = PairingListener(AGENT, store, lambda peer: asyncio.sleep(0, "000000"), host="127.0.0.1", port=0)
    await listener.start()
    reasons = []
    try:
        for _ in range(4):
            reader, writer = await asyncio.open_connection("127.0.0.1", listener.port)
            try:
                await initiate(reader, writer, HOST, lambda c, p: None)
            except PairingError as exc:
                reasons.append(str(exc))
            finally:
                writer.close()
            await asyncio.sleep(0.02)
    finally:
        await listener.close()
    # (a correct guess of 000000 is a one in a million chance)
    assert reasons[:3] == ["wrong code"] * 3
    assert "too many failed attempts" in reasons[3]


async def test_tampered_public_value_is_caught(tmp_path, monkeypatch):
    """A man in the middle swapping the initiator's key breaks its commitment."""
    real = handshake._keypair
    calls = []

    def keypair():
        x, pub = real()
        calls.append(pub)
        return x, pub

    monkeypatch.setattr(handshake, "_keypair", keypair)
    real_commit = handshake._commit
    # The initiator commits to one key but reveals another. (Flip a bit
    # rather than zeroing the first byte, which is already zero about one
    # run in 256 and then changes nothing.)
    monkeypatch.setattr(handshake, "_commit", lambda pub: real_commit(bytes([pub[0] ^ 1]) + pub[1:]) if len(calls) == 1 else real_commit(pub))
    with pytest.raises(PairingError):
        await pair(tmp_path)


def test_invalid_public_values_rejected():
    for bad in ("00" * 256, "01".rjust(512, "0"), f"{handshake.P - 1:0512x}", "zz", 7):
        with pytest.raises(PairingError):
            handshake._peer_value(bad)


def test_code_helpers():
    assert handshake.format_code("123456") == "123 456"
    assert handshake.normalise_code(" 123-456 ") == "123456"


# ------------------------------------------------------------------ store
def test_store_round_trip_and_permissions(tmp_path):
    path = tmp_path / "sub" / "pairings.json"
    store = PairingStore(path)
    store.add("laptop", "Work laptop", "agent", b"k" * 32)
    again = PairingStore(path)
    assert again.key("laptop") == b"k" * 32
    assert [p.device_id for p in again.peers("agent")] == ["laptop"]
    assert again.peers("host") == []
    if sys.platform != "win32":
        assert os.stat(path).st_mode & 0o077 == 0
    assert again.remove("laptop") and not again.remove("laptop")
    assert PairingStore(path).peers() == []


def test_store_sees_changes_from_other_processes(tmp_path):
    path = tmp_path / "pairings.json"
    running = PairingStore(path)
    assert running.key("laptop") is None
    PairingStore(path).add("laptop", "Laptop", "agent", b"x" * 32)
    assert running.key("laptop") == b"x" * 32


def test_store_skips_broken_entries(tmp_path):
    path = tmp_path / "pairings.json"
    path.write_text(json.dumps({"v": 1, "peers": {"a": {"key": "short"}, "b": {"name": "no key"}}}))
    assert PairingStore(path).peers() == []
    with pytest.raises(ValueError):
        store = PairingStore(path)
        path.write_text("{not json")
        store.peers()


def test_store_rejects_bad_records(tmp_path):
    store = PairingStore(tmp_path / "p.json")
    with pytest.raises(ValueError):
        store.add("x", "X", "phone", b"k" * 32)
    with pytest.raises(ValueError):
        store.add("x", "X", "agent", b"short")


# ---------------------------------------------------------- paired TLS
async def connect(host_store, agent_id, key, secret=None, auth_key="same"):
    server = HostServer(
        {"laptop", "tablet"},
        server_context(secret, host_store.key),
        host="127.0.0.1",
        port=0,
        verify_hello=HelloVerifier(secret, host_store.key),
    )
    await server.start()
    sink = FakeSink()
    session = AgentSession(sink, lambda *a: None)
    ctx = client_context(device_id=agent_id, key=key) if key else client_context(secret)
    client = AgentClient(agent_id, agent_id, "127.0.0.1", server.port, ctx, session,
                         auth_key=key if auth_key == "same" else auth_key)
    task = asyncio.create_task(client.run_once())
    for _ in range(100):
        if server.connections or task.done():
            break
        await asyncio.sleep(0.02)
    connected = set(server.connections)
    task.cancel()
    await server.close()
    return connected


async def test_paired_agent_connects_without_a_secret(tmp_path):
    store = PairingStore(tmp_path / "host.json")
    store.add("laptop", "Laptop", "agent", b"L" * 32)
    assert await connect(store, "laptop", b"L" * 32) == {"laptop"}


async def test_unpaired_or_wrong_key_cannot_connect(tmp_path):
    store = PairingStore(tmp_path / "host.json")
    store.add("laptop", "Laptop", "agent", b"L" * 32)
    assert await connect(store, "laptop", b"W" * 32) == set()
    assert await connect(store, "tablet", b"T" * 32) == set()


async def test_paired_device_cannot_claim_another_id(tmp_path):
    """tablet completes TLS with its own key, then says it is laptop."""
    store = PairingStore(tmp_path / "host.json")
    store.add("laptop", "Laptop", "agent", b"L" * 32)
    store.add("tablet", "Tablet", "agent", b"T" * 32)
    server = HostServer({"laptop", "tablet"}, server_context(None, store.key), host="127.0.0.1",
                        port=0, verify_hello=HelloVerifier(None, store.key))
    await server.start()
    session = AgentSession(FakeSink(), lambda *a: None)
    client = AgentClient("laptop", "x", "127.0.0.1", server.port,
                         client_context(device_id="tablet", key=b"T" * 32), session, auth_key=b"T" * 32)
    with pytest.raises(Exception):
        await asyncio.wait_for(client.run_once(), 3)
    assert server.connections == {}
    await server.close()


async def test_shared_secret_still_works_alongside_pairing(tmp_path):
    store = PairingStore(tmp_path / "host.json")
    secret = "correct horse battery staple"
    # an agent from before pairing existed: no auth in hello
    assert await connect(store, "tablet", None, secret=secret, auth_key=None) == {"tablet"}
    # a current agent on the shared secret proves it with the secret's key
    from curslink.security import derive_key

    assert await connect(store, "tablet", None, secret=secret, auth_key=derive_key(secret)) == {"tablet"}


def test_hello_verifier_rules():
    paired = {"laptop": b"L" * 32}
    verify = HelloVerifier("correct horse battery staple", paired.get)
    # no auth: only for unpaired devices on the shared secret
    assert verify("tablet", None, b"bind")
    assert not verify("laptop", None, b"bind")
    assert not HelloVerifier(None, paired.get)("tablet", None, b"bind")
    from curslink.security import hello_auth

    assert verify("laptop", hello_auth(b"L" * 32, b"bind", "laptop"), b"bind")
    assert not verify("laptop", hello_auth(b"L" * 32, b"other", "laptop"), b"bind")


# ------------------------------------------------------------------- CLI
def test_pair_role_comes_from_the_config(tmp_path):
    cfg = tmp_path / "curslink.toml"
    cfg.write_text('[agent]\ndevice_id = "laptop"\nname = "Work laptop"\n')
    args = cli.build_parser().parse_args(["pair", "-c", str(cfg)])
    me, pcfg = cli._this_device(args)
    assert me == DeviceInfo("laptop", "Work laptop", "agent") and pcfg.enabled


def test_pair_needs_a_role_when_config_has_both():
    from pathlib import Path

    from curslink.config import ConfigError

    example = str(Path(__file__).parent.parent / "examples" / "curslink.toml")
    with pytest.raises(ConfigError, match="--as"):
        cli._this_device(cli.build_parser().parse_args(["pair", "-c", example]))
    me, _ = cli._this_device(cli.build_parser().parse_args(["pair", "-c", example, "--as", "host"]))
    assert me == DeviceInfo("desktop", "Main PC", "host")


def test_pair_dialog_command(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert dialog_command(AGENT)[:3] == [sys.executable, "-m", "curslink"]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert dialog_command(AGENT) == [sys.executable, "_pair-dialog", "Work laptop", "agent", "laptop"]


def test_hidden_pair_dialog_command(monkeypatch):
    from curslink.pairing import dialog

    monkeypatch.setattr(dialog, "run", lambda *a: 7 if a == ("Main PC", "host", "desktop") else 0)
    assert cli.main(["_pair-dialog", "Main PC", "host", "desktop"]) == 7


def test_store_remembers_where_a_peer_was(tmp_path):
    store = PairingStore(tmp_path / "pairings.json")
    store.add("mac", "Mac", "host", b"k" * 32, "192.168.1.5")
    assert PairingStore(store.path).get("mac").address == "192.168.1.5"
    store.set_address("mac", "fe80::1%en0")
    assert PairingStore(store.path).get("mac").address == "fe80::1"
    store.add("pc", "PC", "agent", b"k" * 32)
    assert "address" not in json.loads(store.path.read_text())["peers"]["pc"]


async def test_agent_falls_back_to_the_paired_address(tmp_path, monkeypatch):
    from curslink import app
    from curslink.config import agent_config
    from curslink.pairing import discovery

    async def nothing(device_id, timeout=3.0):
        return None

    monkeypatch.setattr(discovery, "find", nothing)
    store = PairingStore(tmp_path / "pairings.json")
    store.add("mac", "Mac", "host", b"k" * 32, "192.168.1.5")
    cfg = agent_config({"agent": {"device_id": "pc", "name": "PC"}})
    target = await app.resolve_host(cfg, store)
    assert (target.host, target.port) == ("192.168.1.5", cfg.port)


async def test_agent_tries_the_last_good_address_first(tmp_path, monkeypatch):
    """The Mac grew a second address (.72) that doesn't answer; the one that
    worked last time (.52) is tried first, and if nothing answers it's the
    one used, not whichever the host listed first."""
    from curslink import app
    from curslink.config import agent_config
    from curslink.pairing import discovery

    async def found(device_id, timeout=3.0):
        return discovery.Found("mac", "Mac", "host", ("192.168.68.72", "192.168.68.52"), 24851, 24850)

    tried = []

    async def reachable(addresses, port, timeout=1.0, ssl=None):
        tried.append(list(addresses))
        return answer["addr"]

    answer = {"addr": "192.168.68.52"}
    monkeypatch.setattr(discovery, "find", found)
    monkeypatch.setattr(discovery, "reachable", reachable)
    store = PairingStore(tmp_path / "pairings.json")
    store.add("mac", "Mac", "host", b"k" * 32, "192.168.68.52")
    cfg = agent_config({"agent": {"device_id": "pc", "name": "PC"}})
    assert (await app.resolve_host(cfg, store)).host == "192.168.68.52"
    assert tried[0] == ["192.168.68.52", "192.168.68.72"]
    answer["addr"] = None
    assert (await app.resolve_host(cfg, store)).host == "192.168.68.52"


# ---------------------------------------------------------------- QR codes
async def _scan(qr, url, me=AGENT, about=None, token=None):
    from curslink.pairing.qr import parse_invite

    invite = parse_invite(url)
    reader, writer = await asyncio.open_connection("127.0.0.1", invite["pairing_port"])
    try:
        return await initiate(reader, writer, me, lambda code, peer: None,
                              token=invite["token"] if token is None else token, about=about)
    finally:
        writer.close()


async def test_qr_pairing_needs_no_code_and_carries_screens():
    from curslink.pairing.qr import QrPairing, parse_invite

    got = []
    qr = QrPairing(HOST, lambda result, address: got.append((result, address)), host="127.0.0.1")
    await qr.start()
    try:
        url = qr.url(["127.0.0.1"], 24850, "relay.example.com:24852", "k" * 20)
        invite = parse_invite(url)
        assert invite["id"] == "desktop" and invite["control_port"] == 24850
        assert invite["relay"] == "relay.example.com:24852" and invite["relay_key"] == "k" * 20
        screens = [{"x": 0, "y": 0, "width": 411, "height": 914, "name": "phone"}]
        result = await _scan(qr, url, about={"screens": screens, "platform": "Android 15"})
        await asyncio.sleep(0.05)
    finally:
        await qr.close()
    assert result.peer == HOST
    (theirs, address), = got
    assert theirs.key == result.key and theirs.peer == AGENT and address == "127.0.0.1"
    assert theirs.extra == {"screens": screens, "platform": "Android 15"}


async def test_qr_pairing_refuses_a_wrong_token_and_works_once():
    from curslink.pairing.qr import QrPairing

    got = []
    qr = QrPairing(HOST, lambda result, address: got.append(result), host="127.0.0.1")
    await qr.start()
    try:
        url = qr.url(["127.0.0.1"], 24850)
        with pytest.raises(PairingError, match="expired"):
            await _scan(qr, url, token=b"x" * 16)
        assert not got
        await _scan(qr, url)
        await asyncio.sleep(0.05)
        assert len(got) == 1
        with pytest.raises(PairingError, match="expired"):
            await _scan(qr, url)
    finally:
        await qr.close()


async def test_a_qr_request_to_the_usual_listener_is_refused(tmp_path):
    store = PairingStore(tmp_path / "s.json")

    async def ask(peer):
        raise AssertionError("no code should be asked for")

    listener = PairingListener(HOST, store, ask, host="127.0.0.1", port=0)
    await listener.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", listener.port)
        with pytest.raises(PairingError, match="QR"):
            await initiate(reader, writer, AGENT, lambda c, p: None, token=b"t" * 16)
        writer.close()
    finally:
        await listener.close()
