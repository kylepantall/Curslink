"""Logs, crash reports, and handing work back to the Qt thread."""

from __future__ import annotations

import os
import subprocess
import sys
import json
import logging
import textwrap

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
    text = reports[0].read_text()
    # faulthandler words it per platform: "Segmentation fault" or "access violation" (Windows).
    assert "Segmentation fault" in text or "access violation" in text
    assert str(reports[0]) in r.stdout
    assert not (tmp_path / "fault-host.log").read_text().strip()  # handled once


def test_the_bundle_is_one_json_lines_file(tmp_path):
    from curslink import diagnostics

    (tmp_path / "gui.jsonl").write_text(
        json.dumps({"type": "log", "ts": "2026-11-14T10:00:02.000Z", "proc": "gui", "lvl": "info", "msg": "b"}) + "\n"
        + "not json\n")
    (tmp_path / "host.jsonl.1").write_text(
        json.dumps({"type": "log", "ts": "2026-11-14T10:00:01.000Z", "proc": "host", "lvl": "warning", "msg": "a"})
        + "\n")
    (tmp_path / "gui.log").write_text("covered by gui.jsonl\n")
    (tmp_path / "winhelper.log").write_text(
        "2026-11-14 10:00:03,250 INFO    curslink.winhelper: started\nTraceback line\n")
    (tmp_path / "crash-20261114-100000-host-error.txt").write_text("boom")
    (tmp_path / "pairings.json").write_text("{secret}")
    os.environ["CURSLINK_LOG_DIR"] = str(tmp_path)
    try:
        out = diagnostics.write_bundle(tmp_path / "bundle.jsonl")
    finally:
        del os.environ["CURSLINK_LOG_DIR"]
    records = [json.loads(line) for line in out.read_text().splitlines()]  # every line parses
    assert records[0]["type"] == "meta" and records[0]["format"] == diagnostics.BUNDLE_FORMAT
    assert any(r["type"] == "crash" and r["text"] == "boom" for r in records)
    logs = [r for r in records if r["type"] == "log"]
    assert [r["msg"] for r in logs] == ["a", "b", "started\nTraceback line"]  # merged in time order
    assert logs[2]["proc"] == "winhelper" and logs[2]["sub"] == "winhelper"
    assert "secret" not in out.read_text() and "covered by" not in out.read_text()


def test_records_are_flat_json_with_events_and_fields(tmp_path):
    from curslink import diagnostics

    diagnostics.set_identity("desk", "host")
    logger = logging.getLogger("curslink.router")
    rec = logger.makeRecord(logger.name, logging.INFO, __file__, 1, "switched to %s", ("laptop",), None,
                            extra={"event": "switch", "fields": {"to": "laptop", "ms": 3}})
    line = diagnostics.JsonFormatter().format(rec)
    d = json.loads(line)
    assert d["ts"].endswith("Z") and d["device"] == "desk" and d["role"] == "host"
    assert d["sub"] == "router" and d["msg"] == "switched to laptop" and d["lvl"] == "info"
    assert d["event"] == "switch" and d["fields"] == {"to": "laptop", "ms": 3}
    assert diagnostics.matches(d, ["sub=rout", "fields.to=LAPTOP"])
    assert not diagnostics.matches(d, ["-laptop"])
    assert diagnostics.matches(d, ["switch"], "info") and not diagnostics.matches(d, [], "warning")
    assert "router: switched to laptop [switch]" in diagnostics.short_line(d)


def test_the_json_log_is_written_beside_the_text_log(tmp_path):
    r = run_py("""
        import logging
        from curslink import diagnostics
        diagnostics.setup("host", console=False)
        diagnostics.event(logging.getLogger("curslink.net.udp"), "udp_quiet", "UDP went quiet", gap_ms=900)
    """, tmp_path)
    assert r.returncode == 0, r.stderr
    lines = [json.loads(x) for x in (tmp_path / "host.jsonl").read_text().splitlines()]
    hit = [x for x in lines if x.get("event") == "udp_quiet"]
    assert hit and hit[0]["fields"] == {"gap_ms": 900} and hit[0]["proc"] == "host" and hit[0]["sub"] == "net.udp"


def test_diag_show_filters_a_file(tmp_path):
    bundle = tmp_path / "b.jsonl"
    bundle.write_text("\n".join(json.dumps(r) for r in [
        {"type": "log", "ts": "1", "proc": "host", "lvl": "info", "sub": "net", "msg": "fine"},
        {"type": "log", "ts": "2", "proc": "host", "lvl": "error", "sub": "net", "msg": "broken"},
        {"type": "log", "ts": "3", "proc": "gui", "lvl": "error", "sub": "gui", "msg": "other"},
    ]) + "\n")
    r = subprocess.run([sys.executable, "-m", "curslink", "diag", "show", "-f", str(bundle), "--json", "-l", "error",
                        "proc=host"], env={**os.environ, "CURSLINK_LOG_DIR": str(tmp_path)},
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert [json.loads(x)["msg"] for x in r.stdout.splitlines()] == ["broken"]


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
