"""Two-camera gaze confirmation: agent facing detector, host arbiter, wire."""

import asyncio
import time

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


def test_host_face_lost_counts_as_turned_away():
    arb, router = make_arbiter(host_fresh_s=0.5, host_alive_s=10)
    arb.set_camera("right", True)
    feed(arb, 2, 1, 0.0, 0.3)
    arb.on_facing(3.0, "right", True)  # host camera lost your face 2.7 s ago
    assert router.switches == [RIGHT]


def test_dead_or_silent_host_camera_leaves_it_to_the_agents():
    arb, router = make_arbiter(host_alive_s=10)
    arb.set_camera("right", True)
    feed(arb, 2, 1, 0.0, 0.3)
    arb.on_facing(60.0, "right", True)  # host camera silent for a minute
    assert router.switches == [RIGHT]

    arb, router = make_arbiter()
    arb.set_camera("right", True)
    arb.on_facing(1.0, "right", True)  # host camera never saw anyone
    assert router.switches == [RIGHT]


# ------------------------------------------- facing only: no calibrated host
def facing_only(local_camera=False, **confirm):
    router = FakeRouter()
    screens = {"desktop": ["main"], "left": ["main"], "right": ["main"]}
    arb = GazeArbiter(None, "desktop", router.switch, lambda: router.current,
                      lambda d: screens.get(d, []), ConfirmSettings(**confirm))
    arb.set_camera("left", True)
    arb.set_camera("right", True)
    if local_camera:
        arb.set_camera("desktop", True)
    return arb, router


def test_agent_camera_alone_switches_there_and_back():
    arb, router = facing_only()
    arb.on_facing(0.0, "left", True)
    assert router.switches == [LEFT]
    arb.on_facing(0.5, "left", True)  # the once-a-second refresh: nothing new
    assert router.switches == [LEFT]
    arb.on_facing(2.0, "left", False)  # turned back to the host, which has no camera
    assert router.switches == [LEFT, MONITOR]
    arb.on_facing(2.5, "right", False)  # not on it: nothing to do
    assert router.switches == [LEFT, MONITOR]


def test_turning_away_goes_to_another_device_that_sees_you():
    arb, router = facing_only()
    arb.on_facing(0.0, "left", True)
    arb.on_facing(0.1, "right", True)
    assert router.switches == [LEFT, RIGHT]
    arb.on_facing(0.2, "right", False)
    assert router.switches == [LEFT, RIGHT, LEFT]  # "left" still sees you
    arb.on_facing(5.0, "left", False)
    assert router.switches[-1] == MONITOR  # "right"'s report is stale by now


def test_host_camera_uncalibrated_reports_like_an_agent():
    arb, router = facing_only(local_camera=True)
    arb.on_facing(0.0, "left", True)
    arb.on_facing(1.0, "left", False)  # the host's camera has a say: nobody sees you
    assert router.switches == [LEFT]
    arb.on_facing(1.2, "desktop", True)
    assert router.switches == [LEFT, MONITOR]
    arb.on_facing(2.0, "left", True)
    assert router.switches == [LEFT, MONITOR, LEFT]
    arb.on_facing(2.5, "desktop", True)  # both still see you: refreshes don't ping-pong
    arb.on_facing(3.0, "left", True)
    assert router.switches == [LEFT, MONITOR, LEFT]


def test_refresh_does_not_undo_bring_input_home():
    arb, router = facing_only()
    arb.on_facing(0.0, "left", True)
    router.current = MONITOR  # the return hotkey
    arb.on_facing(1.0, "left", True)
    assert router.switches == [LEFT]
    arb.on_facing(2.0, "left", False)
    arb.on_facing(3.0, "left", True)  # looked away and back
    assert router.switches == [LEFT, LEFT]


def test_facing_only_respects_confirm_off_and_suspend():
    arb, router = facing_only(enabled=False)
    arb.on_facing(0.0, "left", True)
    assert router.switches == []
    arb, router = facing_only()
    arb.suspended = True
    arb.on_facing(0.0, "left", True)
    assert router.switches == []
    arb.suspended = False
    arb.on_facing(0.1, "left", False)
    arb.on_facing(0.2, "left", True)
    assert router.switches == [LEFT]


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
camera_name = "FaceTime HD Camera"
facing_deg = 20
center_pitch_deg = -10
dwell_ms = 200
""")
    cfg = agent_config(raw)
    assert cfg.gaze.enabled and cfg.gaze.camera == 1 and cfg.gaze.camera_name == "FaceTime HD Camera"
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
    facing = host_config(tomllib.loads('[gaze]\ndwell_ms = 500\nfacing_deg = 25\nfacing_dwell_ms = 100\n' + base))
    assert facing.gaze.settings.dwell_s == pytest.approx(0.5)  # the calibrated classifier's
    assert facing.gaze.facing.facing_deg == 25 and facing.gaze.facing.dwell_s == pytest.approx(0.1)


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


def test_app_agent_camera_alone_drives_router(layout):
    """Host gaze off (no camera, no calibration): the laptop's camera alone
    moves the keyboard and mouse there and back."""
    from types import SimpleNamespace

    from conftest import FakeCapture, FakeOverlay, FakeTransport
    from curslink.app import make_arbiter as app_arbiter, wire_agent_cameras
    from curslink.config import HostConfig
    from curslink.router import Router, RouterSettings

    cfg = HostConfig("desktop", None, layout)  # [gaze] off
    router = Router(layout, "desktop", FakeCapture(), FakeSink(), FakeTransport(connected={"laptop"}),
                    FakeOverlay(), RouterSettings(gaze_switching=True))
    arbiter = app_arbiter(cfg, router)
    assert arbiter.classifier is None
    server = SimpleNamespace(on_connect=lambda c: None, on_disconnect=lambda d: None,
                             on_facing=None, connections={})
    wire_agent_cameras(server, arbiter)
    server.on_connect(SimpleNamespace(device_id="laptop", camera=True))

    server.on_facing("laptop", True)
    assert router.active_device == "laptop"
    server.on_facing("laptop", False)
    assert router.active_device == "desktop"
    router.set_gaze_paused(True)
    server.on_facing("laptop", True)
    assert router.active_device == "desktop"


async def test_facing_camera_refreshes_and_reports_changes():
    from curslink.app import FacingCamera

    class Tracker:
        def start(self): pass
        def stop(self): pass

    reports = []
    det = FacingDetector(FacingSettings(dwell_s=0, lost_after_s=10))
    cam = FacingCamera(det, reports.append, Tracker(), asyncio.get_running_loop(), refresh_s=0.15)
    cam.TICK_S = 0.05
    cam.start()
    try:
        cam.on_sample(time.monotonic(), 0, 0)
        assert reports == [True]
        await asyncio.sleep(0.4)
        assert reports.count(True) >= 2  # refreshed while nothing changed
    finally:
        cam.stop()



def test_crossing_an_edge_by_hand_isnt_undone_by_gaze(tmp_path, layout):
    """Looking at the desktop and pushing the mouse over the edge to the
    laptop: gaze used to "see" you look back at the desktop 250 ms later and
    pull the pointer back, over and over."""
    from conftest import FakeCapture, FakeOverlay, FakeTransport
    from curslink.app import make_arbiter as app_arbiter
    from curslink.config import GazeConfig, HostConfig
    from curslink.router import Router, RouterSettings

    cal = Calibration()
    cal.add(Target("desktop", "left"), -10, 0)
    cal.add(Target("desktop", "right"), 10, 0)
    cal.add(Target("laptop", "main"), 40, 0)
    cal.save(tmp_path / "cal.json")
    cfg = HostConfig("desktop", None, layout,
                     gaze=GazeConfig(enabled=True, calibration=tmp_path / "cal.json",
                                     settings=GazeSettings(dwell_s=0.0)))
    router = Router(layout, "desktop", FakeCapture(), FakeSink(), FakeTransport(connected={"laptop"}),
                    FakeOverlay(), RouterSettings(gaze_switching=True))
    now = [100.0]
    router.clock = lambda: now[0]
    arbiter = app_arbiter(cfg, router)
    router.on_pointer(2500, 500)
    now[0] += 1
    for i in range(5):
        arbiter.on_sample(now[0] + i / 30, 10, 0)   # looking at the right monitor
    router.on_pointer(3839, 500)                    # and pushing over the edge
    assert router.active_device == "laptop"
    now[0] += 1
    for i in range(30):                             # still looking at the right monitor
        arbiter.on_sample(now[0] + i / 30, 10, 0)
    assert router.active_device == "laptop"
    for i in range(10):                             # look at the laptop, then back
        arbiter.on_sample(now[0] + 1 + i / 30, 40, 0)
    for i in range(10):
        arbiter.on_sample(now[0] + 2 + i / 30, -10, 0)
    assert router.is_local                           # a real look back still switches



def test_a_camera_that_never_saw_you_doesnt_send_input_home(layout):
    """Kyle's PC camera failed to open, so the PC kept reporting "not facing"
    every second; each one sent input home the moment the mouse stopped."""
    from types import SimpleNamespace

    from conftest import FakeCapture, FakeOverlay, FakeTransport
    from curslink.app import make_arbiter as app_arbiter, wire_agent_cameras
    from curslink.config import HostConfig
    from curslink.router import Router, RouterSettings

    cfg = HostConfig("desktop", None, layout)  # no host camera: facing only
    router = Router(layout, "desktop", FakeCapture(), FakeSink(), FakeTransport(connected={"laptop"}),
                    FakeOverlay(), RouterSettings(gaze_switching=True))
    arbiter = app_arbiter(cfg, router)
    server = SimpleNamespace(on_connect=lambda c: None, on_disconnect=lambda d: None,
                             on_facing=None, connections={})
    wire_agent_cameras(server, arbiter)
    server.on_connect(SimpleNamespace(device_id="laptop", camera=True))
    router.on_pointer(3839, 500)                 # over the edge by hand
    assert router.active_device == "laptop"
    router.clock = lambda: 1e9                    # the mouse has been still a while
    for _ in range(3):
        server.on_facing("laptop", False)         # the once-a-second refresh
    assert router.active_device == "laptop"
    server.on_facing("laptop", True)
    server.on_facing("laptop", False)             # a real turn away still goes home
    assert router.is_local


# ------------------------------------------- every camera from its own angle
# Kyle's desk (diagnostics, 2026-11-16): the PC's camera has a bright window
# behind him and loses his face once he turns to the Mac at the side; the
# Mac's camera sees him the whole time, from its own angle.
PC = Target("desktop", "main")
MAC = Target("mac", "main")
WIDE = Target("mac", "screen2")


def joint_calibration():
    from curslink.gaze.zones import Calibration

    cal = Calibration()
    for _ in range(5):
        cal.add(PC, -4, -1)
        cal.add(WIDE, -28, -9)          # the PC's camera never saw the look at the Mac's own screen
        cal.add(PC, 38, -3, camera="mac")
        cal.add(WIDE, 22, 6, camera="mac")
        cal.add(MAC, 2, -5, camera="mac")
    return cal


def joint_arbiter():
    from curslink.gaze.zones import JointClassifier

    router = FakeRouter()
    router.current = PC
    clf = JointClassifier(joint_calibration(), GazeSettings(dwell_s=0.2), local="desktop")
    clf.force(PC)
    arbiter = GazeArbiter(clf, "desktop", router.switch, lambda: router.current, lambda d: ["main"])
    arbiter.set_camera("mac", True)
    return router, clf, arbiter


def test_calibration_keeps_each_cameras_view(tmp_path):
    from curslink.gaze.zones import Calibration

    path = tmp_path / "calibration.json"
    joint_calibration().save(path)
    cal = Calibration.load(path)
    assert cal.centroids()[PC] == (-4, -1) and MAC not in cal.centroids()
    assert cal.centroids("mac") == {PC: (38, -3), WIDE: (22, 6), MAC: (2, -5)}


def test_a_devices_camera_alone_sees_the_look_the_hosts_camera_lost():
    router, clf, arbiter = joint_arbiter()
    for i in range(10):  # looking at the PC: both cameras see it, each at its own angle
        arbiter.on_sample(i / 20, -4, -1)
        arbiter.on_pose(i / 20, "mac", 38, -3)
    assert router.switches == []
    for i in range(20):  # turned to the Mac: the PC's camera has lost the face
        arbiter.on_pose(2 + i / 20, "mac", 2, -5)
    assert router.switches == [MAC]


def test_cameras_that_disagree_dont_switch():
    router, clf, arbiter = joint_arbiter()
    for i in range(20):  # the Mac's camera says the wide screen, the PC's still says the PC
        arbiter.on_sample(i / 20, -4, -1)
        arbiter.on_pose(i / 20, "mac", 22, 6)
    assert router.switches == []
    for i in range(20):  # both say the wide screen
        arbiter.on_sample(2 + i / 20, -28, -9)
        arbiter.on_pose(2 + i / 20, "mac", 22, 6)
    assert router.switches == [WIDE]


def test_a_calibrated_camera_isnt_also_asked_whether_you_face_it():
    router, clf, arbiter = joint_arbiter()
    arbiter.on_sample(0.0, -4, -1)
    arbiter.on_pose(0.0, "mac", 38, -3)
    # at its angle the Mac's camera never sees you "facing" it while you look at its wide screen;
    # and a "facing" from it mustn't move the pointer on its own either
    arbiter.on_facing(0.1, "mac", True)
    assert router.switches == []
    assert arbiter.confirms("mac", 0.1)
    # an uncalibrated camera's angles are left alone
    arbiter.set_camera("right", True)
    arbiter.on_pose(0.2, "right", 0, 0)
    assert router.switches == []


def test_pose_frames_are_checked():
    assert decode(encode("pose", yaw=12.5, pitch=-3).rstrip(b"\n")).data == {"yaw": 12.5, "pitch": -3}
    for bad in ({"yaw": "x", "pitch": 0}, {"yaw": 1}, {"yaw": True, "pitch": 0}, {"yaw": 1e9, "pitch": 0}):
        with pytest.raises(ProtocolError):
            decode(encode("pose", **bad).rstrip(b"\n"))


def test_an_agent_sends_its_head_angles_only_to_a_host_that_reads_them():
    class Transport:
        def is_closing(self):
            return False

    class Writer:
        transport = Transport()

        def __init__(self):
            self.frames = []

        def write(self, data):
            self.frames.append(decode(data.rstrip(b"\n")))

    client = AgentClient.__new__(AgentClient)
    client._writer, client._pose_sent, client.host_features = Writer(), 0.0, frozenset()
    client.report_pose(10.0, 1.234, -5.678)
    assert client._writer.frames == []  # an older host
    client.host_features = frozenset({"pose"})
    for i in range(30):  # a second of a 30 fps camera
        client.report_pose(10.0 + i / 30, 1.234, -5.678)
    assert 8 <= len(client._writer.frames) <= 13
    assert client._writer.frames[0].t == "pose" and client._writer.frames[0].data == {"yaw": 1.23, "pitch": -5.68}
