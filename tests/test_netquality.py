"""Network diagnostics and the optimiser (curslink.net.quality, curslink.net.check)."""

import asyncio
import os
import socket

from conftest import FakeSink
from curslink import app as app_mod
from curslink.config import agent_config, network_optimise
from curslink.net import check, quality, udp
from curslink.net.client import AgentClient, AgentSession
from curslink.net.server import HostServer
from curslink.protocol import Message, ProtocolError, decode, encode
from curslink.security import client_context, server_context

SECRET = "correct horse battery staple"


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


async def wait_for(cond, timeout=3.0):
    for _ in range(int(timeout / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met")


# ------------------------------------------------------------- measuring
def test_rtt_window_reports_percentiles_and_jitter_over_the_last_minute():
    clock = Clock()
    w = quality.RttWindow(clock=clock)
    assert w.stats()["samples"] == 0
    for rtt in (0.010, 0.020, 0.010, 0.020):
        w.add(rtt)
    s = w.stats()
    assert s["samples"] == 4 and s["p50_ms"] == 20.0 and s["max_ms"] == 20.0
    assert s["jitter_ms"] == 10.0
    clock.t += 61
    w.add(0.005)
    assert w.stats()["samples"] == 1


def test_seq_loss_counts_gaps_and_ignores_repeats():
    clock = Clock()
    loss = quality.SeqLoss(clock=clock)
    assert loss.stats()["loss_pct"] is None
    for seq in (1, 2, 3, 3, 2, 6, 7):   # 4 and 5 lost, repeats ignored
        loss.saw(seq)
    s = loss.stats()
    assert (s["received"], s["lost"]) == (5, 2)
    assert s["loss_pct"] == round(200 / 7, 2)
    clock.t += 31
    assert loss.stats()["loss_pct"] is None


def test_grades_and_tips():
    good = {"samples": 20, "p50_ms": 5, "p95_ms": 10, "jitter_ms": 1}
    assert quality.grade(good, 0.0) == "good"
    assert quality.grade({**good, "p95_ms": 80}, 0) == "fair"
    assert quality.grade(good, 4.0) == "poor"
    assert quality.grade({"samples": 0}, None) == "unknown"
    tips = quality.advice({"path": "relay", "rtt": good, "optimise": True})
    assert "relay" in tips[0] and "by itself" in tips[0]
    tips = quality.advice({"path": "tcp", "udp_offered": True, "rtt": good})
    assert any("UDP 24850" in t for t in tips)
    tips = quality.advice({"path": "udp", "rtt": {**good, "p95_ms": 200}, "interface": {"kind": "wifi"}})
    assert any("Ethernet" in t for t in tips)
    assert quality.advice({"path": "udp", "rtt": good, "loss_pct": 0}) == []


# ------------------------------------------------------------- optimiser
def test_optimiser_sends_moves_twice_on_a_lossy_link_and_stops_when_it_recovers():
    clock = Clock()
    opt = quality.Optimiser(clock=clock)
    rtt = {"samples": 30, "p95_ms": 20, "jitter_ms": 2}
    assert opt.update(rtt, 0.0, udp=True) == quality.Tuning()
    t = opt.update(rtt, 2.0, udp=True)
    assert t.copies == 2 and "twice" in t.reason
    # held: a better reading straight after changes nothing
    clock.t += 5
    assert opt.update(rtt, 0.0, udp=True).copies == 2
    # between the on and off thresholds it stays on
    clock.t += quality.HOLD_S
    assert opt.update(rtt, 0.6, udp=True).copies == 2
    assert opt.update(rtt, 0.1, udp=True).copies == 1


def test_optimiser_never_doubles_moves_over_tcp_or_to_an_old_agent():
    opt = quality.Optimiser(clock=Clock())
    rtt = {"samples": 30, "p95_ms": 20, "jitter_ms": 2}
    assert opt.update(rtt, 10.0, udp=False).copies == 1
    opt = quality.Optimiser(clock=Clock())
    assert opt.update(rtt, 10.0, udp=True, copies_ok=False).copies == 1


def test_optimiser_paces_moves_on_busy_and_slow_links():
    clock = Clock()
    opt = quality.Optimiser(clock=clock)
    assert opt.update({"samples": 30, "p95_ms": 90, "jitter_ms": 5}, 0, udp=True).move_interval_s \
        == quality.FAST_INTERVAL_S
    clock.t += quality.HOLD_S
    assert opt.update({"samples": 30, "p95_ms": 300, "jitter_ms": 5}, 0, udp=True).move_interval_s \
        == quality.POOR_INTERVAL_S
    clock.t += quality.HOLD_S
    # better, but not good enough to leave "poor"
    assert opt.level == "poor"
    assert opt.update({"samples": 30, "p95_ms": 100, "jitter_ms": 5}, 0, udp=True).move_interval_s \
        == quality.POOR_INTERVAL_S
    assert opt.update({"samples": 30, "p95_ms": 10, "jitter_ms": 1}, 0, udp=True).move_interval_s == 0.0


def test_optimiser_off_or_without_enough_samples_does_nothing():
    rtt = {"samples": 30, "p95_ms": 500, "jitter_ms": 50}
    assert quality.Optimiser(enabled=False).update(rtt, 20, udp=True) == quality.Tuning()
    assert quality.Optimiser().update({**rtt, "samples": 3}, 20, udp=True) == quality.Tuning()


def test_low_latency_marking_sets_dscp_ef():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        assert quality.mark_low_latency(s)
        assert s.getsockopt(socket.IPPROTO_IP, socket.IP_TOS) == quality.DSCP_EF
    finally:
        s.close()
    assert quality.mark_low_latency(None) is False


# --------------------------------------------------------------- protocol
def test_netstats_frames_validate():
    msg = decode(encode("netstats", udp=True, loss_pct=1.5, rtt_ms=None, jitter_ms=2))
    assert msg == Message("netstats", {"udp": True, "loss_pct": 1.5, "rtt_ms": None, "jitter_ms": 2})
    for bad in ({"udp": "yes"}, {"udp": True, "loss_pct": "1"}):
        try:
            decode(encode("netstats", **bad))
        except ProtocolError:
            continue
        raise AssertionError(f"accepted {bad}")


def test_a_second_copy_is_taken_only_when_the_first_was_lost():
    key, conn_id = os.urandom(32), os.urandom(8)
    keys = udp.Keys(key)
    moves = []
    agent = udp.AgentUdp({"port": 1, "id": udp.b64(conn_id), "key": udp.b64(key)}, lambda *m: moves.append(m))
    agent.datagram_received(udp.seal(keys, conn_id, udp.MOVE, 1, 1.0, 1.0, 1.0), None)
    agent.datagram_received(udp.seal(keys, conn_id, udp.MOVE2, 1, 1.0, 1.0, 1.0), None)  # repeat: dropped
    agent.datagram_received(udp.seal(keys, conn_id, udp.MOVE2, 2, 2.0, 2.0, 2.0), None)  # its MOVE was lost
    agent.datagram_received(udp.seal(keys, conn_id, udp.MOVE, 3, 3.0, 3.0, 3.0), None)
    assert moves == [(1.0, 1.0, 1.0), (2.0, 2.0, 2.0), (3.0, 3.0, 3.0)]
    assert agent.copies == 1
    s = agent.loss.stats()
    assert (s["received"], s["lost"]) == (2, 1)  # the loss under the copies, not hidden by them


# ------------------------------------------------------------- end to end
async def start(optimise=True):
    server = HostServer({"laptop"}, server_context(SECRET), host="127.0.0.1", port=0, optimise=optimise)
    await server.start()
    sink = FakeSink()
    session = AgentSession(sink, lambda e, x, y: None)
    client = AgentClient("laptop", "Laptop", "127.0.0.1", server.port, client_context(SECRET), session,
                         optimise=optimise)
    task = asyncio.create_task(client.run_once())
    await wait_for(lambda: server.is_connected("laptop"))
    return server, client, task, sink


async def stop(server, task):
    await server.close()
    await asyncio.wait_for(task, 3)


async def test_link_report_and_netstats_round_trip(monkeypatch):
    monkeypatch.setattr("curslink.net.client.NETSTATS_EVERY_S", 0.05)
    server, client, task, sink = await start()
    conn = server.connections["laptop"]
    assert "netstats" in conn.features
    await wait_for(lambda: client.udp is not None and client.udp.heard_lately())
    await wait_for(lambda: conn.netstats is not None)
    await wait_for(lambda: conn.rtt.stats()["samples"] > 0, timeout=3)
    report = server.link_report("laptop")
    assert report["path"] == "udp" and report["udp_offered"] and report["local"] == "127.0.0.1"
    assert report["rtt"]["samples"] >= 1 and report["optimise"]
    await wait_for(lambda: client.session.host_link.get("rtt_ms") is not None)
    mine = client.link_report()
    assert mine["path"] == "udp" and mine["rtt"]["p50_ms"] is not None
    event = check.network_event("host", [report], True, [{"name": "lo", "ip": "127.0.0.1", "kind": "other"}])
    link = event["links"][0]
    assert link["interface"]["name"] == "lo" and link["grade"] in ("good", "fair", "poor", "unknown")
    await stop(server, task)


async def test_paced_moves_keep_the_newest_and_go_before_a_click():
    server, client, task, sink = await start()
    conn = server.connections["laptop"]
    conn.optimiser.tuning = quality.Tuning(move_interval_s=0.05)
    conn.udp = None  # all over TCP, to see the order
    server.send("laptop", "enter", x=0, y=0, effect="none")
    await wait_for(lambda: client.session.active)
    for i in range(1, 6):
        server.send("laptop", "move", x=i, y=i, ts=1000.0 + i)
    server.send("laptop", "button", button="left", pressed=True)
    await wait_for(lambda: ("button", "left", True) in sink.calls)
    moves = [c for c in sink.calls if c[0] == "move"]
    # the first went at once; 2-4 were replaced by 5, which went before the click
    assert moves[-2:] == [("move", 1, 1), ("move", 5, 5)]
    assert sink.calls[-1] == ("button", "left", True)
    # and a held move goes by itself once its turn comes
    server.send("laptop", "move", x=9, y=9, ts=2000.0)
    server.send("laptop", "move", x=10, y=10, ts=2001.0)
    await wait_for(lambda: ("move", 10, 10) in sink.calls)
    assert ("move", 9, 9) not in sink.calls
    await stop(server, task)


async def test_retune_turns_on_copies_from_the_agents_loss():
    server, client, task, sink = await start()
    conn = server.connections["laptop"]
    await wait_for(lambda: conn.udp.usable(server.udp.clock()))
    for _ in range(20):
        conn.rtt.add(0.005)
    import time as _time
    conn.netstats, conn.netstats_at = {"udp": True, "loss_pct": 4.0}, _time.monotonic()
    changes = []
    server.on_tuning = lambda d, t: changes.append((d, t))
    server.retune(conn)
    assert conn.udp.copies == 2 and changes and changes[0][0] == "laptop"
    server.send("laptop", "enter", x=0, y=0, effect="none")
    await wait_for(lambda: client.session.active)
    server.send("laptop", "move", x=3, y=4, ts=5000.0)
    await wait_for(lambda: client.udp.moves >= 1)
    await asyncio.sleep(0.05)
    assert ("move", 3, 4) in sink.calls
    await stop(server, task)


async def test_no_optimiser_means_no_pacing_or_tuning_task():
    server, client, task, sink = await start(optimise=False)
    conn = server.connections["laptop"]
    assert conn.tuning == quality.Tuning() and not conn.optimiser.enabled
    await stop(server, task)


# --------------------------------------------------------- relay upgrade
async def test_agent_on_the_relay_reconnects_once_the_host_answers_on_the_lan(tmp_path):
    from curslink.pairing.discovery import Found

    cfg = agent_config({"agent": {"device_id": "laptop"}}, tmp_path)

    class Client:
        path = "relay"
        ssl_context = None
        reconnected = 0

        def reconnect(self):
            self.reconnected += 1
            self.path = "udp"

    class Peer:
        device_id, name = "desk", "Desk"

    async def find(device_id, timeout):
        return Found("desk", "Desk", "host", ("192.168.1.5",), 24851, 24850)

    async def reachable(addresses, port, ssl=None):
        return addresses[0]

    client, session = Client(), AgentSession(FakeSink(), lambda e, x, y: None)
    original = app_mod.paired_host
    app_mod.paired_host = lambda cfg, store: Peer()
    try:
        session.active = True  # in use: wait
        runner = asyncio.create_task(app_mod.upgrade_path(cfg, object(), client, session, every_s=0.01,
                                                          find=find, reachable=reachable))
        await asyncio.sleep(0.05)
        assert client.reconnected == 0
        session.active = False
        await wait_for(lambda: client.reconnected == 1)
        await asyncio.sleep(0.05)
        assert client.reconnected == 1  # direct now: nothing more to do
        runner.cancel()
    finally:
        app_mod.paired_host = original


# ------------------------------------------------------------------ check
def test_interface_kinds():
    assert check.interface_kind("en0", "Wi-Fi") == "wifi"
    assert check.interface_kind("wlan0") == "wifi"
    assert check.interface_kind("en5", "Thunderbolt Ethernet Slot 1") == "wired"
    assert check.interface_kind("eth0") == "wired"
    assert check.interface_kind("utun3") == "vpn"
    assert check.interface_kind("Intel(R) Wi-Fi 6 AX201 160MHz") == "wifi"
    assert check.interface_kind("lo0") == "other"


def test_interface_for_matches_our_end_of_a_connection():
    known = [{"name": "Wi-Fi", "ip": "192.168.1.20", "kind": "wifi"}]
    assert check.interface_for("::ffff:192.168.1.20", known)["name"] == "Wi-Fi"
    assert check.interface_for("10.0.0.1", known) is None
    assert check.interface_for("", known) is None


async def test_run_check_reports_mdns_and_peers(monkeypatch):
    from curslink.pairing import discovery
    from curslink.pairing.discovery import Found

    async def browse(timeout):
        return [Found("desk", "Desk", "host", ("192.168.1.5",), 24851, 24850),
                Found("laptop", "Laptop", "agent", ("192.168.1.6",), 24851)]

    monkeypatch.setattr(discovery, "browse", browse)
    monkeypatch.setattr(check, "interfaces", lambda: [{"name": "Wi-Fi", "ip": "192.168.1.6", "kind": "wifi"}])
    result = await check.run_check("laptop", {"desk", "phone"})
    mdns = result["mdns"]
    assert mdns["self_visible"] and mdns["peers"] == {"desk": True, "phone": False}
    assert [d["id"] for d in mdns["devices"]] == ["desk"]
    lines = check.summarise(result)
    assert any("Found by mDNS: desk" in l for l in lines)
    assert any("Not found by mDNS: phone" in l for l in lines)


async def test_emit_check_survives_a_broken_mdns(monkeypatch):
    from curslink.pairing import discovery

    async def browse(timeout):
        raise OSError("no network")

    monkeypatch.setattr(discovery, "browse", browse)
    got = []
    await check.emit_check(lambda kind, **data: got.append((kind, data)), "laptop", set())
    kind, data = got[0]
    assert kind == "netcheck" and data["mdns"]["ok"] is False
    assert "isn't working" in data["summary"][0]


def test_network_optimise_config():
    assert network_optimise({}) is True
    assert network_optimise({"network": {"optimise": False}}) is False
