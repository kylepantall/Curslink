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
    """``wifi``, ``wired``, ``vpn``, ``cable`` (a USB-C/Thunderbolt link
    straight to another computer) or ``other``, from an interface's name."""
    from curslink.pairing.discovery import is_cable_name

    text = f"{port} {name}".lower()
    if is_cable_name(text):
        return "cable"
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
            if not isinstance(ip.ip, str) or ip.ip.startswith("127."):
                continue
            port = ports.get(adapter.name, "")
            kind = interface_kind(str(adapter.name), f"{port} {name}")
            if ip.ip.startswith("169.254."):
                from curslink.pairing.discovery import is_cable

                if not is_cable(ip.ip, f"{adapter.name} {port} {name}"):
                    continue  # Windows' self-assigned address on Wi-Fi Direct, Bluetooth, a VPN
                kind = "cable"
            entry = {"name": port or name, "device": adapter.name, "ip": ip.ip, "kind": kind}
            if kind == "cable":
                from curslink.pairing.discovery import is_cable_name

                # A Thunderbolt Bridge / USB4 link, or an Ethernet cable straight between them.
                entry["cable"] = "usb-c" if is_cable_name(f"{adapter.name} {port} {name}") else "ethernet"
            found.append(entry)
    return found


def interface_for(ip: str, known: list[dict] | None = None) -> dict | None:
    """The interface with that address (our end of a connection), or None."""
    if not ip:
        return None
    ip = ip.split("%")[0]
    if ip.startswith("::ffff:"):
        ip = ip[7:]
    return next((i for i in (interfaces() if known is None else known) if i["ip"] == ip), None)


def brief(iface: dict | None) -> dict | None:
    """What the other end needs to know of our interface: its kind and name."""
    if not iface:
        return None
    out = {"kind": iface.get("kind", "other"), "name": iface.get("name", "")}
    if iface.get("cable"):
        out["cable"] = iface["cable"]
    return out


def _end(iface: dict | None) -> str:
    """One end of a link in plain words: "Ethernet", "Wi-Fi", "a VPN (Tailscale)"."""
    if not iface:
        return ""
    kind, name = iface.get("kind"), iface.get("name", "")
    if kind == "wifi":
        return "Wi-Fi"
    if kind == "wired":
        return "Ethernet"
    if kind == "vpn":
        return f"a VPN ({name})" if name else "a VPN"
    if kind == "cable":
        return "a USB-C cable" if iface.get("cable") == "usb-c" else "an Ethernet cable"
    return name or ""


def how_connected(link: dict) -> str:
    """How this device talks to the other one, as plainly as can be said:
    "USB-C cable to WKS82261", "Ethernet cable straight to WKS82261",
    "Wi-Fi here, Ethernet on WKS82261", "Ethernet at both ends", "Through
    the relay". ``link`` is a finished link report: ``interface`` is our
    end, ``peer_interface`` the other end's (when it told us)."""
    name = link.get("name") or ("the host" if link.get("device_id") == "host" else link.get("device_id") or "it")
    if link.get("path") == "relay":
        return f"Through the relay (over the internet) to {name}"
    ours, theirs = link.get("interface") or {}, link.get("peer_interface") or {}
    for end in (ours, theirs):
        if end.get("kind") == "cable":
            if end.get("cable") == "usb-c":
                return f"USB-C cable to {name}"
            return f"Ethernet cable straight to {name}"
    here, there = _end(ours), _end(theirs)
    if here and there:
        text = f"{here} at both ends, here and on {name}" if here == there else f"{here} here, {there} on {name}"
    elif here:
        text = f"{here} here, to {name}"
    elif there:
        text = f"To {name}, which is on {there}"
    else:
        text = f"Over the network to {name}"
    return text[0].upper() + text[1:]


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
    peer = link.get("peer_interface") or (link.get("agent") or {}).get("iface")
    link["peer_interface"] = peer if isinstance(peer, dict) else None
    link["how"] = how_connected(link)
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
    # interfaces() can run networksetup on a Mac: not on this loop.
    result: dict = {"interfaces": await asyncio.to_thread(interfaces), "mdns": {}}
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
        names = {"wifi": "Wi-Fi", "wired": "wired", "vpn": "VPN", "cable": "cable straight to the other computer",
                 "other": "other"}
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
