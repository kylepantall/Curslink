"""Wiring the pieces together into the runnable host and agent."""

from __future__ import annotations

import asyncio
import itertools
import logging
import math
import sys
import threading
import time
from dataclasses import replace
from typing import Callable

from curslink import events
from curslink.config import AgentConfig, HostConfig, PairingConfig
from curslink.gaze.confirm import FacingDetector, GazeArbiter
from curslink.gaze.zones import Calibration, GazeClassifier, Target
from curslink.inputs.keys import swap_cmd_ctrl, swap_for
from curslink.net.client import AgentClient, AgentSession, NotReady
from curslink.net.client import Target as ConnectTarget
from curslink.net.relay import HostRelayLink, RelayTarget, paired_room_id
from curslink.net.server import HostServer
from curslink.pairing.handshake import DeviceInfo
from curslink.pairing.store import PairingStore
from curslink.router import Router, SwitchEvent
from curslink.security import HelloVerifier, client_context, server_context
from curslink.tally import Tally

log = logging.getLogger(__name__)


class CaptureStats:
    """How the mouse reports reach the loop while input is on another device:
    how many, how many moves they became, and how long each waited for the
    loop. Logged every so often by :func:`report_capture`."""

    def __init__(self) -> None:
        self.reports = 0
        self.moves = 0
        self.waits: list[float] = []

    def sent(self, waited: float) -> None:
        self.moves += 1
        if len(self.waits) < 20000:
            self.waits.append(waited)

    def take(self) -> tuple[int, int, list[float]]:
        out = (self.reports, self.moves, self.waits)
        self.reports, self.moves, self.waits = 0, 0, []
        return out


class LoopBridge:
    """Hands input callbacks from pynput's threads to the asyncio loop.

    Relative motion is added up until the loop gets to it, so a fast mouse
    (1000 reports a second) sends the agent one move per loop turn instead
    of a backlog of tiny ones it then replays late. Buttons, scrolls and keys
    take any motion still waiting with them, so a click lands where the
    pointer was when you clicked.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, router: Router, drag=None):
        self._loop = loop
        self._router = router
        # A curslink.files.handoff.DragHandoff watches the left button and
        # the pointer for file drags, and takes a drop on another device.
        self._drag = drag
        self._lock = threading.Lock()
        self._dx = 0.0
        self._dy = 0.0
        self._ts = 0.0   # when the newest motion waiting was captured
        self._pending = False
        self._gen = 0  # a flush scheduled before the last take is stale
        self.stats = CaptureStats()
        self.scrolls = Tally("scroll from this mouse")

    def on_pointer(self, x, y):
        self._loop.call_soon_threadsafe(self._pointer, x, y)

    def _pointer(self, x, y):
        if self._drag is not None:
            self._drag.on_pointer(x, y)
        self._router.on_pointer(x, y)

    def _button(self, button, pressed):
        if self._drag is not None and self._drag.on_button(button, pressed):
            return  # a file drop on another device: it's handled
        self._router.on_button(button, pressed)

    def on_motion(self, dx, dy):
        now = time.monotonic()
        with self._lock:
            self._dx += dx
            self._dy += dy
            self._ts = now
            self.stats.reports += 1
            if self._pending:
                return
            self._pending = True
            gen = self._gen
        self._loop.call_soon_threadsafe(self._flush, gen)

    def _take(self) -> tuple[float, float, float]:
        with self._lock:
            dx, dy, ts = self._dx, self._dy, self._ts
            self._dx = self._dy = 0.0
            self._pending = False
            self._gen += 1
        return dx, dy, ts

    def _motion(self, dx: float, dy: float, ts: float) -> None:
        self.stats.sent(time.monotonic() - ts)
        self._router.on_motion(dx, dy, ts)

    def _flush(self, gen: int) -> None:
        with self._lock:
            if gen != self._gen:
                return  # a button or key took this motion with it
        dx, dy, ts = self._take()
        if dx or dy:
            self._motion(dx, dy, ts)

    def _after_motion(self, fn, *args):
        """Run ``fn`` on the loop after the motion waiting now."""
        dx, dy, ts = self._take()

        def run():
            if dx or dy:
                self._motion(dx, dy, ts)
            fn(*args)

        self._loop.call_soon_threadsafe(run)

    def on_button(self, button, pressed):
        self._after_motion(self._button, button, pressed)

    def on_scroll(self, dx, dy):
        self.scrolls.add(dx, dy)
        self._after_motion(self._router.on_scroll, dx, dy)

    def on_key(self, key, pressed):
        self._after_motion(self._router.on_key, key, pressed)


class Pairing:
    """The pairing listener plus the LAN advertisement, for a running host or agent."""

    def __init__(self, cfg: PairingConfig, me: DeviceInfo, store: PairingStore, control_port: int | None = None):
        self.cfg = cfg
        self.me = me
        self.store = store
        self.control_port = control_port
        self.listener = None
        self.advertiser = None

    async def start(self) -> None:
        if not self.cfg.enabled:
            return
        from curslink.pairing.discovery import Advertiser
        from curslink.pairing.service import PairingListener, ask_in_dialog

        # The advert carries the real port, so if ours is taken (say a host and
        # an agent on one machine) any free port will do.
        for port in (self.cfg.port, 0):
            self.listener = PairingListener(self.me, self.store, ask_in_dialog, port=port)
            try:
                await self.listener.start()
                break
            except OSError as exc:
                log.info("can't listen for pairing on port %d (%s)", port, exc)
                self.listener = None
        if self.listener is None:
            log.warning("pairing unavailable on this device")
            return
        self.advertiser = Advertiser(self.me, self.listener.port, self.control_port)
        try:
            await self.advertiser.start()
        except Exception as exc:  # no multicast, no mDNS: pairing by address still works
            log.warning("can't advertise on the LAN (%s); pair with `curslink pair --address`", exc)
            self.advertiser = None

    async def close(self) -> None:
        if self.advertiser:
            await self.advertiser.close()
        if self.listener:
            await self.listener.close()


class HostRelays:
    """The host's links to the relay: one room for the shared secret (if
    any) plus one room per paired agent, kept in step with the pairing store."""

    def __init__(self, target: RelayTarget, host_id: str, shared_room: bool, store: PairingStore, tls, handle,
                 refresh_s: float = 5.0):
        self.target = target
        self.host_id = host_id
        self.shared_room = shared_room
        self.store = store
        self.tls = tls
        self.handle = handle
        self.refresh_s = refresh_s
        self.links: dict[str, HostRelayLink] = {}
        self._task: asyncio.Task | None = None

    def rooms(self) -> set[str]:
        rooms = {paired_room_id(p.key, self.host_id) for p in self.store.peers("agent")}
        if self.shared_room and self.target.room:
            rooms.add(self.target.room)
        return rooms

    async def sync(self) -> None:
        wanted = self.rooms()
        for room in set(self.links) - wanted:  # unpaired or re-paired
            await self.links.pop(room).close()
        for room in wanted - set(self.links):
            link = HostRelayLink(replace(self.target, room=room), self.tls, self.handle)
            link.start()
            self.links[room] = link

    def start(self) -> None:
        async def loop():
            while True:
                try:
                    await self.sync()
                except ValueError as exc:  # unreadable pairing store
                    log.warning("%s", exc)
                await asyncio.sleep(self.refresh_s)

        self._task = asyncio.create_task(loop())

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
        for link in self.links.values():
            await link.close()
        self.links.clear()


async def run_host(cfg: HostConfig) -> None:
    from curslink.inputs.pynput_backend import PynputCapture, PynputSink
    from curslink.overlay import OverlayProcess

    loop = asyncio.get_running_loop()
    remote_ids = {d for d in cfg.layout.devices if d != cfg.device_id}
    store = PairingStore(cfg.pairing.store)
    tls = server_context(cfg.secret, store.key)
    server = HostServer(
        remote_ids,
        tls,
        host=cfg.listen,
        port=cfg.port,
        verify_hello=HelloVerifier(cfg.secret, store.key),
        udp=cfg.udp,
    )
    if not cfg.secret and not store.peers("agent"):
        log.info("no devices paired yet: run `curslink pair` here or on an agent")
    home = next(iter(cfg.layout.screens(cfg.device_id)))
    capture = PynputCapture(park_at=home.to_os(*home.center))
    overlay = OverlayProcess()
    overlay.start()
    router = Router(
        cfg.layout,
        cfg.device_id,
        capture=capture,
        local_sink=PynputSink(),
        transport=server,
        overlay=overlay,
        settings=cfg.router,
    )
    if cfg.router.swap_cmd_ctrl:
        router.key_for = cmd_ctrl_swapper(server)

    def agent_connected(conn) -> None:
        events.emit("agent", device_id=conn.device_id, name=conn.name, platform=conn.platform,
                    connected=True, screens=conn.screens)

    def agent_disconnected(device_id: str) -> None:
        router.on_disconnect(device_id)
        events.emit("agent", device_id=device_id, connected=False)

    server.on_connect = agent_connected
    server.on_disconnect = agent_disconnected
    router.listeners.append(lambda e: events.emit(
        "switch", **{"from": e.from_device, "to": e.to_device, "screen": e.screen.id, "reason": e.reason}))
    remember_positions(router)
    if cfg.lan:
        await server.start()
    pairing = Pairing(cfg.pairing, DeviceInfo(cfg.device_id, cfg.name, "host"), store,
                      server.port if cfg.lan else None)
    await pairing.start()
    relay = None
    if cfg.relay:
        relay = HostRelays(cfg.relay, cfg.device_id, cfg.secret is not None, store, tls, server.handle_streams)
        relay.start()
        log.info("waiting for agents via relay %s", cfg.relay.address)
    handoff = start_file_drags(cfg, router, server, overlay, capture, loop) if cfg.files else None
    bridge = LoopBridge(loop, router, handoff)
    capture.start(bridge)
    reporter = loop.create_task(report_capture(bridge, capture, server, router))
    router.blocked = input_blocked_reason()

    # Gaze can come from this computer's camera, the agents' own cameras,
    # or both, so the arbiter runs even when this camera is off.
    arbiter = make_arbiter(cfg, router)
    wire_agent_cameras(server, arbiter)
    tracker = start_gaze(cfg, arbiter, loop) if cfg.gaze.enabled else None

    log.info(
        "Curslink host running as %r. Return hotkey: %s, gaze toggle: %s",
        cfg.device_id,
        cfg.router.return_hotkey,
        cfg.router.gaze_toggle_hotkey,
    )
    stop = asyncio.Event()
    events.emit(
        "running", role="host", device_id=cfg.device_id, name=cfg.name,
        active=router.active_device, screen=router.screen.id,
        gaze=tracker is not None, gaze_paused=router.gaze_paused,
    )
    camera = HostCamera(tracker, lambda: start_gaze(cfg, arbiter, loop) if cfg.gaze.enabled else None,
                        lambda: arbiter.set_camera(cfg.device_id, False), loop)

    def gaze_paused(paused: bool) -> None:
        events.emit("gaze", paused=paused)
        loop.create_task(camera.pause() if paused else camera.resume(router))

    router.on_gaze_paused = gaze_paused

    async def calibrate_on() -> None:
        """The app is calibrating: give it the camera, and keep input here.
        Linking stays up so agents can show "Look here" on their screens; the
        app restarts us afterwards, which brings gaze back."""
        if not router.is_local:
            router.go_home("app")
        arbiter.suspended = True  # agents' cameras mustn't take the pointer mid-calibration
        await camera.pause(for_good=True)
        events.emit("calibrating", on=True)

    def command(line: str) -> None:
        if line == "calibrate on":
            loop.create_task(calibrate_on())
        elif line.startswith("look "):
            look_command(server, line[5:])
        elif line.startswith("hint "):
            hint_command(server, line[5:])
        else:
            host_command(router, stop, line)

    if events.channel.enabled:
        events.read_commands(loop, command)
    try:
        await stop.wait()
    finally:
        reporter.cancel()
        camera.stop()
        capture.stop()
        overlay.close()
        await pairing.close()
        if relay:
            await relay.close()
        await server.close()


def cmd_ctrl_swapper(server: HostServer, local_platform: str = sys.platform):
    """Swap Cmd and Ctrl for keys going to a computer of the other kind (Mac vs PC)."""
    def key_for(device_id: str, key: str) -> str:
        conn = server.connections.get(device_id)
        if conn is not None and swap_for(local_platform, conn.platform):
            return swap_cmd_ctrl(key)
        return key
    return key_for


class HostCamera:
    """This computer's camera for gaze: paused gaze lets go of it (its light
    goes off), resuming opens it again. Calibration takes it for good; the
    app restarts the host afterwards."""

    def __init__(self, tracker, start: Callable[[], object], released: Callable[[], None],
                 loop: asyncio.AbstractEventLoop):
        self.tracker = tracker
        self._start = start
        self._released = released
        self._loop = loop
        self._lock = asyncio.Lock()
        self._held = False  # calibrating: don't reopen it

    async def pause(self, for_good: bool = False) -> None:
        async with self._lock:
            self._held = self._held or for_good
            running, self.tracker = self.tracker, None
            if running is None:
                return
            await self._loop.run_in_executor(None, running.stop)
            self._released()
            log.info("camera released (%s)", "calibrating" if for_good else "gaze paused")

    async def resume(self, router: Router) -> None:
        async with self._lock:
            if self.tracker is not None or self._held or router.gaze_paused:
                return
            self.tracker = self._start()
            if self.tracker is not None:
                log.info("gaze resumed: camera reopened")

    def stop(self) -> None:
        if self.tracker is not None:
            self.tracker.stop()
            self.tracker = None


def start_file_drags(cfg: HostConfig, router: Router, server: HostServer, overlay, capture,
                     loop: asyncio.AbstractEventLoop):
    """Dragging files to another device (see curslink.files.handoff)."""
    from curslink.files import macdrag
    from curslink.files.handoff import DragHandoff

    # macOS says what's being dragged on its drag pasteboard; elsewhere the
    # overlay helper's drop targets find out.
    drag_source = macdrag.drag_pasteboard if macdrag.available() else None
    from curslink.files.native import clean_old

    # A real drag of files arriving from an agent needs the real button-up
    # held back until they're all here, which the Mac's event tap can do.
    native_press = None
    if sys.platform == "darwin":
        from curslink.inputs.pynput_backend import press_left_here as native_press
    handoff = DragHandoff(router, server, overlay.command, capture.inject_drag_end, cfg.name, emit=events.emit,
                          drag_source=drag_source, native_press=native_press,
                          release_held=getattr(capture, "release_held", lambda: None))
    if hasattr(capture, "hold_left_up"):
        capture.hold_left_up = handoff.native.holding
    clean_old()
    log.info("file drags: %s", "read from the drag pasteboard" if drag_source else "found by drop targets at the edges")

    def on_line(line: str) -> None:  # the helper's reader thread
        try:
            loop.call_soon_threadsafe(handoff.on_helper, line)
        except RuntimeError:  # the loop has closed
            pass

    overlay.on_line = on_line
    server.on_file_result = handoff.on_result
    server.on_drag_out = handoff.on_drag_out
    server.on_files = handoff.on_files
    return handoff


def _ms(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * q))] * 1000.0


def capture_report(bridge: LoopBridge, capture, server: HostServer, device_id: str, span: float) -> str | None:
    """One log line on the host's half of the pointer path, or None if the
    mouse didn't move: reports from the mouse, moves sent, how long reports
    waited for the loop and took in the event tap, and the network round trip."""
    reports, moves, waits = bridge.stats.take()
    taps = getattr(capture, "tap_times", None)
    tap_times = list(taps) if taps is not None else []
    if taps is not None:
        taps.clear()
    conn = server.connections.get(device_id)
    rtts = list(conn.rtts) if conn is not None else []
    if conn is not None:
        conn.rtts.clear()
    if not reports:
        return None
    line = (f"pointer to {device_id}: {reports} mouse reports in {span:.0f}s ({reports / span:.0f}/s) "
            f"sent as {moves} moves; waited for the loop p50 {_ms(waits, 0.5):.2f} / "
            f"p99 {_ms(waits, 0.99):.2f} / max {_ms(waits, 1.0):.2f} ms")
    if tap_times:
        line += f"; event tap took p50 {_ms(tap_times, 0.5):.2f} / max {_ms(tap_times, 1.0):.2f} ms"
    path = getattr(conn, "udp", None)
    if path is not None:
        line += f"; {path.sent} by UDP" if path.up else "; UDP path down, all by TCP"
        path.sent = 0
    if rtts:
        line += (f"; network round trip p50 {_ms(rtts, 0.5):.1f} / p95 {_ms(rtts, 0.95):.1f} / "
                 f"max {_ms(rtts, 1.0):.1f} ms")
    return line


async def report_capture(bridge: LoopBridge, capture, server: HostServer, router: Router,
                         every_s: float = 10.0) -> None:
    """While input is on another device, log how its pointer path is doing,
    so a diagnostics bundle shows where any lag comes from."""
    target = None
    while True:
        await asyncio.sleep(every_s)
        target = router.active_device if not router.is_local else target
        if target is None:
            continue
        line = capture_report(bridge, capture, server, target, every_s)
        if line:
            log.info("%s", line)
        if router.is_local:
            target = None


def input_blocked_reason():
    """For Router.blocked: on macOS, input can't be captured (so it mustn't
    leave this Mac) until Accessibility and Input Monitoring are allowed.
    macOS forgets them when an update's signature changes."""
    from curslink import macperms

    if not macperms.MAC:
        return lambda: ""
    state = {"checked": 0.0, "why": ""}

    def blocked() -> str:
        now = time.monotonic()
        if now - state["checked"] >= 5.0:
            state["checked"] = now
            missing = [k for k in (macperms.ACCESSIBILITY, macperms.INPUT_MONITORING) if not macperms.granted(k)]
            state["why"] = ("macOS hasn't allowed " + " and ".join(m.replace("-", " ").title() for m in missing)
                            + " for Curslink, so it can't capture the mouse and keyboard. Allow them in "
                            "System Settings > Privacy & Security, then restart Curslink.") if missing else ""
        return state["why"]

    return blocked


def host_command(router: Router, stop: asyncio.Event, command: str) -> None:
    """One command from the desktop app (see :mod:`curslink.events`)."""
    if command == "stop":
        stop.set()
    elif command == "home":
        if not router.is_local:
            router.go_home("app")
    elif command == "gaze pause":
        router.set_gaze_paused(True)
    elif command == "gaze resume":
        router.set_gaze_paused(False)
    elif command == "gaze toggle":
        router.set_gaze_paused(not router.gaze_paused)
    else:
        log.debug("unknown command %r", command)


def look_command(server: HostServer, payload: str) -> None:
    """``look {"device": ..., "screen": ..., ...}`` shows the calibration's
    "Look here" card on that agent's screen; ``look {"hide": true}`` takes it
    down everywhere."""
    import json

    try:
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except ValueError as exc:
        log.debug("bad look command %r: %s", payload, exc)
        return
    device = data.pop("device", None)
    for device_id in list(server.connections):
        if not server.supports(device_id, "look"):
            continue
        if device_id == device and not data.get("hide"):
            server.send(device_id, "look", **data)
        else:
            server.send(device_id, "look", hide=True)


def hint_command(server: HostServer, payload: str) -> None:
    """``hint {"devices": {device_id: {"screens": [...]}}}`` marks where
    screens overlap on those agents' monitors while the Layout page is being
    edited (see curslink.gui.overlaphint); agents not listed take theirs down."""
    import json

    try:
        data = json.loads(payload)
        devices = data.get("devices", {}) if isinstance(data, dict) else None
        if not isinstance(devices, dict):
            raise ValueError("no devices")
    except ValueError as exc:
        log.debug("bad hint command %r: %s", payload, exc)
        return
    for device_id in list(server.connections):
        if not server.supports(device_id, "hint"):
            continue
        hint = devices.get(device_id)
        if isinstance(hint, dict) and isinstance(hint.get("screens"), list) and hint["screens"]:
            server.send(device_id, "hint", screens=hint["screens"])
        else:
            server.send(device_id, "hint", hide=True)


def start_gaze(cfg: HostConfig, arbiter: GazeArbiter, loop: asyncio.AbstractEventLoop):
    """This computer's camera. Calibrated, it tells which screen you look
    at; not calibrated, it still tells whether you face this computer, the
    way an agent's camera does, which is enough alongside agents' cameras."""
    try:
        from curslink.gaze.cameras import resolve
        from curslink.gaze.tracker import HeadPoseTracker
    except ImportError as exc:  # pragma: no cover - the gaze extra is missing
        log.error("gaze needs the gaze extra (pip install curslink[gaze]): %s", exc)
        return None
    camera = resolve(cfg.gaze.camera, cfg.gaze.camera_name)
    if arbiter.classifier is None:
        log.info("%s isn't calibrated: camera %d only tells when you face this computer",
                 cfg.gaze.calibration, cfg.gaze.camera)
        detector = FacingDetector(cfg.gaze.facing)
        arbiter.set_camera(cfg.device_id, True)
        facing: FacingCamera

        def on_facing_sample(t: float, yaw: float, pitch: float) -> None:  # tracker thread
            loop.call_soon_threadsafe(facing.on_sample, t, yaw, pitch)

        tracker = HeadPoseTracker(on_facing_sample, camera=camera, preview=cfg.gaze.preview)
        facing = FacingCamera(
            detector, lambda f: arbiter.on_facing(time.monotonic(), cfg.device_id, f), tracker, loop,
            refresh_s=1.0)
        facing.start()
        return facing

    looking = LookingReporter(arbiter.classifier)

    def on_sample(t: float, yaw: float, pitch: float) -> None:
        arbiter.on_sample(t, yaw, pitch)
        looking.sample(yaw, pitch)

    tracker = HeadPoseTracker(
        lambda t, yaw, pitch: loop.call_soon_threadsafe(on_sample, t, yaw, pitch),
        camera=camera,
        preview=cfg.gaze.preview,
    )
    tracker.start()
    return tracker


class LookingReporter:
    """Tells the desktop app which screen the camera thinks you're looking
    at, so the screen maps can light it up live. Sent when it changes and
    once a second besides (the app clears it when these stop)."""

    def __init__(self, classifier):
        self.classifier = classifier
        self.key: str | None = None
        self.sent = 0.0

    def sample(self, yaw: float, pitch: float) -> None:
        target, d = self.classifier.nearest(yaw, pitch)
        key = f"{target.device_id}/{target.screen_id}" if d <= self.classifier.settings.max_distance_deg else ""
        now = time.monotonic()
        if key != self.key or now - self.sent >= 1.0:
            self.key, self.sent = key, now
            events.emit("looking", screen=key)


def make_arbiter(cfg: HostConfig, router: Router) -> GazeArbiter:
    """The host's gaze classifier (when this camera is on and calibrated),
    checked against agents' own cameras, or those cameras on their own."""
    classifier = None
    if cfg.gaze.enabled and cfg.gaze.calibration.exists():
        classifier = GazeClassifier(Calibration.load(cfg.gaze.calibration), cfg.gaze.settings)
    elif cfg.gaze.enabled:
        log.warning("%s does not exist; calibrate for the best gaze switching", cfg.gaze.calibration)
    arbiter = GazeArbiter(
        classifier,
        cfg.device_id,
        switch=router.on_gaze,
        current=lambda: Target(router.active_device, router.screen.id),
        screens_of=lambda device_id: [s.id for s in cfg.layout.screens(device_id)],
        settings=cfg.gaze.confirm,
    )

    if classifier is not None:
        def on_switch(event: SwitchEvent) -> None:
            if event.reason in ("edge", "hotkey"):
                # You moved it by hand: gaze keeps what it sees and only acts
                # when you look somewhere else. Forcing it to the new screen
                # made it "see" you look back at the screen you're still
                # facing 250 ms later, and the pointer ping-ponged across
                # the edge (Kyle's 0.4.16 logs: edge, gaze, edge, gaze...).
                return
            classifier.force(Target(event.to_device, event.screen.id))

        router.listeners.append(on_switch)
        classifier.force(Target(router.active_device, router.screen.id))
    arbiter.on_blocked = lambda target: log.debug(
        "gaze: waiting for %s's camera to confirm you're facing it", target.device_id)
    return arbiter


def wire_agent_cameras(server: HostServer, arbiter: GazeArbiter) -> None:
    """Feed agents' camera reports into the arbiter, keeping the server's
    existing connect/disconnect callbacks."""
    connected, disconnected = server.on_connect, server.on_disconnect

    def cameras_changed() -> None:
        events.emit("cameras", devices=sorted(d for d in arbiter.cameras if d != arbiter.local_id))

    def on_connect(conn) -> None:
        had = conn.device_id in arbiter.cameras
        arbiter.set_camera(conn.device_id, conn.camera)
        if conn.camera:
            log.info("%s's own camera will help decide when you're looking at it", conn.device_id)
        connected(conn)
        if had != conn.camera:
            cameras_changed()

    def on_disconnect(device_id: str) -> None:
        had = device_id in arbiter.cameras
        arbiter.forget(device_id)
        disconnected(device_id)
        if had:
            cameras_changed()

    def on_facing(device_id: str, facing: bool) -> None:
        arbiter.on_facing(time.monotonic(), device_id, facing)
        events.emit("facing", device_id=device_id, facing=facing)

    server.on_connect, server.on_disconnect, server.on_facing = on_connect, on_disconnect, on_facing
    for conn in server.connections.values():  # connected before gaze started
        arbiter.set_camera(conn.device_id, conn.camera)
    if server.connections:
        cameras_changed()


def positions_path():
    from curslink.pairing.store import default_dir

    return default_dir() / "pointer-positions.json"


def remember_positions(router: Router, path=None) -> None:
    """Where the pointer was on each device and monitor survives the host
    restarting, and is saved after every switch."""
    import json

    path = path or positions_path()
    try:
        router.remember(json.loads(path.read_text()))
    except (OSError, ValueError):
        pass

    def save(_event) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(router.remembered()))
            tmp.replace(path)
        except OSError:
            log.debug("couldn't save pointer positions", exc_info=True)

    router.listeners.append(save)


async def run_agent(cfg: AgentConfig) -> None:
    from curslink.inputs.pynput_backend import PynputSink
    from curslink.overlay import OverlayProcess

    # The helper draws the switch animation and, for dropped files, the
    # placeholder that follows the pointer and the "Receiving..." popup.
    overlay = OverlayProcess() if cfg.animations or cfg.files.enabled else None
    if overlay:
        overlay.start()
    from curslink.smoothing import PointerSmoother

    sink = PynputSink(cfg.modifier_map)
    if sys.platform == "win32":
        # The input helper, if it's switched on, reaches admin apps and UAC
        # prompts (see curslink.winhelper); without it, input goes in here.
        from curslink.winhelper import win32 as helper_win32
        from curslink.winhelper.client import HelperSink

        sink = HelperSink(sink, helper_win32.this_session(), cfg.modifier_map)
    smoother = PointerSmoother(sink.move_to) if cfg.smooth_pointer else None
    session = AgentSession(
        sink,
        overlay.show if overlay and cfg.animations else (lambda effect, x, y: None),
        smoother=smoother,
    )
    native = receive_files(cfg, session, overlay) if cfg.files.enabled and overlay is not None else None
    store = PairingStore(cfg.pairing.store)
    client = AgentClient(
        cfg.device_id, cfg.name, cfg.host, cfg.port, None, session,
        resolve=lambda: resolve_host(cfg, store),
        udp=cfg.udp,
    )
    session.on_look = lambda data: events.emit("look", **data)
    if native is not None:
        send_files_home(cfg, session, client, overlay, native, asyncio.get_running_loop())
    session.on_hint = lambda data: events.emit("hint", **data)

    def on_state(state: str, detail: str) -> None:
        if state == "connected" and client.host and not detail.startswith("relay"):
            peer = paired_host(cfg, store)
            if peer is not None and peer.address != client.host:
                store.set_address(peer.device_id, client.host)
        if state != "connected":
            events.emit("look", hide=True)  # the host can't take it down now
            events.emit("hint", hide=True)
        events.emit("host", state=state, detail=detail)

    client.on_state = on_state
    camera = start_facing(cfg, client, asyncio.get_running_loop()) if cfg.gaze.enabled else None
    pairing = Pairing(cfg.pairing, DeviceInfo(cfg.device_id, cfg.name, "agent"), store)
    await pairing.start()
    stop = asyncio.Event()
    if events.channel.enabled:
        events.read_commands(asyncio.get_running_loop(), lambda command: command == "stop" and stop.set())
    events.emit("running", role="agent", device_id=cfg.device_id, name=cfg.name)
    runner = asyncio.create_task(client.run_forever())
    stopper = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({runner, stopper}, return_when=asyncio.FIRST_COMPLETED)
        if runner.done():
            runner.result()  # raise what stopped it
    finally:
        for task in (runner, stopper):
            task.cancel()
        if camera:
            camera.stop()
        if smoother:
            smoother.close()
        await pairing.close()
        if overlay:
            overlay.close()


def receive_files(cfg: AgentConfig, session: AgentSession, overlay):
    """Files dragged here from the host: the placeholder while they're
    carried, then a popup while they arrive (see curslink.files)."""
    import json

    from curslink.files.popups import ReceivePopups
    from curslink.files.transfer import FileReceiver

    from curslink.files.native import NativeDrop, clean_old

    sink = session.sink
    popups = ReceivePopups(overlay.command, events.emit)
    receiver = FileReceiver(cfg.files.folder, popups)

    def cancel_drag() -> None:  # Escape ends a drag without dropping; then let go
        sink.key("esc", True)
        sink.key("esc", False)
        time.sleep(0.05)
        sink.button("left", False)

    # A real drag needs the injected button-down and moves to drive it, which
    # Windows' drag and drop follows; elsewhere the files go to Downloads.
    native = NativeDrop(overlay.command, receiver, popups,
                        press=(lambda: sink.button("left", True)) if sys.platform == "win32" else None,
                        release=lambda: sink.button("left", False), cancel=cancel_drag)
    clean_old()

    def on_drag(data: dict) -> None:
        tid, info = data.get("id"), data.get("card")
        if data.get("drop") and isinstance(tid, str):
            native.let_go(tid, inject=True)
        elif data.get("hide"):
            native.cancel(tid if isinstance(tid, str) else None)
            if not isinstance(tid, str):
                overlay.command("card hide")
        elif isinstance(info, dict) and isinstance(tid, str):
            native.begin(tid, info)  # its bytes are on their way already
        elif isinstance(info, dict):  # an older host: sent when it's dropped
            overlay.command("card " + json.dumps(info, separators=(",", ":")))

    session.on_drag = on_drag
    session.files = receiver
    return native


def send_files_home(cfg: AgentConfig, session: AgentSession, client: AgentClient, overlay, native,
                    loop: asyncio.AbstractEventLoop) -> None:
    """A file dragged from here over the edge to the host (or carried there
    by a look): see curslink.files.dragout."""
    from curslink.files.dragout import AgentDragOut

    drag_out = AgentDragOut(overlay.command, client.send, client.send_bulk, cfg.name)

    def on_line(line: str) -> None:  # the helper's reader thread
        try:
            loop.call_soon_threadsafe(lambda: native.on_helper(line) or drag_out.on_helper(line))
        except RuntimeError:  # the loop has closed
            pass

    overlay.on_line = on_line
    session.drag_out = drag_out


class FacingCamera:
    """A device's own camera, watching for the user facing it. ``report``
    gets each change (an agent sends it to the host; the host's own
    uncalibrated camera hands it to the arbiter), and the current state
    again every ``refresh_s`` when that's set."""

    TICK_S = 0.1

    def __init__(self, detector: FacingDetector, report: Callable[[bool], None], tracker,
                 loop: asyncio.AbstractEventLoop, refresh_s: float | None = None):
        self.detector = detector
        self.report = report
        self.tracker = tracker
        self.loop = loop
        self.refresh_s = refresh_s
        self._ticker: asyncio.Task | None = None

    def on_sample(self, t: float, yaw: float, pitch: float) -> None:  # on the loop
        self._report(self.detector.update(t, yaw, pitch))

    def _report(self, changed: bool | None) -> None:
        if changed is None:
            return
        log.debug("camera: you are %sfacing this device", "" if changed else "not ")
        events.emit("facing", facing=changed)
        self.report(changed)

    async def _tick(self) -> None:
        refreshed = time.monotonic()
        while True:
            await asyncio.sleep(self.TICK_S)
            now = time.monotonic()
            changed = self.detector.tick(now)
            if changed is not None:
                self._report(changed)
                refreshed = now
            elif self.refresh_s is not None and now - refreshed >= self.refresh_s:
                self.report(self.detector.facing)
                refreshed = now

    def start(self) -> None:
        self.tracker.start()
        self._ticker = self.loop.create_task(self._tick())

    def stop(self) -> None:  # from any thread
        if self._ticker:
            self.loop.call_soon_threadsafe(self._ticker.cancel)
        self.tracker.stop()


def start_facing(cfg: AgentConfig, client: AgentClient, loop: asyncio.AbstractEventLoop) -> FacingCamera | None:
    try:
        from curslink.gaze.cameras import resolve
        from curslink.gaze.tracker import HeadPoseTracker
    except ImportError as exc:  # pragma: no cover - the gaze extra is missing
        log.error("[agent.gaze] needs the gaze extra (pip install curslink[gaze]): %s", exc)
        return None
    detector = FacingDetector(cfg.gaze.settings)
    camera: FacingCamera

    def on_sample(t: float, yaw: float, pitch: float) -> None:  # tracker thread
        loop.call_soon_threadsafe(camera.on_sample, t, yaw, pitch)

    tracker = HeadPoseTracker(on_sample, camera=resolve(cfg.gaze.camera, cfg.gaze.camera_name),
                              preview=cfg.gaze.preview)
    camera = FacingCamera(detector, client.report_facing, tracker, loop)
    client.facing_source = lambda: detector.facing
    camera.start()
    log.info("using camera %d to tell the host when you're facing this device", cfg.gaze.camera)
    return camera


def paired_host(cfg: AgentConfig, store: PairingStore):
    hosts = store.peers("host")
    if cfg.host_id:
        return next((p for p in hosts if p.device_id == cfg.host_id), None)
    if len(hosts) > 1:
        names = ", ".join(p.device_id for p in hosts)
        raise NotReady(f"paired with several hosts ({names}); set [agent].host_id to pick one")
    return hosts[0] if hosts else None


async def resolve_host(cfg: AgentConfig, store: PairingStore) -> ConnectTarget:
    """Which key to use and where the host is, worked out again before every connection."""
    peer = paired_host(cfg, store)
    relay = cfg.relay
    if peer is not None:
        ctx, auth_key = client_context(device_id=cfg.device_id, key=peer.key), peer.key
        if relay:
            relay = replace(relay, room=paired_room_id(peer.key, peer.device_id))
    elif cfg.secret:
        ctx, auth_key = client_context(cfg.secret), None
    else:
        raise NotReady("waiting to be paired: run `curslink pair` on the host or here")
    if cfg.host:
        return ConnectTarget(cfg.host, cfg.port, ctx, auth_key, relay)
    if peer is not None:
        from curslink.pairing.discovery import find

        found = await find(peer.device_id, timeout=2.0)
        if found is not None and found.control_port:
            from curslink.pairing.discovery import reachable

            # The host advertises all its addresses, some unreachable from
            # here (VM bridges, VPNs): use one that answers.
            # Last time's address first: it worked then.
            order = sorted(found.addresses, key=lambda a: a != peer.address)
            address = await reachable(order, found.control_port, ssl=ctx)
            if address is None:
                address = peer.address if peer.address in found.addresses else found.address
                log.warning("host found on the LAN, but none of its addresses (%s) answer on port %d: "
                            "is Curslink running on it, and is it awake? Trying %s anyway",
                            ", ".join(found.addresses), found.control_port, address)
            else:
                log.info("host found on the LAN at %s (it advertises %s)", address, ", ".join(found.addresses))
            # Remembered once a connection works (see run_agent), not here:
            # an address that only answered the probe mustn't become "last good".
            return ConnectTarget(address, found.control_port, ctx, auth_key, relay)
        if peer.address:
            # mDNS didn't reach us (some routers and mesh Wi-Fi drop it one
            # way): try where we paired with it or last found it.
            log.info("%s didn't answer on mDNS; trying its last address %s:%d", peer.name, peer.address, cfg.port)
            return ConnectTarget(peer.address, cfg.port, ctx, auth_key, relay)
    if relay:
        return ConnectTarget("", cfg.port, ctx, auth_key, relay)
    if peer is None:
        raise NotReady("set [agent].host, a [relay], or pair with the host so it can be found on the LAN")
    raise NotReady(f"looking for {peer.name} on the LAN...")


MIN_SAMPLES = 5    # fewer than this and a screen's centre isn't trusted
GOOD_SAMPLES = 15  # stop waiting once this many are in


class Calibrator:
    """Records head angles while the user looks at each screen in turn.

    Used by ``curslink calibrate`` (prompts on the terminal) and the desktop
    app's calibration page (buttons). Not thread safe: drive it from one thread.
    """

    def __init__(self, camera: int = 0, tracker_factory=None):
        if tracker_factory is None:
            from curslink.gaze.tracker import HeadPoseTracker as tracker_factory
        self._lock = threading.Lock()
        self._samples: list[tuple[float, float, float]] = []
        self.latest: tuple[float, float] | None = None
        self.calibration = Calibration()
        self.tracker = tracker_factory(self._on_sample, camera=camera)

    def _on_sample(self, t: float, yaw: float, pitch: float) -> None:
        with self._lock:
            self._samples.append((t, yaw, pitch))
            self.latest = (yaw, pitch)

    def start(self) -> None:
        log.info("calibration: starting camera")
        self.tracker.start()

    def stop(self) -> None:
        log.info("calibration: stopping camera")
        self.tracker.stop()

    def wait_for_face(self, timeout: float = 15.0) -> bool:
        deadline = time.monotonic() + timeout
        alive = getattr(self.tracker, "alive", None)
        while self.latest is None and time.monotonic() < deadline:
            if alive is not None and not alive():
                log.warning("calibration: the camera stopped before a face was found: %s",
                            getattr(self.tracker, "error", None))
                return False
            time.sleep(0.1)
        if self.latest is None:
            log.warning("calibration: no face within %.0fs", timeout)
        return self.latest is not None

    def record(self, target: Target, seconds: float = 1.5, settle: float = 0.2,
               patience: float = 3.0) -> tuple[float, float] | None:
        """Sample for ``seconds`` while the user looks at ``target``; returns
        its (yaw, pitch) centre, or None if too few samples came in.

        A screen well off to the side (another computer beside this one) has
        the head turned far enough that the camera only sees the face now and
        then, so if too few samples are in after ``seconds`` it keeps going
        for up to ``patience`` more before giving up."""
        start = time.monotonic()
        with self._lock:
            self._samples.clear()
        time.sleep(seconds)
        deadline = start + seconds + patience
        while True:
            with self._lock:
                samples = [(y, p) for t, y, p in self._samples if t >= start + settle]
            if len(samples) >= GOOD_SAMPLES or time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        if len(samples) < MIN_SAMPLES:
            log.warning("calibration: only %d samples for %s in %.1fs, not recorded", len(samples), target,
                        time.monotonic() - start)
            return None
        for yaw, pitch in samples:
            self.calibration.add(target, yaw, pitch)
        centre = self.calibration.centroids()[target]
        log.info("calibration: %s at yaw %+.1f, pitch %+.1f from %d samples", target, centre[0], centre[1],
                 len(samples))
        return centre


def close_pairs(cal: Calibration, margin_deg: float) -> list[tuple[Target, Target, float]]:
    """Screens whose centres are too close for gaze to tell apart reliably."""
    warn = 2 * margin_deg
    out = []
    for (a, pa), (b, pb) in itertools.combinations(cal.centroids().items(), 2):
        gap = math.hypot(pa[0] - pb[0], pa[1] - pb[1])
        if gap < warn:
            out.append((a, b, gap))
    return out


def calibrate(cfg: HostConfig, seconds: float = 1.5) -> Calibration:
    """Interactive: look at each screen, press Enter, hold still."""
    cal = Calibrator(cfg.gaze.camera)
    cal.start()
    try:
        print("Warming up the camera...")
        if not cal.wait_for_face():
            raise RuntimeError("no face detected; check the camera and lighting")
        for screen in cfg.layout.screens():
            device = cfg.layout.devices[screen.device_id]
            input(
                f"\nLook at the CENTRE of {device.name} / screen '{screen.id}' "
                "and press Enter (keep looking for a moment)..."
            )
            centre = cal.record(Target(screen.device_id, screen.id), seconds)
            if centre is None:
                print("  not enough samples (is your face visible?), skipping this screen")
                continue
            print(f"  recorded: yaw {centre[0]:+.1f}, pitch {centre[1]:+.1f}")
    finally:
        cal.stop()
    return cal.calibration
