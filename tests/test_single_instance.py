"""Only one copy of the app runs: a second launch shows the first one's window."""

import pytest


@pytest.fixture
def qapp():
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:
        pytest.skip(f"Qt can't load: {exc}")
    return QApplication.instance() or QApplication(["curslink-test"])


@pytest.fixture
def name(tmp_path, monkeypatch):
    import tempfile

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    return f"curslink-test-{tmp_path.name}"


def test_a_second_copy_cant_take_the_lock(qapp, name):
    from curslink.gui.app import _take_instance_lock

    first = _take_instance_lock(name)
    assert first is not None
    assert _take_instance_lock(name) is None
    first.unlock()
    again = _take_instance_lock(name)
    assert again is not None
    again.unlock()


def test_a_lock_left_by_a_crashed_copy_is_taken(qapp, name, tmp_path):
    import subprocess
    import sys

    from curslink.gui.app import _take_instance_lock

    # Another copy takes the lock and dies without letting go of it.
    crash = (
        "import os, sys\n"
        "from PySide6.QtCore import QLockFile\n"
        "lock = QLockFile(sys.argv[1])\n"
        "assert lock.tryLock(0)\n"
        "os._exit(0)\n"
    )
    path = tmp_path / f"{name}.lock"
    subprocess.run([sys.executable, "-c", crash, str(path)], check=True, timeout=60)
    assert path.exists()
    lock = _take_instance_lock(name)
    assert lock is not None
    lock.unlock()


def test_a_second_launch_waits_for_the_first_to_listen_then_shows_it(qapp, name):
    import threading

    from PySide6.QtCore import QObject, QTimer

    from curslink.gui import app as gui_app

    shown = []

    class FakeApp(QObject):
        def show_window(self):
            shown.append(True)

    owner = FakeApp()
    # The first copy is still starting: it listens a moment after the second asks.
    QTimer.singleShot(600, lambda: gui_app._listen(name, owner))
    result = {}
    asker = threading.Thread(target=lambda: result.setdefault("ok", gui_app._show_running_copy(name, wait_s=5)))
    asker.start()
    import time

    deadline = time.monotonic() + 6
    while (asker.is_alive() or not shown) and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.02)
    asker.join(1)
    assert result.get("ok") is True
    assert shown


def test_nobody_answers_when_nothing_is_running(qapp, name):
    from curslink.gui.app import _show_running_copy

    assert _show_running_copy(name, wait_s=0.3) is False
