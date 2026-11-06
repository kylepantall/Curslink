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
    # IPv4 first: it is what most home networks route reliably.
    addresses = tuple(sorted(addresses, key=lambda a: ":" in a))
    cport = _txt(props, "cport")
    return Found(
        device_id=device_id,
        name=_txt(props, "name") or device_id,
        role=role,
        addresses=addresses,
        pairing_port=info.port,
        control_port=int(cport) if cport.isdigit() else None,
        service=info.name,
    )


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
        host = re.sub(r"[^A-Za-z0-9-]", "-", socket.gethostname().split(".")[0]) or "curslink"
        self._info = ServiceInfo(
            SERVICE_TYPE,
            _instance_name(self.me),
            port=self.pairing_port,
            properties=props,
            server=f"{host}.local.",
            parsed_addresses=local_addresses(),
        )
        self._zc = AsyncZeroconf()
        # allow_name_change: two devices with the same display name still both show up.
        await self._zc.async_register_service(self._info, allow_name_change=True)
        log.info("advertising %s on the LAN for pairing (port %d)", self.me.name, self.pairing_port)

    async def close(self) -> None:
        if self._zc is None:
            return
        try:
            await self._zc.async_unregister_all_services()
        finally:
            await self._zc.async_close()
            self._zc = None


async def browse(timeout: float = 3.0, exclude: str | None = None) -> list[Found]:
    """Every Curslink device that answers within ``timeout`` seconds."""
    from zeroconf import ServiceStateChange
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

    names: set[str] = set()

    def on_change(zeroconf, service_type, name, state_change):
        if state_change in (ServiceStateChange.Added, ServiceStateChange.Updated):
            names.add(name)

    azc = AsyncZeroconf()
    browser = AsyncServiceBrowser(azc.zeroconf, [SERVICE_TYPE], handlers=[on_change])
    try:
        await asyncio.sleep(timeout)
        infos = [AsyncServiceInfo(SERVICE_TYPE, n) for n in sorted(names)]
        await asyncio.gather(*(i.async_request(azc.zeroconf, 1500) for i in infos))
    finally:
        await browser.async_cancel()
        await azc.async_close()
    found: dict[str, Found] = {}
    for info in infos:
        item = found_from_info(info)
        if item and item.device_id != exclude:
            found.setdefault(item.device_id, item)
    return sorted(found.values(), key=lambda f: (f.role != "host", f.name.lower()))


async def find(device_id: str, timeout: float = 3.0) -> Found | None:
    for item in await browse(timeout):
        if item.device_id == device_id:
            return item
    return None
