import asyncio

import pytest

from curslink.pairing.discovery import SERVICE_TYPE, Advertiser, browse, found_from_info
from curslink.pairing.handshake import DeviceInfo


def info(props, port=24851, addresses=("192.168.1.20",)):
    from zeroconf import ServiceInfo

    return ServiceInfo(SERVICE_TYPE, f"x.{SERVICE_TYPE}", port=port, properties=props,
                       server="x.local.", parsed_addresses=list(addresses))


def test_found_from_txt_record():
    f = found_from_info(info({"id": "desk", "name": "Desk", "role": "host", "cport": "24850"}))
    assert (f.device_id, f.name, f.role, f.address, f.pairing_port, f.control_port) == (
        "desk", "Desk", "host", "192.168.1.20", 24851, 24850)
    assert f.host == "x.local"


def test_looking_reporter_says_when_the_screen_changes(monkeypatch):
    from curslink import app, events
    from curslink.gaze.zones import Target

    sent = []
    monkeypatch.setattr(events, "emit", lambda kind, **data: sent.append((kind, data)))

    class Classifier:
        class settings:
            max_distance_deg = 20.0

        def nearest(self, yaw, pitch):
            return (Target("desk", "main"), abs(yaw)) if yaw < 15 else (Target("laptop", "screen1"), abs(yaw - 30))

    r = app.LookingReporter(Classifier())
    r.sample(0, 0)
    r.sample(1, 0)  # same screen, within a second: nothing new
    r.sample(30, 0)
    r.sample(80, 0)  # far from every screen
    assert [d["screen"] for _k, d in sent] == ["desk/main", "laptop/screen1", ""]


def test_firewall_check_is_a_no_op_off_windows():
    from curslink import winfirewall

    if not winfirewall.WINDOWS:
        assert winfirewall.status() == "ok" and winfirewall.allow()


def test_unusable_adverts_are_ignored():
    assert found_from_info(info({"id": "desk", "role": "phone"})) is None
    assert found_from_info(info({"role": "host"})) is None
    assert found_from_info(info({"id": "d", "role": "agent"}, addresses=())) is None


async def test_advertise_and_browse():
    adverts = [
        Advertiser(DeviceInfo("pytest-desk", "Desk", "host"), 24990, 24991),
        Advertiser(DeviceInfo("pytest-laptop", "Laptop", "agent"), 24992),
    ]
    try:
        for a in adverts:
            await a.start()
    except OSError as exc:
        pytest.skip(f"no multicast here: {exc}")
    try:
        found = {f.device_id: f for f in await browse(2.0, exclude="pytest-laptop")}
    finally:
        for a in adverts:
            await a.close()
    if "pytest-desk" not in found:
        pytest.skip("mDNS answers did not arrive (multicast blocked on this machine)")
    assert "pytest-laptop" not in found
    assert found["pytest-desk"].control_port == 24991 and found["pytest-desk"].pairing_port == 24990


def test_local_network_probe(monkeypatch):
    import errno
    import socket

    from curslink import macperms
    from curslink.pairing import discovery

    bound, refused = [], set()

    class FakeSocket:
        def __init__(self, *_):
            self.addr = None

        def bind(self, addr):
            self.addr = addr[0]
            bound.append(addr)

        def setsockopt(self, *_):
            pass

        def sendto(self, _data, _dest):
            if self.addr in refused:
                raise OSError(errno.EHOSTUNREACH, "No route to host")

        def close(self):
            pass

    monkeypatch.setattr(socket, "socket", FakeSocket)
    monkeypatch.setattr(discovery, "local_addresses", lambda: ["192.168.1.5", "10.0.0.2"])
    assert macperms._local_network_ok()
    assert bound[0] == ("192.168.1.5", 0)  # bound, like zeroconf's sockets
    refused.update({"192.168.1.5", "10.0.0.2"})
    assert not macperms._local_network_ok()
    refused.discard("10.0.0.2")  # one address getting through is enough
    assert macperms._local_network_ok()


def test_addresses_on_our_network_come_first():
    import ipaddress

    from curslink.pairing.discovery import rank_addresses

    lan = [ipaddress.ip_network("192.168.68.0/22")]
    # What a Mac with a VM bridge and a VPN advertises.
    mac = ("192.168.64.1", "10.200.1.2", "fe80::1", "192.168.68.52")
    assert rank_addresses(mac, lan)[0] == "192.168.68.52"
    assert rank_addresses(mac, lan)[-1] == "fe80::1"
    assert rank_addresses(("10.0.0.5", "192.168.1.9"), [])[0] == "10.0.0.5"  # no match: order kept


def test_reachable_skips_addresses_that_dont_answer():
    import asyncio

    from curslink.pairing.discovery import reachable

    async def go():
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            # 192.0.2.1 is TEST-NET: never answers.
            return (await reachable(("192.0.2.1", "127.0.0.1"), port, timeout=0.3),
                    await reachable(("192.0.2.1",), port, timeout=0.3))

    assert asyncio.run(go()) == ("127.0.0.1", None)


def test_resolve_local_name_returns_ipv4_or_nothing():
    from curslink.pairing.discovery import resolve_local_name

    assert "127.0.0.1" in asyncio.run(resolve_local_name("localhost"))
    assert asyncio.run(resolve_local_name("no-such-host.invalid", timeout=2.0)) == []
