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


LOCK_THEN_EXEC = """
import os, sys
sys.path.insert(0, {root!r})
import tempfile
tempfile.gettempdir = lambda: {tmp!r}
from PySide6.QtWidgets import QApplication
app = QApplication(["curslink-test"])
from curslink.gui.app import _take_instance_lock
lock = _take_instance_lock({name!r})
print("again" if len(sys.argv) > 1 else "first", lock is not None, flush=True)
if len(sys.argv) == 1:
    os.execv(sys.executable, [sys.executable, __file__, "again"])
"""


@pytest.mark.skipif(__import__("sys").platform == "win32", reason="no exec on Windows")
def test_exec_in_place_keeps_the_lock_so_updates_must_not_exec(qapp, name, tmp_path):
    """Why the app restarts after an update instead of exec'ing the new
    version: an exec keeps the pid, so the new image can't take the lock
    and quits, leaving nothing running (Curslink didn't reopen after updating)."""
    import subprocess
    import sys
    from pathlib import Path

    root = str(Path(__file__).resolve().parent.parent)
    script = tmp_path / "lock_then_exec.py"
    script.write_text(LOCK_THEN_EXEC.format(root=root, tmp=str(tmp_path), name=name))
    out = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=60,
                         env={**__import__("os").environ, "QT_QPA_PLATFORM": "offscreen"}).stdout
    assert out.split() == ["first", "True", "again", "False"]


@pytest.mark.skipif(__import__("sys").platform == "win32", reason="POSIX waiter")
def test_restart_waits_for_this_copy_to_exit(tmp_path):
    import subprocess
    import sys
    import time

    from curslink.gui.app import restart_command

    old = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.5)"])
    marker = tmp_path / "started"
    started = time.monotonic()
    waiter = subprocess.Popen(restart_command(["/bin/sh", "-c", f"touch {marker}"], old.pid))
    time.sleep(0.5)
    assert not marker.exists()  # the old copy is still running
    old.wait()
    waiter.wait(timeout=10)
    assert marker.exists() and time.monotonic() - started >= 1.4
