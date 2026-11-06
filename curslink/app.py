"""Wiring the pieces together into the runnable host and agent."""

from __future__ import annotations

import asyncio
import logging
import time

from curslink.config import AgentConfig, HostConfig
from curslink.gaze.zones import Calibration, GazeClassifier, Target
from curslink.net.client import AgentClient, AgentSession
from curslink.net.relay import HostRelayLink
from curslink.net.server import HostServer
from curslink.router import Router, SwitchEvent
from curslink.security import client_context, server_context

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


async def run_host(cfg: HostConfig) -> None:
    from curslink.inputs.pynput_backend import PynputCapture, PynputSink
    from curslink.overlay import OverlayProcess

    loop = asyncio.get_running_loop()
    remote_ids = {d for d in cfg.layout.devices if d != cfg.device_id}
    tls = server_context(cfg.secret)
    server = HostServer(
        remote_ids,
        tls,
        host=cfg.listen,
        port=cfg.port,
    )
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
    if cfg.lan:
        await server.start()
    relay = None
    if cfg.relay:
        relay = HostRelayLink(cfg.relay, tls, server.handle_streams)
        relay.start()
        log.info("waiting for agents via relay %s", cfg.relay.address)
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
        if relay:
            await relay.close()
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
    client = AgentClient(
        cfg.device_id, cfg.name, cfg.host, cfg.port, client_context(cfg.secret), session, relay=cfg.relay
    )
    try:
        await client.run_forever()
    finally:
        if overlay:
            overlay.close()


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
