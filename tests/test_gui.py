"""The desktop app's window, drawn offscreen (skipped without Qt)."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture
def qapp():
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:  # e.g. no libEGL on a bare Linux box
        pytest.skip(f"Qt can't load: {exc}")
    return QApplication.instance() or QApplication(["curslink-test"])


@pytest.fixture
def desktop(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("CURSLINK_HOME", str(tmp_path))
    from curslink.gui.app import Curslink
    from curslink.gui.prefs import Prefs
    from curslink.gui.tray import Tray
    from curslink.gui.window import MainWindow

    app = Curslink(Prefs(start_linking=False))
    app.window = MainWindow(app)
    app.tray = Tray(app)
    yield app
    app.window.close()
    app.window.deleteLater()


def test_first_run_sets_up_a_host(desktop, qapp):
    w = desktop.window
    assert not desktop.has_config()
    assert w.stack.currentWidget() is w.pages["setup"]
    w.pages["setup"].create()
    qapp.processEvents()
    assert desktop.role == "host"
    assert desktop.check_config() is None
    for page in ("status", "devices", "layout", "calibrate", "settings"):
        w.show_page(page)
        qapp.processEvents()
        assert w.stack.currentWidget() is w.pages[page]


def test_events_update_the_status_and_tray(desktop, qapp):
    desktop.window.pages["setup"].create()
    me = desktop.config().get("host", "device_id")
    cfg = desktop.config()
    cfg.add_device("laptop", "Work laptop")
    cfg.save()
    desktop.config_saved(restart=False)
    s = desktop.status
    s.apply({"event": "running", "role": "host", "device_id": me, "name": "x", "active": me, "screen": "main",
             "gaze": True})
    s.apply({"event": "agent", "device_id": "laptop", "name": "Work laptop", "connected": True, "screens": []})
    s.apply({"event": "switch", "from": me, "to": "laptop", "screen": "main", "reason": "gaze"})
    s.apply({"event": "gaze", "paused": True})
    desktop.service.state = "running"
    desktop.service_changed.emit("running")
    qapp.processEvents()
    assert s.active == "laptop" and s.gaze_paused and s.agents["laptop"].connected
    assert "Work laptop" in desktop.tray.detail.text()
    assert desktop.tray.gaze.text().lower().startswith("resume")


def test_layout_page_saves_moves(desktop, qapp):
    desktop.window.pages["setup"].create()
    cfg = desktop.config()
    cfg.add_device("laptop", "Work laptop")
    cfg.save()
    desktop.config_saved(restart=False)
    page = desktop.window.pages["layout"]
    page.load()
    laptop = next(i for i in page.items if i.device_id == "laptop")
    page.scene.clearSelection()
    laptop.setSelected(True)
    page.spins["y"].setValue(250)
    assert page.dirty
    page.save()
    assert next(d for d in desktop.config().devices() if d["id"] == "laptop")["screens"][0]["y"] == 250


def test_theme_modes_and_looks(desktop, qapp, monkeypatch):
    from curslink.gui import theme

    for look in ("win", "mac", "linux"):
        monkeypatch.setenv("CURSLINK_LOOK", look)
        for mode in ("light", "dark"):
            theme.manager().set_mode(mode)
            th = theme.current()
            assert th.platform == look and th.dark == (mode == "dark")
            assert "QPushButton#primary" in theme.stylesheet(th)
    theme.manager().set_mode("system")


def test_camera_picker_keeps_the_name(qapp):
    from curslink.gaze.cameras import Camera
    from curslink.gui.camerapicker import CameraPicker

    picker = CameraPicker()
    picked = []
    picker.picked.connect(lambda i, n: picked.append((i, n)))
    picker.set_current(0, "Logitech C920")
    picker._fill([Camera(0, "FaceTime HD Camera", "Built in"), Camera(1, "Logitech C920", "USB")])
    assert picker.current() == (1, "Logitech C920")  # found by name though the index moved
    assert picked == []  # loading isn't a choice
    picker.setCurrentIndex(0)
    assert picked == [(0, "FaceTime HD Camera")]


def test_calibrate_draws_the_layout_without_angles(desktop, qapp):
    desktop.window.pages["setup"].create()
    cfg = desktop.config()
    cfg.add_device("laptop", "Work laptop")
    cfg.save()
    desktop.config_saved(restart=False)
    page = desktop.window.pages["calibrate"]
    desktop.window.show_page("calibrate")
    qapp.processEvents()
    assert len(page.map.screens) == len(page.targets) >= 2
    page.state, page.step = "ready", 1
    page.results = {page.targets[0][1]: (3.0, -1.0)}
    page.show_state()
    first, second = page.targets[0][0].key, page.targets[1][0].key
    assert page.map.states[first] == {"done"}
    assert "target" in page.map.states[second]
    page.cancel()
    assert page.overlay.layers == []


def test_look_here_overlay_on_this_computer(qapp):
    from curslink.gui.overlay import LookHere, qscreen_for

    assert qscreen_for("main") is not None
    assert qscreen_for("screen99") is None
    hits = []
    lh = LookHere(lambda: hits.append("record"), lambda: hits.append("cancel"))
    assert lh.show("main", "Step 1 of 2")
    assert len(lh.layers) == len(qapp.screens())
    lh.layers[0].record.emit()
    lh.hide()
    assert hits == ["record"] and lh.layers == []


def test_a_paired_agent_starts_hidden(desktop, qapp):
    setup = desktop.window.pages["setup"]
    setup.agent_choice.setChecked(True)
    setup.create()
    assert desktop.role == "agent"
    assert not desktop.agent_paired()
    store = desktop.window.pages["devices"].store()
    store.add("desk", "Desk PC", "host", b"k" * 32)
    assert desktop.agent_paired()


def test_nothing_paired_means_setup_isnt_done(desktop, qapp):
    desktop.window.pages["setup"].create()
    assert desktop.role == "host" and not desktop.has_pairings()
    desktop.window.pages["devices"].store().add("laptop", "Work laptop", "agent", b"k" * 32)
    assert desktop.has_pairings()
    assert not desktop.agent_paired()  # a host isn't an agent


def test_macos_permissions_card(desktop, qapp, monkeypatch):
    from curslink import macperms

    desktop.window.pages["setup"].create()
    card = desktop.window.pages["status"].permissions
    have = {"accessibility": False, "input-monitoring": False, "camera": False, "local-network": True}
    monkeypatch.setattr(macperms, "MAC", True)
    monkeypatch.setattr(macperms, "granted", lambda k: have[k])
    asked = []
    monkeypatch.setattr(macperms, "request", lambda k: asked.append(k))
    monkeypatch.setattr(macperms, "missing_fresh", macperms.missing)
    card.apply(macperms.missing("host"))
    assert not card.isHidden() and card.rows.count() == 4  # all three, plus "Restart Curslink"
    assert card.restart_row.isHidden()
    card.allow("accessibility")
    assert asked == ["accessibility"] and not card.restart_row.isHidden()
    have["accessibility"] = have["input-monitoring"] = True
    card.apply(macperms.missing("host"))
    assert card.rows.count() == 2  # camera, plus "Restart Curslink"
    have["camera"] = True
    card.apply(macperms.missing("host"))
    assert card.isHidden()
    have["local-network"] = False
    card.apply(macperms.missing("host"))
    assert not card.isHidden() and card.rows.count() == 2  # local network, plus "Restart Curslink"
    assert macperms.needed("agent") == ["accessibility", "local-network"]
    assert macperms.needed("host") == ["accessibility", "input-monitoring", "camera", "local-network"]


def test_live_looking_lights_the_calibrate_map(desktop, qapp):
    desktop.window.pages["setup"].create()
    page = desktop.window.pages["calibrate"]
    page.load_targets()
    key = page.targets[0][0].key
    st = desktop.status
    st.apply({"event": "running", "role": "host", "device_id": "me", "gaze": True})
    assert page.idle_readout() == "Camera on · no face in view"
    st.apply({"event": "looking", "screen": key})
    assert st.looking() == key
    page.live_tick()
    assert page.looking == key and "looking" in page.map.states[key]
    assert page.readout.text().startswith("Camera on · you're looking at")
    st.looking_seen -= 10  # the host stopped saying (no face)
    page.live_tick()
    assert page.looking is None


def test_devices_page_shows_this_computer(desktop, qapp):
    from curslink.pairing.discovery import local_hostname

    desktop.window.pages["setup"].create()
    page = desktop.window.pages["devices"]
    page.refresh()
    texts = " ".join(w.text() for w in page.this.findChildren(type(page.search_state)))
    assert local_hostname() in texts
