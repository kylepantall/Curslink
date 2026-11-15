"""USB-C / Thunderbolt cable links between two computers: found, preferred, followed."""

import asyncio
import ipaddress

from curslink import app as app_mod
from curslink.net import check
from curslink.net.client import AgentSession
from curslink.pairing import discovery
from curslink.pairing.discovery import Found, cable_addresses, is_cable_name, is_link_local, rank_addresses

LAN = ipaddress.ip_network("192.168.68.0/22")
CABLE = ipaddress.ip_network("169.254.0.0/16")


def test_link_local_and_cable_names():
    assert is_link_local("169.254.12.3") and not is_link_local("192.168.68.59") and not is_link_local("fe80::1")
    assert is_cable_name("bridge0") and is_cable_name("Thunderbolt Bridge")
    assert is_cable_name("USB4(TM) P2P Network Adapter") and is_cable_name("Thunderbolt(TM) Networking")
    assert not is_cable_name("en5 Thunderbolt Ethernet Slot 1")  # an Ethernet adapter on the LAN
    assert not is_cable_name("Intel(R) Wi-Fi 6E AX211 160MHz")


def test_the_cable_comes_first_when_we_have_one_too():
    pc = ("192.168.68.59", "169.254.20.7", "fe80::1")
    assert rank_addresses(pc, [LAN, CABLE], [CABLE])[0] == "169.254.20.7"
    # No cable here: their 169.254 address is no use, after the LAN one.
    assert rank_addresses(pc, [LAN], []) == ("192.168.68.59", "169.254.20.7", "fe80::1")
    # A cable with addresses set by hand counts too.
    by_hand = ipaddress.ip_network("10.99.0.0/24")
    assert rank_addresses(("192.168.68.59", "10.99.0.2"), [LAN, by_hand], [by_hand])[0] == "10.99.0.2"
    assert cable_addresses(pc, [CABLE]) == ["169.254.20.7"]


def test_link_local_addresses_are_advertised_but_not_put_in_qr_codes(monkeypatch):
    class Ip:
        def __init__(self, ip):
            self.ip, self.network_prefix = ip, 16

    class Adapter:
        def __init__(self, name, ips):
            self.name, self.nice_name, self.ips = name, name, [Ip(i) for i in ips]

    import ifaddr

    monkeypatch.setattr(ifaddr, "get_adapters", lambda: [Adapter("Ethernet", ["192.168.68.69"]),
                                                          Adapter("bridge0", ["169.254.3.4"]),
                                                          Adapter("Bluetooth Device (Personal Area Network)",
                                                                  ["169.254.9.9"]),
                                                          Adapter("Microsoft Wi-Fi Direct Virtual Adapter",
                                                                  ["169.254.8.8"]),
                                                          Adapter("Surfshark WireGuard", ["169.254.7.7"]),
                                                          Adapter("Hyper-V Virtual Ethernet Adapter",
                                                                  ["169.254.6.6"]),
                                                          Adapter("Realtek USB GbE Family Controller",
                                                                  ["169.254.5.5"]),
                                                          Adapter("lo0", ["127.0.0.1"])])
    # Thunderbolt Bridge, and an Ethernet cable straight to the other computer;
    # not Windows' 169.254 on Bluetooth, Wi-Fi Direct, a VPN or Hyper-V.
    assert discovery.local_addresses() == ["192.168.68.69", "169.254.3.4", "169.254.5.5"]
    assert discovery.local_addresses(link_local=False) == ["192.168.68.69"]
    assert discovery.cable_networks() == [CABLE, CABLE]
    kinds = {i["device"]: i["kind"] for i in check.interfaces()}
    assert kinds == {"Ethernet": "wired", "bridge0": "cable", "Realtek USB GbE Family Controller": "cable"}


class Client:
    def __init__(self, host):
        self.host, self.path, self.ssl_context, self.reconnected = host, "udp", None, 0

    def reconnect(self):
        self.reconnected += 1


class Peer:
    device_id, name = "kyles-pc", "Kyles_PC"


async def wait_for(cond, timeout=2.0):
    for _ in range(int(timeout / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


async def test_agent_moves_onto_the_cable_and_back_off_it(tmp_path, monkeypatch):
    from tests.test_netquality import agent_config

    cfg = agent_config({"agent": {"device_id": "wks82261"}}, tmp_path)
    nets: list = []
    host_addresses = ["192.168.68.59"]
    probed = []

    async def find(device_id, timeout):
        return Found("kyles-pc", "Kyles_PC", "host", tuple(host_addresses), 24851, 24850)

    async def reachable(addresses, port, ssl=None):
        probed.append(list(addresses))
        return addresses[0]

    monkeypatch.setattr(app_mod, "paired_host", lambda cfg, store: Peer())
    client, session = Client("192.168.68.59"), AgentSession(None, lambda e, x, y: None)
    runner = asyncio.create_task(app_mod.follow_cable(
        cfg, object(), client, session, every_s=0.01, first_s=0.02, max_s=0.05,
        find=find, reachable=reachable, cable_networks=lambda: list(nets)))
    try:
        await asyncio.sleep(0.1)
        assert client.reconnected == 0 and not probed  # no cable: nothing to look for
        nets.append(CABLE)  # plugged in: our end has its address, the host's not yet advertised
        await asyncio.sleep(0.1)
        assert client.reconnected == 0
        session.active = True  # input is here: don't cut it off
        host_addresses.append("169.254.20.7")
        await asyncio.sleep(0.1)
        assert client.reconnected == 0
        session.active = False
        await wait_for(lambda: client.reconnected == 1)
        assert probed[-1] == ["169.254.20.7"]
        client.host = "169.254.20.7"  # what resolve_host picks on the reconnect
        await asyncio.sleep(0.1)
        assert client.reconnected == 1  # on the cable: stays
        nets.clear()  # unplugged
        await wait_for(lambda: client.reconnected == 2)
        client.host = "192.168.68.59"
        await asyncio.sleep(0.1)
        assert client.reconnected == 2
    finally:
        runner.cancel()


async def test_resolve_host_tries_the_cable_before_the_last_address(tmp_path, monkeypatch):
    from tests.test_netquality import agent_config

    cfg = agent_config({"agent": {"device_id": "wks82261"}}, tmp_path)

    class Stored:
        device_id, name, key, address = "kyles-pc", "Kyles_PC", b"k" * 32, "192.168.68.59"

    orders = []

    async def find(device_id, timeout):
        return Found("kyles-pc", "Kyles_PC", "host", ("192.168.68.59", "169.254.20.7"), 24851, 24850)

    async def reachable(addresses, port, ssl=None):
        orders.append(list(addresses))
        return addresses[0]

    monkeypatch.setattr(app_mod, "paired_host", lambda cfg, store: Stored())
    monkeypatch.setattr(discovery, "find", find)
    monkeypatch.setattr(discovery, "reachable", reachable)
    monkeypatch.setattr(discovery, "cable_networks", lambda: [CABLE])
    target = await app_mod.resolve_host(cfg, object())
    assert target.host == "169.254.20.7"
    assert orders == [["169.254.20.7", "192.168.68.59"]]
