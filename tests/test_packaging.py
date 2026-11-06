import sys

from curslink import cli, overlay


def test_overlay_helper_uses_python_m_from_source(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert overlay.helper_command() == [sys.executable, "-m", "curslink.overlay"]


def test_overlay_helper_reruns_packaged_executable(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert overlay.helper_command() == [sys.executable, "_overlay-helper"]


def test_hidden_overlay_helper_command(monkeypatch):
    calls = []
    monkeypatch.setattr(overlay, "run_helper", lambda: calls.append(True))
    assert cli.main(["_overlay-helper"]) == 0
    assert calls == [True]
