"""Two-camera gaze confirmation: agent facing detector, host arbiter, wire."""

import asyncio

import pytest

from conftest import FakeSink
from curslink.config import ConfigError, agent_config, host_config
from curslink.gaze.confirm import ConfirmSettings, FacingDetector, FacingSettings, GazeArbiter
from curslink.gaze.zones import Calibration, GazeClassifier, GazeSettings, Target
from curslink.net.client import AgentClient, AgentSession
from curslink.net.server import HostServer
from curslink.protocol import Message, ProtocolError, decode, encode
from curslink.security import client_context, server_context

# Host "desktop" with one monitor straight ahead; laptop "left" is calibrated
# on the host, laptop "right" isn't (only its own camera knows about it).
MONITOR = Target("desktop", "main")
LEFT = Target("left", "main")
RIGHT = Target("right", "main")


# ------------------------------------------------------------ FacingDetector
def feed_facing(det, yaw, pitch, start, seconds, fps=30):
    changes = []
    for i in range(int(seconds * fps)):
        r = det.update(start + i / fps, yaw, pitch)
        if r is not None:
            changes.append(r)
    return changes


def test_facing_needs_dwell_and_has_hysteresis():
    det = FacingDetector(FacingSettings(facing_deg=15, hysteresis_deg=5, dwell_s=0.1))
    assert feed_facing(det, 40, 0, 0.0, 0.5) == []          # turned away
    assert feed_facing(det, 3, -4, 1.0, 0.05) == []         # a glance is too short
    assert feed_facing(det, 3, -4, 2.0, 0.5) == [True]
    assert feed_facing(det, 18, 0, 3.0, 0.5) == []          # inside the hysteresis band
    assert feed_facing(det, 25, 0, 4.0, 0.5) == [False]


def test_facing_respects_camera_centre():
    det = FacingDetector(FacingSettings(facing_deg=10, center_pitch_deg=-15, dwell_s=0))
    assert det.update(0.0, 0, 0) is None    # 15 degrees above "straight on"
    assert det.update(0.1, 0, -15) is True


def test_face_lost_means_not_facing():
    det = FacingDetector(FacingSettings(dwell_s=0, lost_after_s=0.4))
    assert det.update(0.0, 0, 0) is True
    assert det.tick(0.2) is None
    assert det.tick(0.5) is False
    assert det.tick(0.6) is None


# --------------------------------------------------------------- GazeArbiter
class FakeRouter:
    def __init__(self):
        self.current = MONITOR
        self.switches = []

    def switch(self, target):
        self.switches.append(target)
        self.current = target


def make_arbiter(dwell_s=0.0, **confirm):
    cal = Calibration()
    for target, (yaw, pitch) in {MONITOR: (0, 0), LEFT: (-45, 0)}.items():
        cal.add(target, yaw, pitch)
    clf = GazeClassifier(cal, GazeSettings(dwell_s=dwell_s, max_distance_deg=12))
    router = FakeRouter()
    clf.force(router.current)
    screens = {"desktop": ["main"], "left": ["main"], "right": ["main"]}
    arb = GazeArbiter(clf, "desktop", router.switch, lambda: router.current,
                      lambda d: screens.get(d, []), ConfirmSettings(**confirm))
    return arb, router


def feed(arb, yaw, pitch, start, seconds, fps=30):
    for i in range(int(seconds * fps)):
        arb.on_sample(start + i / fps, yaw, pitch)


def test_agent_without_camera_switches_on_host_gaze_alone():
    arb, router = make_arbiter()
    feed(arb, -44, 0, 0.0, 0.2)
    assert router.switches == [LEFT]


def test_agent_with_camera_must_confirm():
    arb, router = make_arbiter()
    arb.set_camera("left", True)
    blocked = []
    arb.on_blocked = blocked.append
    feed(arb, -44, 0, 0.0, 0.2)
    assert router.switches == [] and blocked
    # the laptop's camera now sees you: the host still sees you looking at it
    arb.on_facing(0.25, "left", True)
    assert router.switches == [LEFT]


def test_stale_or_negative_report_does_not_confirm():
    arb, router = make_arbiter(report_fresh_s=3.0)
    arb.set_camera("left", True)
    arb.on_facing(0.0, "left", False)
    feed(arb, -44, 0, 0.0, 0.2)
    assert router.switches == []
    arb.reports["left"].facing = True
    feed(arb, -44, 0, 5.0, 0.2)  # report is 5 s old
    assert router.switches == []
    arb.reports["left"].at = 5.0
    feed(arb, -44, 0, 5.3, 0.2)
    assert router.switches == [LEFT]


def test_agent_facing_switches_when_host_sees_head_turned_away():
    arb, router = make_arbiter()
    arb.set_camera("right", True)
    feed(arb, 50, 0, 0.0, 0.3)  # far from the monitor; "right" isn't calibrated
    assert router.switches == []
    arb.on_facing(0.3, "right", True)
    assert router.switches == [RIGHT]


def test_agent_facing_ignored_while_host_sees_you_at_its_screen():
    arb, router = make_arbiter()
    arb.set_camera("right", True)
    feed(arb, 2, 1, 0.0, 0.3)  # looking at the monitor; the laptop's camera sees you too
    arb.on_facing(0.3, "right", True)
    assert router.switches == []


def test_host_face_lost_counts_as_turned_away_but_not_dead_camera():
    arb, router = make_arbiter(host_fresh_s=0.5, host_alive_s=10)
    arb.set_camera("right", True)
    feed(arb, 2, 1, 0.0, 0.3)
    arb.on_facing(3.0, "right", True)  # host camera lost your face 2.7 s ago
    assert router.switches == [RIGHT]

    arb, router = make_arbiter(host_alive_s=10)
    arb.set_camera("right", True)
    feed(arb, 2, 1, 0.0, 0.3)
    arb.on_facing(60.0, "right", True)  # host camera silent for a minute
    assert router.switches == []

    arb, router = make_arbiter()
    arb.set_camera("right", True)
    arb.on_facing(1.0, "right", True)  # host camera never saw anyone
    assert router.switches == []


def test_confirmation_can_be_turned_off():
    arb, router = make_arbiter(enabled=False)
    arb.set_camera("left", True)
    feed(arb, -44, 0, 0.0, 0.2)
    assert router.switches == [LEFT]
    router.current = MONITOR
    arb.classifier.force(MONITOR)
    feed(arb, 50, 0, 1.0, 0.2)
    arb.on_facing(1.2, "right", True)  # unregistered and disabled: ignored
    assert router.switches == [LEFT]


def test_forget_drops_camera_and_reports():
    arb, router = make_arbiter()
    arb.set_camera("left", True)
    arb.on_facing(0.0, "left", True)
    arb.forget("left")
    assert "left" not in arb.cameras and "left" not in arb.reports


# ------------------------------------------------------------- protocol/config
def test_facing_frame_validated():
    assert decode(encode("facing", facing=True)) == Message("facing", {"facing": True})
    with pytest.raises(ProtocolError):
        decode(b'{"t":"facing","facing":"yes"}\n')


LAYOUT = """
[[devices]]
id = "desktop"
  [[devices.screens]]
  id = "main"
  x = 0
  y = 0
  width = 1920
  height = 1080
"""


def test_agent_gaze_config():
    import tomllib

    raw = tomllib.loads("""
[agent]
device_id = "laptop"
[agent.gaze]
enabled = true
camera = 1
facing_deg = 20
center_pitch_deg = -10
dwell_ms = 200
""")
    cfg = agent_config(raw)
    assert cfg.gaze.enabled and cfg.gaze.camera == 1
    assert cfg.gaze.settings.facing_deg == 20
    assert cfg.gaze.settings.center_pitch_deg == -10
    assert cfg.gaze.settings.dwell_s == pytest.approx(0.2)
    assert not agent_config(tomllib.loads('[agent]\ndevice_id = "x"')).gaze.enabled
    with pytest.raises(ConfigError):
        agent_config(tomllib.loads('[agent]\ndevice_id = "x"\n[agent.gaze]\nfacing_deg = 0'))


def test_host_confirm_config():
    import tomllib

    base = '[host]\ndevice_id = "desktop"\n' + LAYOUT
    assert host_config(tomllib.loads(base)).gaze.confirm.enabled
    off = tomllib.loads('[gaze]\nconfirm_with_agents = false\n' + base)
    assert not host_config(off).gaze.confirm.enabled


# ------------------------------------------------------------------ the wire
SECRET = "correct horse battery staple"


async def wait_for(cond, timeout=3.0):
    for _ in range(int(timeout / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met")


async def test_agent_reports_facing_to_host():
    reports = []
    server = HostServer({"laptop"}, server_context(SECRET), host="127.0.0.1", port=0)
    server.on_facing = lambda device_id, facing: reports.append((device_id, facing))
    await server.start()
    session = AgentSession(FakeSink(), lambda e, x, y: None)
    client = AgentClient("laptop", "Laptop", "127.0.0.1", server.port, client_context(SECRET), session)
    facing = [False]
    client.facing_source = lambda: facing[0]
    task = asyncio.create_task(client.run_once())
    try:
        await wait_for(lambda: server.is_connected("laptop"))
        assert server.connections["laptop"].camera
        await wait_for(lambda: reports)  # the first refresh goes out on connect
        assert reports[0] == ("laptop", False)
        facing[0] = True
        client.report_facing(True)
        await wait_for(lambda: ("laptop", True) in reports)
        assert server.connections["laptop"].facing is True
    finally:
        await server.close()
        await asyncio.wait_for(task, 3)


async def test_agent_without_camera_says_so():
    server = HostServer({"laptop"}, server_context(SECRET), host="127.0.0.1", port=0)
    await server.start()
    session = AgentSession(FakeSink(), lambda e, x, y: None)
    client = AgentClient("laptop", "Laptop", "127.0.0.1", server.port, client_context(SECRET), session)
    task = asyncio.create_task(client.run_once())
    try:
        await wait_for(lambda: server.is_connected("laptop"))
        assert not server.connections["laptop"].camera
        client.report_facing(True)  # no camera configured, but harmless
    finally:
        await server.close()
        await asyncio.wait_for(task, 3)


# --------------------------------------------- wired into the real router
def test_app_wiring_with_router(layout, tmp_path):
    from types import SimpleNamespace

    from conftest import FakeCapture, FakeOverlay, FakeTransport
    from curslink.app import make_arbiter as app_arbiter, wire_agent_cameras
    from curslink.config import GazeConfig, HostConfig
    from curslink.router import Router, RouterSettings

    cal = Calibration()
    cal.add(Target("desktop", "left"), -10, 0)
    cal.add(Target("desktop", "right"), 10, 0)
    cal.save(tmp_path / "cal.json")
    cfg = HostConfig("desktop", None, layout,
                     gaze=GazeConfig(enabled=True, calibration=tmp_path / "cal.json",
                                     settings=GazeSettings(dwell_s=0.0)))
    transport = FakeTransport(connected={"laptop"})
    router = Router(layout, "desktop", FakeCapture(), FakeSink(), transport, FakeOverlay(),
                    RouterSettings(gaze_switching=True))
    arbiter = app_arbiter(cfg, router)
    connected, gone = [], []
    server = SimpleNamespace(on_connect=connected.append, on_disconnect=gone.append,
                             on_facing=None, connections={})
    wire_agent_cameras(server, arbiter)
    server.on_connect(SimpleNamespace(device_id="laptop", camera=True))
    assert connected and "laptop" in arbiter.cameras

    for i in range(10):  # head turned well to the right, past both monitors
        arbiter.on_sample(100 + i / 30, 60, 0)
    assert router.active_device == "desktop"
    arbiter.on_facing(100.4, "laptop", True)
    assert router.active_device == "laptop" and router.screen.id == "main"

    server.on_disconnect("laptop")
    assert gone == ["laptop"] and "laptop" not in arbiter.cameras
