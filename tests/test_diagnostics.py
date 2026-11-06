"""Logs, crash reports, and handing work back to the Qt thread."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import zipfile

import pytest


def run_py(code: str, tmp_path, **env) -> subprocess.CompletedProcess:
    full_env = {**os.environ, "CURSLINK_LOG_DIR": str(tmp_path), "QT_QPA_PLATFORM": "offscreen", **env}
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)], env=full_env,
                          capture_output=True, text=True, timeout=60)


def test_logs_go_to_a_file(tmp_path):
    r = run_py("""
        import logging
        from curslink import diagnostics
        diagnostics.setup("cli")
        logging.getLogger("x").info("hello from the test")
    """, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "hello from the test" in (tmp_path / "cli.log").read_text()


def test_unhandled_errors_on_threads_write_a_report(tmp_path):
    r = run_py("""
        import threading
        from curslink import diagnostics
        diagnostics.setup("cli")
        def boom():
            raise ValueError("kaboom")
        t = threading.Thread(target=boom, name="worker"); t.start(); t.join()
    """, tmp_path)
    assert r.returncode == 0, r.stderr
    reports = list(tmp_path.glob("crash-*-cli-error.txt"))
    assert len(reports) == 1
    text = reports[0].read_text()
    assert "kaboom" in text and "thread worker" in text and "== Environment ==" in text


def test_a_native_crash_is_reported_on_the_next_start(tmp_path):
    r = run_py("""
        import ctypes
        from curslink import diagnostics
        diagnostics.setup("host")
        ctypes.string_at(0)  # segfault
    """, tmp_path)
    assert r.returncode != 0
    assert (tmp_path / "fault-host.log").read_text().strip()
    r = run_py("""
        from curslink import diagnostics
        diagnostics.setup("host")
        print(diagnostics.last_native_crash())
    """, tmp_path)
    assert r.returncode == 0, r.stderr
    reports = list(tmp_path.glob("crash-*-host-native.txt"))
    assert len(reports) == 1
    assert "Segmentation fault" in reports[0].read_text()
    assert str(reports[0]) in r.stdout
    assert not (tmp_path / "fault-host.log").read_text().strip()  # handled once


def test_the_bundle_has_the_logs(tmp_path):
    from curslink import diagnostics

    (tmp_path / "gui.log").write_text("a line\n")
    (tmp_path / "pairings.json").write_text("{secret}")
    os.environ["CURSLINK_LOG_DIR"] = str(tmp_path)
    try:
        out = diagnostics.write_bundle(tmp_path / "bundle.zip")
    finally:
        del os.environ["CURSLINK_LOG_DIR"]
    names = zipfile.ZipFile(out).namelist()
    assert "gui.log" in names and "environment.txt" in names
    assert "pairings.json" not in names


def test_background_results_always_reach_the_qt_thread(tmp_path):
    """Regression: v0.3.0 segfaulted when many background tasks finished
    (each made a QObject that its worker thread then freed)."""
    pytest.importorskip("PySide6.QtWidgets")
    r = run_py("""
        import time
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
        from curslink.gui.common import in_background
        app = QApplication([])
        got, errs = [], []
        for i in range(300):
            in_background(lambda i=i: (time.sleep(0.002), i)[1], got.append)
            in_background(lambda: 1 / 0, None, errs.append)
        def check():
            if len(got) == 300 and len(errs) == 300:
                app.quit()
        t = QTimer(); t.timeout.connect(check); t.start(20)
        QTimer.singleShot(15000, app.quit)
        app.exec()
        print(len(got), len(errs), sorted(got) == list(range(300)))
    """, tmp_path)
    if "libEGL" in r.stderr or "could not load the Qt platform plugin" in r.stderr:
        pytest.skip("Qt can't load here")
    assert r.returncode == 0, r.stderr[-2000:]
    assert r.stdout.split() == ["300", "300", "True"]
