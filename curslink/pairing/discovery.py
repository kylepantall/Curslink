"""Finding Curslink devices on the LAN with mDNS / DNS-SD (Bonjour).

Every running host and agent advertises ``_curslink._tcp.local.`` with its
pairing port and a TXT record::

    id=<device_id> name=<display name> role=host|agent v=1 cport=<host port>

``cport`` (hosts only) is where agents connect for input, so a paired agent
can find its host without a configured address.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from dataclasses import dataclass, field

from curslink.pairing.handshake import DeviceInfo, PAIRING_VERSION

log = logging.getLogger(__name__)

SERVICE_TYPE = "_curslink._tcp.local."


@dataclass(frozen=True)
class Found:
    device_id: str
    name: str
    role: str
    addresses: tuple[str, ...]
    pairing_port: int
    control_port: int | None = None
    service: str = field(default="", compare=False)
    host: str = field(default="", compare=False)  # its .local name, e.g. "kyles-pc.local"

    @property
    def address(self) -> str:
        return self.addresses[0]


def _txt(properties: dict, key: str) -> str:
    value = properties.get(key.encode())
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else ""


def found_from_info(info) -> Found | None:
    """A resolved zeroconf ``ServiceInfo`` -> :class:`Found`, or None if it isn't usable."""
    props = info.properties or {}
    device_id, role = _txt(props, "id"), _txt(props, "role")
    addresses = tuple(
        a for a in info.parsed_scoped_addresses() if not ipaddress.ip_address(a.split("%")[0]).is_loopback
    ) or tuple(info.parsed_scoped_addresses())
    if not device_id or role not in ("host", "agent") or not addresses or not info.port:
        return None
    addresses = rank_addresses(addresses)
    cport = _txt(props, "cport")
    return Found(
        device_id=device_id,
        name=_txt(props, "name") or device_id,
        role=role,
        addresses=addresses,
        pairing_port=info.port,
        control_port=int(cport) if cport.isdigit() else None,
        service=info.name,
        host=(info.server or "").rstrip("."),
    )


def local_networks() -> list[ipaddress.IPv4Network]:
    """The IPv4 networks this machine is on (LAN, VPN, VM bridges)."""
    try:
        import ifaddr  # a zeroconf dependency
    except ImportError:  # pragma: no cover
        return []
    nets = []
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            if isinstance(ip.ip, str):
                net = ipaddress.ip_network(f"{ip.ip}/{ip.network_prefix}", strict=False)
                if not (net.network_address.is_loopback or net.network_address.is_link_local):
                    nets.append(net)
    return nets


def rank_addresses(addresses, networks=None) -> tuple[str, ...]:
    """Best address to try first. A device advertises every address it has,
    and a Mac also has VM bridges and VPN tunnels the other device can't
    reach (192.168.64.1, 10.x), so: an address on one of our own networks
    first, then other IPv4, then IPv6."""
    nets = local_networks() if networks is None else networks

    def key(a: str):
        ip = ipaddress.ip_address(a.split("%")[0])
        if ip.version == 6:
            return 2
        return 0 if any(ip in n for n in nets) else 1

    return tuple(sorted(addresses, key=key))


async def reachable(addresses, port: int, timeout: float = 2.0, ssl=None) -> str | None:
    """The first of ``addresses`` (in order) that accepts a connection on
    ``port`` within ``timeout``. All are tried at once, so a few that never
    answer cost one ``timeout``, not one each (Wi-Fi can take over a second
    to answer). With ``ssl`` (the pairing's TLS-PSK context) the TLS
    handshake has to complete too: proof it's really that paired device,
    not just something listening on the port."""

    async def probe(addr: str) -> bool:
        try:
            _reader, writer = await asyncio.wait_for(asyncio.open_connection(addr, port, ssl=ssl), timeout)
        except (OSError, asyncio.TimeoutError):
            return False
        writer.close()
        return True

    addresses = list(addresses)
    answered = await asyncio.gather(*(probe(a) for a in addresses))
    return next((a for a, ok in zip(addresses, answered) if ok), None)


async def resolve_local_name(name: str, timeout: float = 3.0) -> list[str]:
    """IPv4 addresses the operating system's resolver gives for a ``.local``
    name (Windows, macOS and most Linux resolve these themselves), or []."""
    try:
        infos = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(name, None, family=socket.AF_INET, type=socket.SOCK_STREAM),
            timeout,
        )
    except (OSError, asyncio.TimeoutError):
        return []
    return list(dict.fromkeys(info[4][0] for info in infos))


def local_addresses() -> list[str]:
    """This machine's LAN addresses (IPv4, not loopback or link-local)."""
    try:
        import ifaddr  # a zeroconf dependency
    except ImportError:  # pragma: no cover
        return []
    out = []
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            if not isinstance(ip.ip, str):
                continue
            addr = ipaddress.ip_address(ip.ip)
            if not (addr.is_loopback or addr.is_link_local):
                out.append(ip.ip)
    return out


def zeroconf_options() -> dict:
    """Keyword arguments for ``Zeroconf``: IPv4 only, on the LAN addresses.

    zeroconf's default is every interface, which on Windows includes
    127.0.0.1 (its multicast socket fails with WinError 1231 "network
    location cannot be reached") and VPN/virtual adapters that can't carry
    multicast, and each failing socket costs a send. Binding just the real
    addresses makes queries and announcements go out reliably."""
    from zeroconf import IPVersion

    addresses = list(dict.fromkeys(local_addresses()))
    opts: dict = {"ip_version": IPVersion.V4Only}
    if addresses:
        opts["interfaces"] = addresses
    return opts


def local_hostname() -> str:
    """This machine's mDNS name, e.g. "kyles-pc.local"."""
    host = re.sub(r"[^A-Za-z0-9-]", "-", socket.gethostname().split(".")[0]) or "curslink"
    return f"{host}.local"


def _instance_name(me: DeviceInfo) -> str:
    label = re.sub(r"[^A-Za-z0-9 _-]", "", me.name).strip()[:40] or "Curslink"
    return f"{label} ({me.device_id[:20]}).{SERVICE_TYPE}"


class Advertiser:
    """Advertises this device while the host or agent runs."""

    def __init__(self, me: DeviceInfo, pairing_port: int, control_port: int | None = None):
        self.me = me
        self.pairing_port = pairing_port
        self.control_port = control_port
        self._zc = None
        self._info = None

    async def start(self) -> None:
        from zeroconf import ServiceInfo
        from zeroconf.asyncio import AsyncZeroconf

        props = {"id": self.me.device_id, "name": self.me.name, "role": self.me.role, "v": str(PAIRING_VERSION)}
        if self.control_port:
            props["cport"] = str(self.control_port)
        addresses = local_addresses()
        self._info = ServiceInfo(
            SERVICE_TYPE,
            _instance_name(self.me),
            port=self.pairing_port,
            properties=props,
            server=f"{local_hostname()}.",
            parsed_addresses=addresses,
        )
        self._zc = AsyncZeroconf(**zeroconf_options())
        # allow_name_change: two devices with the same display name still both show up.
        await self._zc.async_register_service(self._info, allow_name_change=True)
        log.info("advertising %s on the LAN for pairing (port %d) as %s, %s", self.me.name, self.pairing_port,
                 local_hostname(), ", ".join(addresses) or "no LAN address")

    async def close(self) -> None:
        if self._zc is None:
            return
        try:
            await self._zc.async_unregister_all_services()
        finally:
            await self._zc.async_close()
            self._zc = None


async def browse(timeout: float = 3.0, exclude: str | None = None, want: str | None = None) -> list[Found]:
    """Every Curslink device that answers within ``timeout`` seconds.

    With ``want`` (a device id) it returns as soon as that device has
    answered, so a long ``timeout`` (Wi-Fi drops the odd multicast packet)
    only costs time when the device really isn't there."""
    from zeroconf import ServiceStateChange
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

    names: set[str] = set()

    def on_change(zeroconf, service_type, name, state_change):
        if state_change in (ServiceStateChange.Added, ServiceStateChange.Updated):
            names.add(name)

    log.info("discovery: looking for Curslink devices for %.0fs from %s (%s)", timeout, local_hostname(),
             ", ".join(local_addresses()) or "no LAN address")
    azc = AsyncZeroconf(**zeroconf_options())
    browser = AsyncServiceBrowser(azc.zeroconf, [SERVICE_TYPE], handlers=[on_change])
    tag = f"({want[:20]})" if want else None
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            await asyncio.sleep(0.1)
            if tag and any(tag in n for n in names):
                break
        infos = [AsyncServiceInfo(SERVICE_TYPE, n) for n in sorted(names)]
        await asyncio.gather(*(i.async_request(azc.zeroconf, 1500) for i in infos))
    finally:
        await browser.async_cancel()
        await azc.async_close()
    found: dict[str, Found] = {}
    for info in infos:
        item = found_from_info(info)
        if item is None:
            log.info("discovery: %s answered but didn't resolve to a usable address", info.name)
        elif item.device_id != exclude:
            found.setdefault(item.device_id, item)
    if found:
        log.info("discovery: found %s", "; ".join(
            f"{f.name} ({f.role}, {f.host or '?'}, {', '.join(f.addresses)})" for f in found.values()))
    else:
        log.info("discovery: found nothing (%d answer(s), including this device's own). If the other "
                 "computer is running Curslink, a firewall or macOS Local Network access is likely "
                 "blocking mDNS (UDP 5353)", len(names))
    return sorted(found.values(), key=lambda f: (f.role != "host", f.name.lower()))


async def find(device_id: str, timeout: float = 3.0) -> Found | None:
    for item in await browse(timeout, want=device_id):
        if item.device_id == device_id:
            return item
    return None
