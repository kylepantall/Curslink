"""Wiring the pieces together into the runnable host and agent."""

from __future__ import annotations

import asyncio
import itertools
import logging
import math
import threading
import time
from dataclasses import replace

from curslink import events
from curslink.config import AgentConfig, HostConfig, PairingConfig
from curslink.gaze.confirm import FacingDetector, GazeArbiter
from curslink.gaze.zones import Calibration, GazeClassifier, Target
from curslink.net.client import AgentClient, AgentSession, NotReady
from curslink.net.client import Target as ConnectTarget
from curslink.net.relay import HostRelayLink, RelayTarget, paired_room_id
from curslink.net.server import HostServer
from curslink.pairing.handshake import DeviceInfo
from curslink.pairing.store import PairingStore
from curslink.router import Router, SwitchEvent
from curslink.security import HelloVerifier, client_context, server_context

log = logging.getLogger(__name__)


class LoopBridge:
    """Hands input callbacks from pynput's threads to the asyncio loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop, router: Router):
        self._loop = loop
        self._router = router

    def on_pointer(self, x, y):
        self._loop.call_soon_threadsafe(self._router.on_pointer, x, y)

    def on_motion(self, dx, dy):
        self._loop.call_soon_threadsafe(self._router.on_motion, dx, dy)

    def on_button(self, button, pressed):
        self._loop.call_soon_threadsafe(self._router.on_button, button, pressed)

    def on_scroll(self, dx, dy):
        self._loop.call_soon_threadsafe(self._router.on_scroll, dx, dy)

    def on_key(self, key, pressed):
        self._loop.call_soon_threadsafe(self._router.on_key, key, pressed)


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
    router.on_gaze_paused = lambda paused: events.emit("gaze", paused=paused)
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
    capture.start(LoopBridge(loop, router))

    tracker = None
    if cfg.gaze.enabled:
        tracker = start_gaze(cfg, router, loop, server)

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
    if events.channel.enabled:
        events.read_commands(loop, lambda command: host_command(router, stop, command))
    try:
        await stop.wait()
    finally:
        if tracker:
            tracker.stop()
        capture.stop()
        overlay.close()
        await pairing.close()
        if relay:
            await relay.close()
        await server.close()


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


def start_gaze(cfg: HostConfig, router: Router, loop: asyncio.AbstractEventLoop, server: HostServer | None = None):
    from curslink.gaze.cameras import resolve
    from curslink.gaze.tracker import HeadPoseTracker

    if not cfg.gaze.calibration.exists():
        log.error(
            "gaze is enabled but %s does not exist; run `curslink calibrate` first",
            cfg.gaze.calibration,
        )
        return None
    arbiter = make_arbiter(cfg, router)
    if server is not None:
        wire_agent_cameras(server, arbiter)

    tracker = HeadPoseTracker(
        lambda t, yaw, pitch: loop.call_soon_threadsafe(arbiter.on_sample, t, yaw, pitch),
        camera=resolve(cfg.gaze.camera, cfg.gaze.camera_name),
        preview=cfg.gaze.preview,
    )
    tracker.start()
    return tracker


def make_arbiter(cfg: HostConfig, router: Router) -> GazeArbiter:
    """The host's gaze classifier, checked against agents' own cameras."""
    classifier = GazeClassifier(Calibration.load(cfg.gaze.calibration), cfg.gaze.settings)
    arbiter = GazeArbiter(
        classifier,
        cfg.device_id,
        switch=router.on_gaze,
        current=lambda: Target(router.active_device, router.screen.id),
        screens_of=lambda device_id: [s.id for s in cfg.layout.screens(device_id)],
        settings=cfg.gaze.confirm,
    )

    def on_switch(event: SwitchEvent) -> None:
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

    def on_connect(conn) -> None:
        arbiter.set_camera(conn.device_id, conn.camera)
        if conn.camera:
            log.info("%s will confirm gaze switches with its own camera", conn.device_id)
        connected(conn)

    def on_disconnect(device_id: str) -> None:
        arbiter.forget(device_id)
        disconnected(device_id)

    def on_facing(device_id: str, facing: bool) -> None:
        arbiter.on_facing(time.monotonic(), device_id, facing)
        events.emit("facing", device_id=device_id, facing=facing)

    server.on_connect, server.on_disconnect, server.on_facing = on_connect, on_disconnect, on_facing
    for conn in server.connections.values():  # connected before gaze started
        arbiter.set_camera(conn.device_id, conn.camera)


async def run_agent(cfg: AgentConfig) -> None:
    from curslink.inputs.pynput_backend import PynputSink
    from curslink.overlay import OverlayProcess

    overlay = OverlayProcess() if cfg.animations else None
    if overlay:
        overlay.start()
    session = AgentSession(
        PynputSink(cfg.modifier_map),
        overlay.show if overlay else (lambda effect, x, y: None),
    )
    store = PairingStore(cfg.pairing.store)
    client = AgentClient(
        cfg.device_id, cfg.name, cfg.host, cfg.port, None, session,
        resolve=lambda: resolve_host(cfg, store),
    )
    client.on_state = lambda state, detail: events.emit("host", state=state, detail=detail)
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
        await pairing.close()
        if overlay:
            overlay.close()


class FacingCamera:
    """Agent side: this device's own camera, watching for the user facing it."""

    TICK_S = 0.1

    def __init__(self, detector: FacingDetector, client: AgentClient, tracker, loop: asyncio.AbstractEventLoop):
        self.detector = detector
        self.client = client
        self.tracker = tracker
        self.loop = loop
        self._ticker: asyncio.Task | None = None

    def on_sample(self, t: float, yaw: float, pitch: float) -> None:  # on the loop
        self._report(self.detector.update(t, yaw, pitch))

    def _report(self, changed: bool | None) -> None:
        if changed is None:
            return
        log.debug("camera: you are %sfacing this device", "" if changed else "not ")
        events.emit("facing", facing=changed)
        self.client.report_facing(changed)

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(self.TICK_S)
            self._report(self.detector.tick(time.monotonic()))

    def start(self) -> None:
        self.tracker.start()
        self._ticker = self.loop.create_task(self._tick())

    def stop(self) -> None:
        if self._ticker:
            self._ticker.cancel()
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
    camera = FacingCamera(detector, client, tracker, loop)
    client.facing_source = lambda: detector.facing
    camera.start()
    log.info("using camera %d to confirm when you're facing this device", cfg.gaze.camera)
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
            return ConnectTarget(found.address, found.control_port, ctx, auth_key, relay)
    if relay:
        return ConnectTarget("", cfg.port, ctx, auth_key, relay)
    if peer is None:
        raise NotReady("set [agent].host, a [relay], or pair with the host so it can be found on the LAN")
    raise NotReady(f"looking for {peer.name} on the LAN...")


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

    def record(self, target: Target, seconds: float = 1.5, settle: float = 0.2) -> tuple[float, float] | None:
        """Sample for ``seconds`` while the user looks at ``target``; returns
        its (yaw, pitch) centre, or None if too few samples came in."""
        start = time.monotonic()
        with self._lock:
            self._samples.clear()
        time.sleep(seconds)
        with self._lock:
            samples = [(y, p) for t, y, p in self._samples if t >= start + settle]
        if len(samples) < 5:
            log.warning("calibration: only %d samples for %s, skipped", len(samples), target)
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
