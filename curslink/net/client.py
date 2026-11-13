"""Agent side: connects to the host and replays the input it receives."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import platform
import ssl
from dataclasses import dataclass
from typing import Awaitable, Callable

from curslink.inputs.base import InputSink
from curslink.net import quality
from curslink.net.framing import STREAM_LIMIT, read_message
from curslink.net.relay import RelayError, RelayTarget, agent_connect
from curslink.net.udp import AgentUdp
from curslink.protocol import (AGENT_FEATURES, CLIPBOARD_FEATURE, EDGE_FEATURE, FILES_FEATURE, FILES_OUT_FEATURE, DROP_FEATURE, FILE_ACK_FEATURE, MIRROR_CURSOR_FEATURE, MIRROR_FEATURE, MIRROR_TILES_FEATURE, NETSTATS_FEATURE, POPUPS_FEATURE, PREPARE_FEATURE, PROTOCOL_VERSION, SCREENS_FEATURE, UDP_FEATURE, Message, ProtocolError,
                               encode)
from curslink.security import channel_binding, hello_auth
from curslink.tally import Tally

log = logging.getLogger(__name__)

# With a relay configured too, give up on the LAN address this quickly.
LAN_ATTEMPT_TIMEOUT_S = 2.0
# Without a relay to fall back to, still give up on an address that doesn't
# answer well before the OS does (about 21 s on Windows): the next attempt
# looks the host up again and may pick an address that works.
LAN_ONLY_TIMEOUT_S = 6.0
# Resend our camera's facing state this often, so the host can tell a
# fresh report from a stale one.
FACING_REFRESH_S = 1.0
# Input from the host that means it's here, so the radio should stay awake
# (see curslink.net.udp.PROBE_WARM_S).
WARM_ON = frozenset({"enter", "leave", "move", "button", "scroll", "key", "drag"})
# Tell the host how the link looks from here this often (netstats).
NETSTATS_EVERY_S = 5.0


class AgentSession:
    """Applies host messages to the local OS. Pure logic, no networking."""

    def __init__(self, sink: InputSink, show_effect: Callable[..., None], smoother=None):
        self.sink = sink
        self.show_effect = show_effect
        self.scrolls = Tally("scroll from the host")
        # A curslink.smoothing.PointerSmoother plays timestamped moves back
        # evenly; without one (or from a host that sends no ts) each move is
        # applied as it arrives.
        self.smoother = smoother
        # The host's calibration asks this device to show "Look here" on one
        # of its screens (or to hide it). See curslink.gui.overlay.
        self.on_look: Callable[[dict], None] = lambda data: None
        # The host's Layout page marking where screens overlap on ours.
        self.on_hint: Callable[[dict], None] = lambda data: None
        # The edge indicator (see curslink.edgehint); None = we don't draw it,
        # so hello doesn't offer the "edge" feature.
        self.on_edge: Callable[[dict], None] | None = None
        # A file drag from the host: {"card": {...}} shows the placeholder
        # that follows the pointer, {"hide": True} takes it down.
        self.on_drag: Callable[[dict], None] = lambda data: None
        # A curslink.files.transfer.FileReceiver takes dropped files; without
        # one the agent doesn't offer the "files" feature.
        self.files = None
        # A curslink.files.dragout.AgentDragOut hands file drags leaving
        # this device over to the host (drag_out, fetch, file_result).
        self.drag_out = None
        # A curslink.popups.AgentPopups opens and closes Isolink's floating
        # chats the host carries here (popup_in, popup_close).
        self.popups = None
        # A curslink.mirror.MirrorPeer captures and shows mirror mode's
        # covered areas (mirror, mirror_frame, mirror_pointer, mirror_ack);
        # without one the agent doesn't offer the "mirror" feature.
        self.mirror = None
        # A curslink.clipboard.sync.ClipboardSync shares the clipboard with
        # the host (clip, clip_data); without one the agent doesn't offer
        # the "clipboard" feature.
        self.clipboard = None
        # A curslink.cursorhide.CursorHider: this device's own cursor is
        # hidden while input is on another device ([agent] hide_cursor).
        self.cursor = None
        # Set while the app's keyboard check is listening: keys from the host
        # go here (as sent, before [agent].modifier_map) instead of being typed.
        self.learn: Callable[[str, bool], None] | None = None
        self.active = False
        # How many times input has come here (each "enter"): mirror mode's
        # CursorPark moves this device's idle cursor aside once per time away.
        self.entered = 0
        self.keys: set[str] = set()
        self.buttons: set[str] = set()
        # The newest move's ts (either channel): UDP can deliver one late, and
        # an older position than one already shown would step the pointer back.
        self._move_ts: float | None = None
        # The host's measure of the round trip, from its pings (see protocol).
        self.host_link: dict = {}

    def new_connection(self) -> None:
        """A fresh connection (maybe to a restarted host, with a new clock)."""
        self._move_ts = None

    def _fresh(self, ts) -> bool:
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            return True
        if self._move_ts is not None and ts < self._move_ts:
            return False
        self._move_ts = ts
        return True

    def udp_move(self, x: float, y: float, ts: float) -> None:
        """A move that came by UDP (see curslink.net.udp)."""
        if not self.active:
            return  # sent before a leave that TCP brought first, or before our enter
        self.handle(Message("move", {"x": x, "y": y, "ts": ts}))

    def handle(self, msg: Message) -> bytes | None:
        t = msg.t
        if t == "ping":
            if "rtt_ms" in msg.data:
                self.host_link = {k: msg.data.get(k) for k in ("rtt_ms", "p95_ms", "jitter_ms", "tuning")}
            reply = {"n": msg.get("n")}
            if "ts" in msg.data:
                reply["ts"] = msg.data["ts"]  # lets the host time the round trip
            return encode("pong", **reply)
        if t == "move":
            ts = msg.data.get("ts")
            if not self._fresh(ts):
                return None
            if self.drag_out is not None and "left" in self.buttons:
                self.drag_out.on_pointer(msg.data["x"], msg.data["y"])
            if self.smoother is not None and isinstance(ts, (int, float)) and not isinstance(ts, bool):
                self.smoother.add(msg.data["x"], msg.data["y"], ts)
            else:
                self.sink.move_to(int(msg.data["x"]), int(msg.data["y"]))
            return None
        if self.smoother is not None and t in ("button", "scroll", "key"):
            self.smoother.snap()  # these happen where the pointer is now, not where playback is
        if t == "enter":
            self.active = True
            self.entered += 1
            x, y = int(msg.data["x"]), int(msg.data["y"])
            if self.smoother is not None:
                self.smoother.reset(x, y)
            else:
                self.sink.move_to(x, y)
            if self.cursor is not None:
                self.cursor.show()  # after the move: it shows where it lands
            box = msg.data.get("box")
            if isinstance(box, list) and len(box) == 4 and all(
                    isinstance(v, (int, float)) and not isinstance(v, bool) for v in box) and box[2] > 0 and box[3] > 0:
                self.show_effect(msg.get("effect", "none"), x, y, tuple(int(v) for v in box))
            else:
                self.show_effect(msg.get("effect", "none"), x, y)
        elif t == "leave":
            self.release_all()
            if "x" in msg.data and "y" in msg.data:  # where the pointer left from (0.4.17+ hosts)
                self.show_effect(msg.get("effect", "none"), int(msg.data["x"]), int(msg.data["y"]))
            park = msg.data.get("park")
            if isinstance(park, list) and len(park) == 2 and not (
                    self.drag_out is not None and getattr(self.drag_out, "busy", False)):
                try:
                    self.sink.move_to(int(park[0]), int(park[1]))
                except Exception:
                    log.debug("couldn't move the cursor aside", exc_info=True)
            if self.cursor is not None:
                self.cursor.hide()  # the pointer is on another device now
        elif t == "look":
            self.on_look(dict(msg.data))
        elif t == "hint":
            self.on_hint(dict(msg.data))
        elif t == "edge":
            if self.on_edge is not None:
                self.on_edge(dict(msg.data))
        elif t == "drag":
            self.on_drag(dict(msg.data))
        elif t == "drag_out":
            if self.drag_out is not None:
                # The host won't send this button's release: the drag is ours to end.
                self.buttons.discard("left")
                self.drag_out.start()
        elif t in ("popup_in", "popup_close", "popup_prepare", "popup_unprepare"):
            if self.popups is not None:
                self.popups.handle(t, dict(msg.data))
        elif t in ("mirror", "mirror_frame", "mirror_pointer", "mirror_ack"):
            if self.mirror is not None:
                self.mirror.handle(t, dict(msg.data))
        elif t in ("clip", "clip_data"):
            if self.clipboard is not None:
                self.clipboard.handle("host", t, dict(msg.data))
        elif t == "fetch":
            if self.drag_out is not None:
                self.drag_out.fetch(dict(msg.data))
        elif t == "file_result" and self.drag_out is not None and self.drag_out.owns(msg.get("id")):
            self.drag_out.result(dict(msg.data))
        elif t.startswith("file_"):
            if self.files is not None:
                return self.files.handle(msg)
        elif t == "button":
            name, pressed = msg.data["button"], msg.data["pressed"]
            if not pressed and name not in self.buttons:
                return None
            (self.buttons.add if pressed else self.buttons.discard)(name)
            self.sink.button(name, pressed)
            if self.drag_out is not None:
                self.drag_out.on_button(name, pressed)
        elif t == "scroll":
            self.scrolls.add(msg.data["dx"], msg.data["dy"])
            try:
                self.sink.scroll(msg.data["dx"], msg.data["dy"])
            except Exception:
                log.exception("couldn't scroll by (%s, %s)", msg.data["dx"], msg.data["dy"])
        elif t == "key":
            key, pressed = msg.data["key"], msg.data["pressed"]
            if self.learn is not None and key not in self.keys:
                self.learn(key, pressed)
                return None
            if not pressed and key not in self.keys:
                return None  # a release whose press happened elsewhere
            (self.keys.add if pressed else self.keys.discard)(key)
            self.sink.key(key, pressed)
        return None

    def set_learning(self, learn: Callable[[str, bool], None] | None) -> None:
        """Start (or stop) handing the host's keys to ``learn``; keys held
        now are let go first so none sticks while they aren't typed."""
        if learn is not None:
            for key in sorted(self.keys):
                with contextlib.suppress(Exception):
                    self.sink.key(key, False)
            self.keys.clear()
        self.learn = learn

    def release_all(self) -> None:
        """Input left this device (or the host went away)."""
        self.active = False
        if self.smoother is not None:
            self.smoother.pause()
        self.on_drag({"hide": True})
        if self.drag_out is not None:
            self.drag_out.on_button("left", False)
        for key in sorted(self.keys):
            with contextlib.suppress(Exception):
                self.sink.key(key, False)
        for button in sorted(self.buttons):
            with contextlib.suppress(Exception):
                self.sink.button(button, False)
        self.keys.clear()
        self.buttons.clear()


def detect_screens() -> list[dict]:
    """This device's monitors, as hello.screens has them (see curslink.displays)."""
    from curslink.displays import os_screens

    return os_screens()


# How often a connected agent looks for monitors plugged in, taken away or
# moved, and tells a host that takes them (a screens frame).
SCREENS_EVERY_S = 3.0


@dataclass
class Target:
    """Where and how to connect: what :class:`AgentClient`'s resolver returns."""

    host: str                      # "" = relay only
    port: int
    ssl_context: ssl.SSLContext | None
    auth_key: bytes | None = None  # proves our identity in hello
    relay: RelayTarget | None = None


class NotReady(Exception):
    """The resolver has nowhere to connect yet (not paired, host not found)."""


class AgentClient:
    def __init__(
        self,
        device_id: str,
        name: str,
        host: str,
        port: int,
        ssl_context: ssl.SSLContext | None,
        session: AgentSession,
        max_backoff_s: float = 10.0,
        auth_key: bytes | None = None,
        resolve: Callable[[], Awaitable[Target]] | None = None,
        relay: RelayTarget | None = None,
        udp: bool = True,
        optimise: bool = False,
    ):
        if not host and relay is None and resolve is None:
            raise ValueError("an agent needs a host address, a relay, or both")
        self.device_id = device_id
        self.name = name
        self.host = host
        self.port = port
        self.ssl_context = ssl_context
        self.auth_key = auth_key
        self.session = session
        self.max_backoff_s = max_backoff_s
        # Re-run before every connection attempt, so pairing or a host that
        # moved to a new address is picked up without a restart.
        self.resolve = resolve
        self.relay = relay
        self.udp_enabled = udp  # take moves by UDP when the host is reached directly
        self.udp: AgentUdp | None = None
        # Mark our packets low-latency (see curslink.net.quality).
        self.optimise = optimise
        self.via = ""
        self._direct = False
        self.connected = asyncio.Event()
        # ("connected", via) / ("disconnected", reason) / ("waiting", why), for the desktop app.
        self.on_state: Callable[[str, str], None] = lambda state, detail: None
        # Set when this agent runs its own camera: says whether the user is
        # facing this device right now (see curslink.gaze.confirm).
        self.facing_source: Callable[[], bool] | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._host_takes_facing = False
        self._screens_sent: list[dict] = []  # what the host was last told (hello or screens)
        self.host_features: frozenset[str] = frozenset()

    def report_facing(self, facing: bool) -> None:
        """Tell the host straight away that our camera's view changed."""
        writer = self._writer
        if writer is None or not self._host_takes_facing or writer.transport.is_closing():
            return
        writer.write(encode("facing", facing=bool(facing)))

    def send(self, t: str, **data) -> None:
        """A frame to the host now, if connected (dropped otherwise)."""
        writer = self._writer
        if writer is not None and not writer.transport.is_closing():
            writer.write(encode(t, **data))

    async def send_bulk(self, t: str, **data) -> None:
        """A frame of a big transfer to the host, waiting while the
        connection is busy. Raises ConnectionError once it's gone."""
        writer = self._writer
        if writer is None or writer.transport.is_closing():
            raise ConnectionError("not connected to the host")
        writer.write(encode(t, **data))
        while writer.transport.get_write_buffer_size() > 64 * 1024:
            if self._writer is not writer or writer.transport.is_closing():
                raise ConnectionError("the host disconnected")
            await asyncio.sleep(0.002)

    def reconnect(self) -> None:
        """Drop the connection; run_forever makes a new one straight away
        (resolving the host again, so a better path is picked up)."""
        writer = self._writer
        if writer is not None and not writer.transport.is_closing():
            writer.close()

    @property
    def path(self) -> str:
        """``relay``, ``udp`` (direct, moves by UDP), ``tcp`` (direct) or "" (not connected)."""
        if self._writer is None:
            return ""
        if not self._direct:
            return "relay"
        return "udp" if self.udp is not None and self.udp.heard_lately() else "tcp"

    def link_report(self) -> dict:
        """How the link to the host looks from here, for the app's Network settings."""
        writer = self._writer
        local = ""
        if writer is not None:
            name = writer.get_extra_info("sockname")
            local = str(name[0]) if isinstance(name, tuple) and name else ""
        host = self.session.host_link
        udp = self.udp
        rtt = udp.rtt.stats() if udp is not None else {"samples": 0}
        if host.get("rtt_ms") is not None:
            # The host's measure (pings over TLS) covers every path; ours only UDP.
            rtt = {**rtt, "samples": max(rtt.get("samples") or 0, 1), "p50_ms": host["rtt_ms"],
                   "p95_ms": host.get("p95_ms"), "jitter_ms": host.get("jitter_ms")}
        return {
            "device_id": "host",
            "name": "",
            "path": self.path,
            "via": self.via if writer is not None else "",
            "udp_offered": udp is not None,
            "rtt": rtt,
            "loss_pct": udp.loss.stats()["loss_pct"] if udp is not None else None,
            "optimise": self.optimise,
            "tuning": {"reason": host.get("tuning") or ""},
            "local": local,
        }

    async def _report_netstats(self) -> None:
        while True:
            await asyncio.sleep(NETSTATS_EVERY_S)
            udp = self.udp
            if udp is None:
                continue
            rtt = udp.rtt.stats()
            self.send("netstats", udp=udp.heard_lately(), loss_pct=udp.loss.stats()["loss_pct"],
                      rtt_ms=rtt["p50_ms"], jitter_ms=rtt["jitter_ms"])

    async def _watch_screens(self) -> None:
        """Tell the host when this device's monitors change (one plugged in,
        taken away, rearranged or rescaled), so its layout can follow."""
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(SCREENS_EVERY_S)
            try:
                screens = await loop.run_in_executor(None, detect_screens)
            except Exception:  # noqa: BLE001 - try again next time
                log.debug("couldn't list the monitors", exc_info=True)
                continue
            if not screens or screens == self._screens_sent:
                continue
            log.info("this device's monitors changed: %s", ", ".join(
                f"{s['id']} {s['width']}x{s['height']} at {s['x']},{s['y']}" for s in screens))
            self._screens_sent = screens
            frame = {"screens": screens}
            if self.session.mirror is not None:
                from curslink.screensize import physical_sizes

                frame["screen_mm"] = await loop.run_in_executor(None, physical_sizes)
            self.send("screens", **frame)

    async def _refresh_facing(self) -> None:
        while True:
            if self.facing_source is not None:
                self.report_facing(self.facing_source())
            await asyncio.sleep(FACING_REFRESH_S)

    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """LAN first (fast, direct); fall back to the relay when there is one."""
        self._direct = False
        if self.host:
            try:
                streams = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port, ssl=self.ssl_context, limit=STREAM_LIMIT),
                    LAN_ATTEMPT_TIMEOUT_S if self.relay else LAN_ONLY_TIMEOUT_S,
                )
                self.via = f"{self.host}:{self.port}"
                self._direct = True
                return streams
            except (OSError, asyncio.TimeoutError) as exc:
                if self.relay is None:
                    if isinstance(exc, asyncio.TimeoutError):
                        raise OSError(f"{self.host}:{self.port} didn't answer in {LAN_ONLY_TIMEOUT_S:.0f}s") from exc
                    raise
                log.debug("host not reachable on the LAN (%s); trying the relay", exc)
        assert self.relay is not None
        if self.ssl_context is None:
            raise ValueError("relay connections need the pairing secret")
        streams = await agent_connect(self.relay.address, self.relay.key, self.relay.room, self.ssl_context)
        self.via = f"relay {self.relay.address}"
        return streams

    async def run_once(self) -> None:
        if self.resolve is not None:
            target = await self.resolve()
            self.host, self.port, self.relay = target.host, target.port, target.relay
            self.ssl_context, self.auth_key = target.ssl_context, target.auth_key
        reader, writer = await self._open()
        self.session.new_connection()
        self.session.host_link = {}
        if self.optimise:
            quality.mark_low_latency(quality.transport_socket(writer))
        try:
            features = list(AGENT_FEATURES) + ([FILES_FEATURE, DROP_FEATURE, FILE_ACK_FEATURE] if self.session.files is not None else [])
            if self.session.drag_out is not None:
                features.append(FILES_OUT_FEATURE)
            if self.session.popups is not None:
                features += [POPUPS_FEATURE, PREPARE_FEATURE]
            if self.session.on_edge is not None:
                features.append(EDGE_FEATURE)
            if self.udp_enabled and self._direct:
                features.append(UDP_FEATURE)
            if self.session.mirror is not None:
                features += [MIRROR_FEATURE, MIRROR_CURSOR_FEATURE, MIRROR_TILES_FEATURE]
            features.append(NETSTATS_FEATURE)
            if self.session.clipboard is not None:
                features.append(CLIPBOARD_FEATURE)
            hello = dict(
                v=PROTOCOL_VERSION,
                device_id=self.device_id,
                name=self.name,
                platform=f"{platform.system()} {platform.release()}",
                screens=detect_screens(),
                features=features,
            )
            if self.session.mirror is not None:
                from curslink.screensize import physical_sizes

                hello["screen_mm"] = physical_sizes()
            self._screens_sent = hello["screens"]
            if self.facing_source is not None:
                hello["camera"] = True
            if self.auth_key is not None:
                binding = channel_binding(writer.get_extra_info("ssl_object"))
                hello["auth"] = hello_auth(self.auth_key, binding, self.device_id)
            writer.write(encode("hello", **hello))
            welcome = await asyncio.wait_for(read_message(reader), 5)
            if welcome is not None and welcome.t == "refused":
                raise ProtocolError(f"the host refused us: {welcome.get('reason', '')}")
            if welcome is None or welcome.t != "welcome":
                raise ProtocolError("host did not welcome us")
            log.info("connected to host via %s", self.via)
            features = welcome.get("features")
            self.host_features = (frozenset(f for f in features if isinstance(f, str))
                                  if isinstance(features, list) else frozenset())
            self._host_takes_facing = "facing" in self.host_features
            if self.facing_source is not None and not self._host_takes_facing:
                log.info("the host is too old to use this device's camera for gaze confirmation")
            offer = welcome.get("udp")
            if isinstance(offer, dict) and self._direct and self.udp_enabled:
                await self._start_udp(offer, writer)
            self._writer = writer
            self.connected.set()
            self.on_state("connected", self.via)
            refresher = None
            if self.facing_source is not None and self._host_takes_facing:
                refresher = asyncio.create_task(self._refresh_facing())
            reporter = (asyncio.create_task(self._report_netstats())
                        if NETSTATS_FEATURE in self.host_features else None)
            watcher = (asyncio.create_task(self._watch_screens())
                       if SCREENS_FEATURE in self.host_features else None)
            try:
                while True:
                    msg = await read_message(reader)
                    if msg is None:
                        break
                    if msg.t in WARM_ON and self.udp is not None:
                        self.udp.warm()  # input is here: keep this end's Wi-Fi awake
                    reply = self.session.handle(msg)
                    if reply:
                        writer.write(reply)
            finally:
                if refresher is not None:
                    refresher.cancel()
                if reporter is not None:
                    reporter.cancel()
                if watcher is not None:
                    watcher.cancel()
        finally:
            if self.udp is not None:
                self.udp.close()
                self.udp = None
            self._writer = None
            self._host_takes_facing = False
            if self.connected.is_set():
                self.on_state("disconnected", "")
            self.connected.clear()
            self.session.release_all()
            if self.session.cursor is not None:
                self.session.cursor.show()  # no host: this device's own mouse is all there is
            if self.session.files is not None:
                self.session.files.close_all()
            if self.session.drag_out is not None:
                self.session.drag_out.close()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _start_udp(self, offer: dict, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        if not peer:
            return
        try:
            self.udp = AgentUdp(offer, self.session.udp_move, mark=self.optimise, warm=self.optimise,
                                active=lambda: self.session.active)
            await self.udp.start(peer[0])
        except (ValueError, OSError) as exc:
            log.info("pointer moves will come over TCP: can't use UDP (%s)", exc)
            self.udp = None

    async def run_forever(self) -> None:
        backoff = 0.5
        waiting = ""
        while True:
            try:
                await self.run_once()
                backoff = 0.5
                log.info("disconnected from host; reconnecting")
            except ssl.SSLError as exc:
                log.error("TLS handshake failed (are these devices paired, or is the secret the same on both?): %s", exc)
            except NotReady as exc:
                if str(exc) != waiting:  # say it once, not every retry
                    waiting = str(exc)
                    log.info("%s", exc)
                    self.on_state("waiting", waiting)
                await asyncio.sleep(2.0)
                continue
            except (OSError, ProtocolError, RelayError, asyncio.TimeoutError) as exc:
                log.info("host unavailable: %s", exc)
                self.on_state("waiting", f"host unavailable: {exc}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self.max_backoff_s)
