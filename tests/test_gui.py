"""The desktop app's window, drawn offscreen (skipped without Qt)."""

from __future__ import annotations

import json
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


def test_remote_look_here_shows_on_an_agent_without_buttons(desktop, qapp):
    from curslink.gui.overlay import find_qscreen

    assert find_qscreen("\\\\.\\DISPLAY9", [0, 0, 10, 10]) is not None  # falls back to a real screen
    desktop._on_look({"event": "look", "screen": "main", "step": "Step 2 of 3 · Kyles_PC", "host": "Mac",
                      "total": 3, "current": 1, "done": [0]})
    look = desktop._look
    assert len(look.layers) == len(qapp.screens())
    target = next(layer for layer in look.layers if layer.with_target)
    assert target.controls.isHidden() and "Space on Mac" in target.keys.text()
    desktop._on_look({"event": "look", "hide": True})
    assert look.layers == []


def test_calibrating_with_linking_up_asks_the_agent_for_the_card(desktop, qapp, monkeypatch):
    desktop.window.pages["setup"].create()
    cfg = desktop.config()
    cfg.add_device("laptop", "Work laptop")
    cfg.save()
    desktop.config_saved(restart=False)
    page = desktop.window.pages["calibrate"]
    page.load_targets()
    sent = []
    monkeypatch.setattr(desktop.service, "send", sent.append)
    monkeypatch.setattr(desktop.service, "restart", lambda: sent.append("restart"))
    page.kept_linking = True
    page.state = "ready"
    page.step = next(i for i, (t, _n) in enumerate(page.targets) if t.device_id == "laptop")
    page.show_state()
    assert page.overlay.layers == []  # not this computer's screen
    look = [s for s in sent if s.startswith("look ")]
    assert look and '"device":"laptop"' in look[-1] and '"screen":' in look[-1]
    page.cancel()
    qapp.processEvents()
    assert sent[-1] == 'look {"hide":true}' or 'look {"hide":true}' in sent
    assert not page.kept_linking


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


def test_a_config_without_devices_goes_back_to_setup(desktop):
    desktop.config_path.parent.mkdir(parents=True, exist_ok=True)
    desktop.config_path.write_text('[host]\neffect = "pop"\n')
    assert not desktop.has_config()
    desktop.config_path.write_text("[agent]\nanimations = false\n")
    assert not desktop.has_config()
    desktop.window.pages["setup"].create()
    assert desktop.has_config() and desktop.check_config() is None


def test_agent_camera_setting_and_host_status(desktop, qapp):
    """An agent can turn its own camera on; the host shows it's in use."""
    from curslink.configedit import ConfigFile, new_agent_config

    desktop.config_path.parent.mkdir(parents=True, exist_ok=True)
    desktop.config_path.write_text(new_agent_config("laptop", "Laptop"))
    desktop.config_saved(restart=False)
    page = desktop.window.pages["settings"]
    page.load()
    assert not page.agent_camera.isChecked()
    page.agent_camera.click()
    assert ConfigFile(desktop.config_path).get("agent.gaze", "enabled") is True
    page.set_agent_camera_device(2, "USB Camera")
    raw = ConfigFile(desktop.config_path).raw()
    assert raw["agent"]["gaze"] == {"enabled": True, "camera": 2, "camera_name": "USB Camera"}
    from curslink.config import agent_config
    assert agent_config(raw).gaze.camera_name == "USB Camera"

    s = desktop.status
    s.apply({"event": "cameras", "devices": ["laptop", "phone"]})
    assert s.cameras == ["laptop", "phone"]


def test_host_status_with_only_an_agents_camera(desktop, qapp):
    desktop.window.pages["setup"].create()
    me = desktop.config().get("host", "device_id")
    cfg = desktop.config()
    cfg.add_device("laptop", "Work laptop")
    cfg.save()
    desktop.config_saved(restart=False)
    s = desktop.status
    s.apply({"event": "running", "role": "host", "device_id": me, "name": "x", "active": me, "screen": "main",
             "gaze": False})
    s.apply({"event": "agent", "device_id": "laptop", "name": "Work laptop", "connected": True, "screens": []})
    s.apply({"event": "cameras", "devices": ["laptop"]})
    desktop.service.state = "running"
    desktop.service_changed.emit("running")
    page = desktop.window.pages["status"]
    page.refresh()
    assert page.gaze_toggle.isChecked()
    assert "Work laptop's camera" in page.gaze_text.detail_label.text()
    sent = []
    desktop.service.send = sent.append
    page.gaze_clicked(False)
    assert sent == ["gaze pause"]
def test_switch_animation_never_takes_focus_or_clicks(qapp):
    from PySide6.QtCore import Qt

    from curslink.overlay import make_effect_widget

    w = make_effect_widget("pop", 50, 50)
    flags = w.windowFlags()
    assert flags & Qt.WindowType.WindowDoesNotAcceptFocus
    assert flags & Qt.WindowType.WindowTransparentForInput
    assert w.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    assert w.testAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)  # else hidden on the Mac
    assert not w.isActiveWindow()
    w.close()


def test_settings_turn_monitor_jumps_on_and_off(desktop, qapp):
    from curslink.config import host_config

    desktop.window.pages["setup"].create()
    cfg = desktop.config()
    cfg.add_device("laptop", "Work laptop")
    cfg.save()
    desktop.config_saved(restart=False)
    page = desktop.window.pages["settings"]
    page.load()
    assert "laptop/main" in page.jump_toggles and not any(t.isChecked() for t in page.jump_toggles.values())
    page.jump_toggles["laptop/main"].click()
    assert desktop.config().get("gaze", "jump_screens") == ["laptop/main"]
    assert page.jump_toggles["laptop/main"].isChecked()
    page.jump_all_on.click()
    everything = list(page.jump_toggles)
    assert desktop.config().get("gaze", "jump_screens") == everything
    assert len(host_config(desktop.config().raw()).router.gaze_jump_screens) == len(everything)
    page.jump_all_off.click()
    assert desktop.config().get("gaze", "jump_screens") == []


def test_a_screen_the_camera_couldnt_see_is_tried_again_not_skipped(desktop, qapp):
    desktop.window.pages["setup"].create()
    cfg = desktop.config()
    cfg.add_device("laptop", "Work laptop")
    cfg.save()
    desktop.config_saved(restart=False)
    page = desktop.window.pages["calibrate"]
    page.load_targets()
    page.state, page.step = "recording", 0
    name = page.targets[0][1]
    page.recorded(name, None)
    assert page.state == "ready" and page.step == 0 and name not in page.results
    assert "couldn't see your face" in page.prompt_detail.text()
    page.state = "recording"
    page.recorded(name, (1.0, 2.0))
    assert page.step == 1 and page.results[name] == (1.0, 2.0)
    assert "couldn't see" not in page.prompt_detail.text()
def test_menu_bar_item_with_no_height_counts_as_placed(desktop, monkeypatch):
    # macOS 26 reports the placed menu bar item as 38 x 0; closing the
    # window must still hide Curslink to the menu bar.
    import sys

    from PySide6.QtCore import QRect

    tray = desktop.tray
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(tray, "available", lambda: True)
    monkeypatch.setattr(tray, "isVisible", lambda: True)
    monkeypatch.setattr(tray, "geometry", lambda: QRect(0, 982, 38, 0))
    assert desktop.tray_placed()
    monkeypatch.setattr(tray, "geometry", lambda: QRect())
    assert not desktop.tray_placed()


def _host_with_laptop(desktop):
    desktop.window.pages["setup"].create()
    cfg = desktop.config()
    cfg.add_device("laptop", "Work laptop")
    cfg.save()
    desktop.config_saved(restart=False)
    page = desktop.window.pages["layout"]
    page.load()
    laptop = next(i for i in page.items if i.device_id == "laptop")
    home = next(i for i in page.items if i.device_id != "laptop")
    return page, home, laptop


def test_layout_page_wont_save_overlapping_screens(desktop, qapp):
    page, home, laptop = _host_with_laptop(desktop)
    hx, hy, hw, hh = home.layout_rect()
    page.scene.clearSelection()
    laptop.setSelected(True)
    page.spins["x"].setValue(hx + hw - 200)
    page.spins["y"].setValue(hy + hh - 200)
    assert page.dirty and page.blocking_overlap()
    assert not page.save_button.isEnabled()
    assert "overlaps" in page.overlap_note.text() and not page.overlap_row.isHidden()
    page.save()  # refused even if called directly
    saved = next(d for d in desktop.config().devices() if d["id"] == "laptop")["screens"][0]
    assert saved["x"] == hx + hw  # still where add_device put it
    # dropping it there pushes it back out to touch instead
    from curslink.gui.pages.layout import push_apart

    others = [home.layout_rect()]
    x, y = push_apart(laptop.layout_rect(), others)
    from curslink.gui.pages.layout import overlap_of

    assert overlap_of((x, y, *laptop.layout_rect()[2:]), others[0]) is None


def test_layout_page_saves_overlap_when_allowed(desktop, qapp):
    from curslink.config import host_config

    page, home, laptop = _host_with_laptop(desktop)
    hx, hy, hw, hh = home.layout_rect()
    page.scene.clearSelection()
    laptop.setSelected(True)
    page.spins["x"].setValue(hx + hw - 200)
    page.spins["y"].setValue(hy + hh - 200)
    page.overlap_toggle.setChecked(True)
    page.bring_to_front(laptop)
    page.overlaps_changed()
    assert page.save_button.isEnabled()
    assert "in front of" in page.overlap_note.text()
    page.save()
    raw = desktop.config().raw()
    assert raw["layout"]["allow_overlap"] is True
    layout = host_config(raw).layout
    (behind, front), = layout.overlaps()
    assert front.device_id == "laptop"
    # turning it off again with the screens still overlapping can't be saved
    page.overlap_toggle.setChecked(False)
    assert not page.save_button.isEnabled()


def test_overlap_shows_on_the_real_monitor_while_editing(desktop, qapp):
    from curslink.gui.overlaphint import OverlapHint, regions_for
    from PySide6.QtGui import QGuiApplication

    page, home, laptop = _host_with_laptop(desktop)
    # As if the Layout page were on screen, without showing the main window
    # (on macOS that reaches for a native window the offscreen platform lacks).
    page.isVisible = lambda: True
    sent = []
    desktop.service.send = sent.append
    hx, hy, hw, hh = home.layout_rect()
    page.scene.clearSelection()
    laptop.setSelected(True)
    page.overlap_toggle.setChecked(True)
    page.spins["x"].setValue(hx + hw - 200)
    page.spins["y"].setValue(hy + hh - 100)
    page.bring_to_front(laptop)
    page.overlaps_changed()
    me = desktop.config().get("host", "device_id")
    payload = page.hint_payload()
    (entry,) = payload[me]["screens"]
    assert entry["regions"][0]["rect"] == [hw - 200, hh - 100, 200, 100]
    assert "Work laptop stands here" == entry["regions"][0]["label"]
    assert page.hint is not None and page.hint.showing  # on this computer's monitor
    # saving finishes the edit: the marks go
    page.save()
    assert not page.hint.showing
    # the area scales from layout pixels to the monitor's own size
    q = QGuiApplication.primaryScreen()
    g = q.geometry()
    (rect, _label, bad), = regions_for(q, {"size": [g.width() * 2, g.height() * 2],
                                           "regions": [{"rect": [100, 50, 200, 100], "bad": True}]})
    assert (rect.x(), rect.y(), rect.width(), rect.height()) == (50, 25, 100, 50) and bad
    hint = OverlapHint()
    hint.show({"screens": [{"screen": "main", "size": [g.width(), g.height()],
                            "regions": [{"rect": [0, 0, 10, 10]}]}]})
    assert hint.showing
    hint.show({"hide": True})
    assert not hint.showing


def test_no_widget_opens_as_a_stray_window(desktop, qapp):
    # A widget shown before it has a parent opens as a window of its own:
    # on Windows that flashed up and vanished, on macOS it left rows piled up.
    from PySide6.QtCore import QEvent, QObject
    from PySide6.QtWidgets import QWidget

    shown = []

    class Watch(QObject):
        def eventFilter(self, obj, event):  # noqa: N802
            if event.type() == QEvent.Type.Show and isinstance(obj, QWidget) and obj.isWindow():
                shown.append(type(obj).__name__)
            return False

    watch = Watch()
    qapp.installEventFilter(watch)
    try:
        from curslink.gui.widgets import Card, label, text_block

        card = Card("Rows", rows=True)
        card.add_row(text_block("Title", "Detail"), label("x"))
        plain = Card("Plain")
        plain.body.addWidget(text_block("Title", "Detail"))
        w = desktop.window
        w.pages["setup"].create()
        for page in ("status", "devices", "layout", "calibrate", "settings"):
            w.show_page(page)
            qapp.processEvents()
        assert shown == []
    finally:
        qapp.removeEventFilter(watch)


def _widest(widget, depth=0):
    # what's asking for the width, for the failure message
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QWidget

    out = []
    for child in widget.findChildren(QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly):
        want = child.minimumSizeHint().width()
        if child.isVisibleTo(widget) and want > 250 and depth < 8:
            text = child.text()[:40] if hasattr(child, "text") else ""
            out.append(f"{'  ' * depth}{type(child).__name__} {want} {text!r}")
            out.extend(_widest(child, depth + 1))
    return "\n".join(out) if depth == 0 else out


def test_layout_page_fits_the_window_with_an_overlap(desktop, qapp, monkeypatch):
    # Side by side, the bottom cards (four number fields and the Edges card,
    # whose "in front of" line didn't wrap) wanted ~800 px, so in a smaller
    # Mac window the page ran off its right edge. Narrow, they now stack.
    from curslink.gui import theme

    monkeypatch.setenv("CURSLINK_LOOK", "mac")
    theme.manager().set_mode("dark")
    try:
        page, home, laptop = _host_with_laptop(desktop)
        hx, hy, hw, hh = home.layout_rect()
        page.scene.clearSelection()
        laptop.setSelected(True)
        page.spins["x"].setValue(hx + hw - 200)
        page.spins["y"].setValue(hy + hh - 200)
        page.overlap_toggle.setChecked(True)
        page.bring_to_front(laptop)
        page.overlaps_changed()
        page.refresh_edges()
        w = desktop.window
        w.show_page("layout")
        # Windows' offscreen fonts come out about twice as wide as anywhere
        # else, so scale the window with them ("Detect my screens" is ~120 px).
        scale = max(1.0, page.detect_button.sizeHint().width() / 120)

        def check(width, part=None):
            w.resize(round(width), 760)
            w.show()
            qapp.processEvents()
            qapp.processEvents()
            m = page.layout_.contentsMargins()
            need = (part.minimumSizeHint().width() + m.left() + m.right() if part
                    else page.widget().minimumSizeHint().width())
            assert need <= page.viewport().width(), (width, page.bottom_row.direction(), _widest(page.widget()))
            stacked = page.bottom_row.direction() == page.bottom_row.Direction.TopToBottom
            assert stacked == (page.viewport().width() < page.side_by_side_width()), width
            return stacked

        check(900 * scale)  # Kyle's window
        assert not check(1400 * scale)
        # just too narrow for the two cards side by side, when the window can be
        just_under = w.width() - page.viewport().width() + page.side_by_side_width() - 20
        if just_under >= w.minimumWidth():
            assert check(just_under, part=page.inspector.parentWidget())  # the header may not fit there
    finally:
        theme.manager().set_mode("system")


def test_clicking_a_screen_doesnt_move_it(desktop, qapp):
    from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
    from PySide6.QtWidgets import QGraphicsSceneMouseEvent

    page, home, laptop = _host_with_laptop(desktop)
    hx, hy, hw, hh = home.layout_rect()
    page.overlap_toggle.setChecked(True)
    page.scene.clearSelection()
    laptop.setSelected(True)
    # near another screen's edge, where a drop would snap to it, and over the monitor's corner
    page.spins["x"].setValue(hx + hw - 237)
    page.spins["y"].setValue(hy + hh - 113)
    page.save()
    page.load()
    laptop = next(i for i in page.items if i.device_id == "laptop")
    home = next(i for i in page.items if i.device_id != "laptop")
    home.setZValue(5)  # the monitor is in front for now
    before = laptop.layout_rect()

    def event(kind, screen_pos):
        e = QGraphicsSceneMouseEvent(kind)
        e.setButton(Qt.MouseButton.LeftButton)
        e.setButtons(Qt.MouseButton.LeftButton if kind == QEvent.Type.GraphicsSceneMousePress
                     else Qt.MouseButton.NoButton)
        e.setScreenPos(screen_pos)
        e.setButtonDownScreenPos(Qt.MouseButton.LeftButton, QPoint(100, 100))
        e.setScenePos(QPointF(laptop.scenePos()))
        e.setButtonDownScenePos(Qt.MouseButton.LeftButton, QPointF(laptop.scenePos()))
        return e

    laptop.mousePressEvent(event(QEvent.Type.GraphicsSceneMousePress, QPoint(100, 100)))
    laptop.setPos(laptop.pos() + QPointF(0.3, 0.2))  # a hand shakes a pixel
    laptop.mouseReleaseEvent(event(QEvent.Type.GraphicsSceneMouseRelease, QPoint(101, 100)))
    assert laptop.layout_rect() == before  # no snapping, no jump
    assert page.in_front(laptop, home)  # the click brought it forward


def test_dragged_screen_stays_where_it_is_let_go(desktop, qapp):
    from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
    from PySide6.QtWidgets import QGraphicsSceneMouseEvent

    from curslink.gui.pages.layout import SCALE

    page, home, laptop = _host_with_laptop(desktop)
    page.overlap_toggle.setChecked(True)
    hx, hy, hw, hh = home.layout_rect()
    # A fixed zoom (a layout pixel is a quarter of an on-screen pixel), not
    # whatever the platform's offscreen view size gives.
    page.view.resetTransform()
    page.view.scale(2.5, 2.5)
    zoom = page.view.transform().m11()
    near = max(1, int(2 / (zoom * SCALE)))  # 2 on-screen px from the monitor's right edge
    laptop.dragging = lambda: True  # as while the mouse holds it
    laptop.setPos((hx + hw + near) * SCALE, (hy + 333) * SCALE)
    assert laptop.layout_rect()[0] == hx + hw  # snapped live, while dragging
    # 12 on-screen px from the edge: too far to snap while dragging (but the
    # old snap on release, 80 layout px, would have pulled it over)
    away = int(12 / (zoom * SCALE))
    assert 0 < away < 80, away
    laptop.setPos((hx + hw - away) * SCALE, (hy + 333) * SCALE)
    shown = laptop.layout_rect()
    laptop.dragging = lambda: False

    e = QGraphicsSceneMouseEvent(QEvent.Type.GraphicsSceneMouseRelease)
    e.setButton(Qt.MouseButton.LeftButton)
    e.setScreenPos(QPoint(300, 100))
    e.setButtonDownScreenPos(Qt.MouseButton.LeftButton, QPoint(100, 100))
    e.setScenePos(QPointF(laptop.scenePos()))
    laptop.mouseReleaseEvent(e)
    assert laptop.layout_rect() == shown  # no jump on release


def test_calibrating_an_edge_on_both_computers(desktop, qapp):
    from PySide6.QtWidgets import QPushButton

    from curslink.gui import service as svc
    from curslink.gui.edgecalib import _scale

    page, home, laptop = _host_with_laptop(desktop)
    sent = []
    desktop.service.send = sent.append
    # Not linking: it says so instead.
    page.calibrate_edge(home.key, "right", laptop.key)
    assert page.calibration is None and "Start linking" in page.edge_note.text()
    desktop.service.state = svc.RUNNING
    buttons = [b.text() for b in page.edges_card.findChildren(QPushButton)]
    assert "Calibrate edge…" in buttons
    page.calibrate_edge(home.key, "right", laptop.key)
    cal = page.calibration
    assert cal is not None and sent[0] == "pick on"
    # This computer's screen first: a clickable layer; the laptop waits its turn.
    window = cal.windows[0]
    assert window.clickable and window.state.side == "right"
    (entry,) = json.loads(sent[-1][5:])["devices"]["laptop"]["screens"]
    assert entry["edge"]["side"] == "left" and "first" in entry["edge"]["waiting"]
    k = _scale(window.screen(), cal.entry(0), "right")
    window.clicked.emit(600 * k)
    window.clicked.emit(200 * k)
    # a click somewhere else on the laptop's screen doesn't count; one on it does
    cal.on_event({"event": "picked", "device": "elsewhere", "screen": "main", "x": 5, "y": 7})
    (entry,) = json.loads(sent[-1][5:])["devices"]["laptop"]["screens"]
    assert "waiting" not in entry["edge"] and entry["edge"]["points"] == []
    screen_id = laptop.screen["id"]
    cal.on_event({"event": "picked", "device": "laptop", "screen": screen_id, "x": 3, "y": 900})
    cal.on_event({"event": "picked", "key": "backspace"})
    cal.on_event({"event": "picked", "device": "laptop", "screen": screen_id, "x": 3, "y": 800})
    cal.on_event({"event": "picked", "device": "laptop", "screen": screen_id, "x": 3, "y": 0})
    assert page.calibration is None and "pick off" in sent
    (link,) = desktop.config().links()
    assert link["a"] == home.key and link["a_side"] == "right" and link["a_span"] == [200, 600]
    assert link["b"] == laptop.key and link["b_side"] == "left" and link["b_span"] == [0, 800]
    buttons = [b.text() for b in page.edges_card.findChildren(QPushButton)]
    assert "Calibrate again…" in buttons and "Reset" in buttons
    from curslink.config import host_config

    assert len(host_config(desktop.config().raw()).layout.links) == 1
    page.reset_edge(home.key, laptop.key)
    assert desktop.config().links() == []


def test_esc_cancels_edge_calibration(desktop, qapp):
    from curslink.gui import service as svc

    page, home, laptop = _host_with_laptop(desktop)
    sent = []
    desktop.service.send = sent.append
    desktop.service.state = svc.RUNNING
    page.calibrate_edge(home.key, "right", laptop.key)
    page.calibration.windows[0].key.emit("esc")
    assert page.calibration is None and desktop.config().links() == []
    assert sent[-1] == 'hint {"devices":{}}'


def test_paired_computer_draws_the_edge_layer(qapp):
    from PySide6.QtGui import QGuiApplication

    from curslink.gui.overlaphint import OverlapHint

    g = QGuiApplication.primaryScreen().geometry()
    hint = OverlapHint()
    hint.show({"screens": [{"screen": "main", "size": [g.width() * 2, g.height() * 2],
                            "edge": {"side": "left", "other": "Kyle's Mac", "points": [100]}}]})
    (window,) = hint.edges.windows.values()
    assert not window.clickable and window.state.points == [50] and not hint.windows
    hint.show({"hide": True})
    assert not hint.showing
