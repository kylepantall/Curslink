"""Wiring the pieces together into the runnable host and agent."""

from __future__ import annotations

import asyncio
import logging
import time

from curslink.config import AgentConfig, HostConfig, PairingConfig
from curslink.gaze.zones import Calibration, GazeClassifier, Target
from curslink.net.client import AgentClient, AgentSession, NotReady
from curslink.net.client import Target as ConnectTarget
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


async def run_host(cfg: HostConfig) -> None:
    from curslink.inputs.pynput_backend import PynputCapture, PynputSink
    from curslink.overlay import OverlayProcess

    loop = asyncio.get_running_loop()
    remote_ids = {d for d in cfg.layout.devices if d != cfg.device_id}
    store = PairingStore(cfg.pairing.store)
    server = HostServer(
        remote_ids,
        server_context(cfg.secret, store.key),
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
    server.on_disconnect = router.on_disconnect
    await server.start()
    pairing = Pairing(cfg.pairing, DeviceInfo(cfg.device_id, cfg.name, "host"), store, server.port)
    await pairing.start()
    capture.start(LoopBridge(loop, router))

    tracker = None
    if cfg.gaze.enabled:
        tracker = start_gaze(cfg, router, loop)

    log.info(
        "Curslink host running as %r. Return hotkey: %s, gaze toggle: %s",
        cfg.device_id,
        cfg.router.return_hotkey,
        cfg.router.gaze_toggle_hotkey,
    )
    try:
        await asyncio.Event().wait()
    finally:
        if tracker:
            tracker.stop()
        capture.stop()
        overlay.close()
        await pairing.close()
        await server.close()


def start_gaze(cfg: HostConfig, router: Router, loop: asyncio.AbstractEventLoop):
    from curslink.gaze.tracker import HeadPoseTracker

    if not cfg.gaze.calibration.exists():
        log.error(
            "gaze is enabled but %s does not exist; run `curslink calibrate` first",
            cfg.gaze.calibration,
        )
        return None
    classifier = GazeClassifier(Calibration.load(cfg.gaze.calibration), cfg.gaze.settings)

    def on_switch(event: SwitchEvent) -> None:
        classifier.force(Target(event.to_device, event.screen.id))

    router.listeners.append(on_switch)
    classifier.force(Target(router.active_device, router.screen.id))

    def handle(t: float, yaw: float, pitch: float) -> None:
        target = classifier.update(t, yaw, pitch)
        if target is not None:
            router.on_gaze(target)

    tracker = HeadPoseTracker(
        lambda t, yaw, pitch: loop.call_soon_threadsafe(handle, t, yaw, pitch),
        camera=cfg.gaze.camera,
        preview=cfg.gaze.preview,
    )
    tracker.start()
    return tracker


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
    pairing = Pairing(cfg.pairing, DeviceInfo(cfg.device_id, cfg.name, "agent"), store)
    await pairing.start()
    try:
        await client.run_forever()
    finally:
        await pairing.close()
        if overlay:
            overlay.close()


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
    if peer is not None:
        ctx, auth_key = client_context(device_id=cfg.device_id, key=peer.key), peer.key
    elif cfg.secret:
        ctx, auth_key = client_context(cfg.secret), None
    else:
        raise NotReady("waiting to be paired: run `curslink pair` on the host or here")
    if cfg.host:
        return ConnectTarget(cfg.host, cfg.port, ctx, auth_key)
    if peer is None:
        raise NotReady("set [agent].host, or pair with the host so it can be found on the LAN")
    from curslink.pairing.discovery import find

    found = await find(peer.device_id, timeout=2.0)
    if found is None or not found.control_port:
        raise NotReady(f"looking for {peer.name} on the LAN...")
    return ConnectTarget(found.address, found.control_port, ctx, auth_key)


def calibrate(cfg: HostConfig, seconds: float = 1.5) -> Calibration:
    """Interactive: look at each screen, press Enter, hold still."""
    from curslink.gaze.tracker import HeadPoseTracker

    latest: list[tuple[float, float, float]] = []
    tracker = HeadPoseTracker(lambda t, y, p: latest.append((t, y, p)), camera=cfg.gaze.camera)
    tracker.start()
    cal = Calibration()
    try:
        print("Warming up the camera...")
        deadline = time.monotonic() + 15
        while not latest and time.monotonic() < deadline:
            time.sleep(0.1)
        if not latest:
            raise RuntimeError("no face detected; check the camera and lighting")
        for screen in cfg.layout.screens():
            device = cfg.layout.devices[screen.device_id]
            input(
                f"\nLook at the CENTRE of {device.name} / screen '{screen.id}' "
                "and press Enter (keep looking for a moment)..."
            )
            start = time.monotonic()
            latest.clear()
            time.sleep(seconds)
            samples = [(y, p) for t, y, p in latest if t >= start + 0.2]
            if len(samples) < 5:
                print("  not enough samples (is your face visible?), skipping this screen")
                continue
            for yaw, pitch in samples:
                cal.add(Target(screen.device_id, screen.id), yaw, pitch)
            cy, cp = cal.centroids()[Target(screen.device_id, screen.id)]
            print(f"  recorded {len(samples)} samples: yaw {cy:+.1f}, pitch {cp:+.1f}")
    finally:
        tracker.stop()
    return cal
