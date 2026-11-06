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
