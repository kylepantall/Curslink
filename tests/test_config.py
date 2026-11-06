from pathlib import Path

import pytest

from curslink.config import ConfigError, agent_config, host_config, load_raw

EXAMPLE = Path(__file__).parent.parent / "examples" / "curslink.toml"


def test_example_config_loads():
    raw = load_raw(EXAMPLE)
    cfg = host_config(raw)
    assert cfg.device_id == "desktop"
    assert set(cfg.layout.devices) == {"desktop", "laptop"}
    assert cfg.gaze.settings.dwell_s == pytest.approx(0.35)
    agent = agent_config(raw)
    assert agent.device_id == "laptop" and agent.port == 24850


def test_env_secret_overrides(monkeypatch):
    monkeypatch.setenv("CURSLINK_SECRET", "from-the-environment")
    assert host_config(load_raw(EXAMPLE)).secret == "from-the-environment"


def test_host_must_be_a_known_device():
    raw = load_raw(EXAMPLE)
    raw["host"]["device_id"] = "nope"
    with pytest.raises(ConfigError):
        host_config(raw)


def test_missing_secret(monkeypatch):
    monkeypatch.delenv("CURSLINK_SECRET", raising=False)
    raw = load_raw(EXAMPLE)
    del raw["secret"]
    with pytest.raises(ConfigError):
        host_config(raw)


def test_bad_screen_size():
    raw = load_raw(EXAMPLE)
    raw["devices"][1]["screens"][0]["width"] = 0
    with pytest.raises(ConfigError):
        host_config(raw)
