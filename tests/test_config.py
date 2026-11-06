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


def test_secret_is_optional_with_pairing(monkeypatch):
    monkeypatch.delenv("CURSLINK_SECRET", raising=False)
    raw = load_raw(EXAMPLE)
    raw.pop("secret", None)
    assert host_config(raw).secret is None
    assert agent_config(raw).secret is None


def test_agent_host_is_optional(monkeypatch):
    raw = load_raw(EXAMPLE)
    raw["agent"].pop("host", None)
    cfg = agent_config(raw)
    assert cfg.host == "" and cfg.pairing.enabled and cfg.pairing.port == 24851
    raw["pairing"] = {"enabled": False}
    with pytest.raises(ConfigError):
        agent_config(raw)


def test_pairing_store_is_relative_to_config(tmp_path):
    raw = load_raw(EXAMPLE)
    raw["pairing"] = {"store": "keys/pairings.json"}
    assert host_config(raw, base_dir=tmp_path).pairing.store == tmp_path / "keys" / "pairings.json"


def test_bad_screen_size():
    raw = load_raw(EXAMPLE)
    raw["devices"][1]["screens"][0]["width"] = 0
    with pytest.raises(ConfigError):
        host_config(raw)
