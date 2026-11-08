"""The network check behind Settings > Network.

Two parts:

* :func:`network_event` builds the ``network`` event a running host or agent
  sends the app every few seconds: each link's report (see
  ``HostServer.link_report`` and ``AgentClient.link_report``) with the
  interface it goes out on, a grade and tips;
* :func:`run_check` is the "Check the network" button: it looks for every
  Curslink device by mDNS (and whether this one can see itself, which
  shows multicast works at all), and lists this computer's interfaces.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import sys
import time
from functools import lru_cache

from curslink.net import quality

log = logging.getLogger(__name__)

MDNS_TIMEOUT_S = 4.0


# -------------------------------------------------------------- interfaces
@lru_cache(maxsize=1)
def _mac_ports() -> dict[str, str]:
    """macOS: device -> hardware port ("en0" -> "Wi-Fi"); en0 isn't always Wi-Fi."""
    try:
        out = subprocess.run(["networksetup", "-listallhardwareports"], capture_output=True, text=True,
                             timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    ports = {}
    for port, device in re.findall(r"Hardware Port: (.+)\nDevice: (\S+)", out):
        ports[device] = port
    return ports


def interface_kind(name: str, port: str = "") -> str:
    """``wifi``, ``wired``, ``vpn`` or ``other``, from an interface's name."""
    text = f"{port} {name}".lower()
    if any(w in text for w in ("wi-fi", "wifi", "wlan", "wireless", "airport", "802.11")) or re.match(r"wl", name):
        return "wifi"
    if any(w in text for w in ("vpn", "tun", "tap", "wireguard", "utun", "tailscale", "zerotier", "ipsec")):
        return "vpn"
    if any(w in text for w in ("ethernet", "thunderbolt", "usb", "lan", "eth", "enp", "eno", "ens", "gbe", "gigabit")):
        return "wired"
    return "other"


def interfaces() -> list[dict]:
    """This computer's IPv4 interfaces: ``{name, ip, kind}``."""
    try:
        import ifaddr  # a zeroconf dependency
    except ImportError:  # pragma: no cover
        return []
    ports = _mac_ports() if sys.platform == "darwin" else {}
    found = []
    for adapter in ifaddr.get_adapters():
        name = adapter.nice_name or adapter.name
        for ip in adapter.ips:
            if not isinstance(ip.ip, str) or ip.ip.startswith(("127.", "169.254.")):
                continue
            port = ports.get(adapter.name, "")
            found.append({"name": port or name, "device": adapter.name, "ip": ip.ip,
                          "kind": interface_kind(str(adapter.name), port or str(name))})
    return found


def interface_for(ip: str, known: list[dict] | None = None) -> dict | None:
    """The interface with that address (our end of a connection), or None."""
    if not ip:
        return None
    ip = ip.split("%")[0]
    if ip.startswith("::ffff:"):
        ip = ip[7:]
    return next((i for i in (interfaces() if known is None else known) if i["ip"] == ip), None)


# ------------------------------------------------------------------ events
class InterfaceCache:
    """Interfaces, looked up again at most every ``ttl_s`` (it isn't free)."""

    def __init__(self, ttl_s: float = 30.0):
        self.ttl_s = ttl_s
        self._at = -ttl_s
        self._list: list[dict] = []

    def get(self) -> list[dict]:
        now = time.monotonic()
        if now - self._at >= self.ttl_s:
            self._list, self._at = interfaces(), now
        return self._list


def finish(link: dict, ifaces: list[dict], mdns: bool | None = None) -> dict:
    """A link report with its interface, grade and tips filled in."""
    link = dict(link)
    link["interface"] = interface_for(link.get("local", ""), ifaces)
    if mdns is not None:
        link["mdns"] = mdns
    link["grade"] = quality.grade(link.get("rtt") or {}, link.get("loss_pct"))
    link["tips"] = quality.advice(link)
    return link


def network_event(role: str, links: list[dict], optimise: bool, ifaces: list[dict],
                  mdns: bool | None = None) -> dict:
    return {"role": role, "optimise": optimise, "links": [finish(l, ifaces, mdns) for l in links]}


# ------------------------------------------------------------------- check
async def run_check(device_id: str, peers: set[str], timeout: float = MDNS_TIMEOUT_S) -> dict:
    """mDNS and interfaces, for the "Check the network" button. ``peers``:
    the device ids this one links with (paired, or in the layout)."""
    from curslink.pairing.discovery import browse

    started = time.monotonic()
    result: dict = {"interfaces": interfaces(), "mdns": {}}
    try:
        found = await browse(timeout)
    except Exception as exc:  # zeroconf can't start (no network, socket in use)
        log.info("network check: mDNS failed: %s", exc)
        result["mdns"] = {"ok": False, "error": str(exc), "devices": [], "self_visible": False, "peers": {}}
        return result
    # browse() leaves this device out only when asked; it answers itself
    # when multicast works on at least one interface.
    devices = [{"id": f.device_id, "name": f.name, "role": f.role, "addresses": list(f.addresses),
                "host": f.host} for f in found]
    ids = {d["id"] for d in devices}
    result["mdns"] = {
        "ok": True,
        "devices": [d for d in devices if d["id"] != device_id],
        "self_visible": device_id in ids,
        "peers": {p: p in ids for p in sorted(peers)},
        "took_s": round(time.monotonic() - started, 1),
    }
    return result


def summarise(check: dict) -> list[str]:
    """Plain-language lines for a :func:`run_check` result."""
    lines = []
    mdns = check.get("mdns") or {}
    if not mdns.get("ok"):
        lines.append(f"mDNS isn't working here: {mdns.get('error') or 'unknown error'}.")
    else:
        if mdns.get("self_visible"):
            lines.append("mDNS works: this computer can see its own announcement.")
        else:
            lines.append("This computer can't see its own mDNS announcement. A firewall, a VPN or "
                         "(on a Mac) Local Network access for Curslink may be blocking it.")
        peers = mdns.get("peers") or {}
        seen = [p for p, ok in peers.items() if ok]
        missing = [p for p, ok in peers.items() if not ok]
        if seen:
            lines.append("Found by mDNS: " + ", ".join(seen) + ".")
        if missing:
            lines.append("Not found by mDNS: " + ", ".join(missing) + ". If it's running and on this "
                         "network, the router may be dropping multicast (common on mesh and guest Wi-Fi).")
        if not peers and not mdns.get("devices"):
            lines.append("No other Curslink device answered.")
    ifaces = check.get("interfaces") or []
    if ifaces:
        names = {"wifi": "Wi-Fi", "wired": "wired", "vpn": "VPN", "other": "other"}
        def one(i: dict) -> str:
            kind = names.get(i["kind"], i["kind"])
            show = kind and kind.lower() not in i["name"].lower() and kind != "other"
            return f"{i['name']} ({kind + ', ' if show else ''}{i['ip']})"

        lines.append("Interfaces: " + ", ".join(one(i) for i in ifaces) + ".")
        if any(i["kind"] == "vpn" for i in ifaces):
            lines.append("A VPN is on. Some VPNs send local traffic through the tunnel, which hides the "
                         "other computer or slows the link.")
    else:
        lines.append("This computer has no network address.")
    return lines


async def emit_check(emit, device_id: str, peers: set[str]) -> None:
    """Run the check and send it to the app as a ``netcheck`` event."""
    try:
        result = await run_check(device_id, peers)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # never take the host or agent down over a check
        log.exception("network check failed")
        result = {"mdns": {"ok": False, "error": str(exc)}, "interfaces": []}
    result["summary"] = summarise(result)
    log.info("network check: %s", " ".join(result["summary"]))
    emit("netcheck", **result)
