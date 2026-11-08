from pathlib import Path

import pytest

from curslink.config import ConfigError, agent_config, host_config, load_raw

EXAMPLE = Path(__file__).parent.parent / "examples" / "curslink.toml"


def test_example_config_loads():
    raw = load_raw(EXAMPLE)
    cfg = host_config(raw)
    assert cfg.device_id == "desktop"
    assert set(cfg.layout.devices) == {"desktop", "laptop"}
    assert cfg.gaze.settings.dwell_s == pytest.approx(0.25)
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


def test_jump_screens_are_parsed_and_unknown_ones_skipped():
    raw = load_raw(EXAMPLE)
    raw["gaze"]["jump_screens"] = [f"{raw['host']['device_id']}/{raw['devices'][0]['screens'][0]['id']}",
                                   "gone/main"]
    cfg = host_config(raw)
    first = raw["devices"][0]
    assert cfg.router.gaze_jump_screens == {(first["id"], first["screens"][0]["id"])}
    raw["gaze"]["jump_screens"] = "everything"
    with pytest.raises(ConfigError):
        host_config(raw)


def test_place_a_phone_against_an_edge(tmp_path):
    from curslink.configedit import ConfigFile

    cfg = ConfigFile(tmp_path / "c.toml")
    cfg.add_device("mac", "Mac", [{"id": "main", "x": 0, "y": 0, "width": 1512, "height": 982}])
    cfg.add_device("phone", "Pixel", [{"id": "phone", "x": 0, "y": 0, "width": 411, "height": 914}])
    assert cfg.side_of("phone", "mac") == "right"
    cfg.place_beside("phone", "mac", "left")
    (s,) = cfg.raw()["devices"][1]["screens"]
    assert (s["x"], s["y"]) == (-411, 34)
    assert cfg.side_of("phone", "mac") == "left"
    cfg.place_beside("phone", "mac", "bottom")
    (s,) = cfg.raw()["devices"][1]["screens"]
    assert (s["x"], s["y"]) == (550, 982)
    assert cfg.side_of("phone", "mac") == "bottom"


def test_switching_roles_keeps_both_roles_settings(tmp_path):
    from curslink.configedit import ConfigFile, new_host_config, stash_path, switch_role

    path = tmp_path / "curslink.toml"
    screens = [{"id": "main", "x": 0, "y": 0, "width": 2560, "height": 1440}]
    path.write_text(new_host_config("desk-pc", "Desk PC", screens), encoding="utf-8")
    cfg = ConfigFile(path)
    cfg.set("host", "effect", "blink")
    cfg.set("pairing", "port", 25000)
    cfg.save()

    assert switch_role(path, screens) == "agent"
    agent = ConfigFile(path)
    assert agent.role == "agent"
    assert agent.get("agent", "device_id") == "desk-pc"
    assert agent.get("agent", "name") == "Desk PC"
    assert agent.get("pairing", "port") == 25000  # shared settings carry over
    assert stash_path(path, "host").exists()
    agent.set("agent", "animations", False)
    agent.save()

    assert switch_role(path, screens) == "host"
    host = ConfigFile(path)
    assert host.role == "host"
    assert host.get("host", "effect") == "blink"  # the old host settings came back
    assert not stash_path(path, "host").exists()
    assert stash_path(path, "agent").exists()

    assert switch_role(path, screens) == "agent"
    assert ConfigFile(path).get("agent", "animations") is False


def test_switching_roles_ignores_a_broken_side_file(tmp_path):
    from curslink.configedit import ConfigFile, new_agent_config, stash_path, switch_role

    path = tmp_path / "curslink.toml"
    path.write_text(new_agent_config("laptop", "Laptop"), encoding="utf-8")
    stash_path(path, "host").write_text('[host]\neffect = "pop"\n', encoding="utf-8")  # no [[devices]]
    assert switch_role(path, [{"x": 0, "y": 0, "width": 1920, "height": 1080}]) == "host"
    host = ConfigFile(path)
    assert host.get("host", "device_id") == "laptop"
    assert [d["name"] for d in host.devices()] == ["Laptop"]
