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


def test_a_phones_edge_spans_every_monitor_on_that_side(tmp_path):
    from curslink.config import host_config
    from curslink.configedit import ConfigFile

    cfg = ConfigFile(tmp_path / "c.toml")
    cfg.set("host", "device_id", "mac")
    # A laptop with a taller monitor to its left, bottoms not lined up, and a
    # small one sitting on top of the laptop (its bottom isn't open).
    cfg.add_device("mac", "Mac", [
        {"id": "main", "x": 0, "y": 400, "width": 1512, "height": 982},
        {"id": "big", "x": -2560, "y": 0, "width": 2560, "height": 1440},
        {"id": "top", "x": 200, "y": -680, "width": 1200, "height": 1080},
    ])
    cfg.add_device("phone", "Pixel", [{"id": "phone", "x": 0, "y": 0, "width": 384, "height": 832}])
    cfg.place_beside("phone", "mac", "bottom", whole_side=True)
    links = {(lk["a"], tuple(lk["a_span"])): (lk["b"], lk["b_side"], tuple(lk["b_span"])) for lk in cfg.links()}
    assert links == {
        ("mac/main", (0, 1512)): ("phone/phone", "top", (0, 384)),
        ("mac/big", (0, 2560)): ("phone/phone", "top", (0, 384)),
    }
    assert all(lk["a_side"] == "bottom" for lk in cfg.links())
    cfg.save()
    layout = host_config(cfg.raw(), base_dir=tmp_path).layout
    # off the bottom of either monitor, at the same fraction along the phone
    big, main = layout.screen("mac", "big"), layout.screen("mac", "main")
    for screen, along, want in ((big, 1280, 192), (main, 1511, 383)):
        c = layout.find_crossing("mac", screen.x + along, screen.bottom - 1, 0, 1)
        assert c is not None, screen.id
        assert (c.screen.device_id, round(c.x - c.screen.x)) == ("phone", want), screen.id
    # and the phone's top edge comes back
    phone = layout.screen("phone", "phone")
    c = layout.find_crossing("phone", phone.x + 100, phone.y, 0, -1)
    assert c is not None and c.screen.device_id == "mac"
    # picking another edge drops these links
    cfg.place_beside("phone", "mac", "left")
    assert cfg.links() == []


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


def test_keymap_tables_load_per_pair():
    raw = load_raw(EXAMPLE)
    raw["keymap"] = {"linux>windows": {"caps_lock": "ctrl"}, "device:laptop": {"cmd": "alt"}}
    cfg = host_config(raw)
    assert cfg.keymaps.key("linux", "Windows 11", "caps_lock") == "ctrl"
    assert cfg.keymaps.key("darwin", "Linux", "cmd_l", "laptop") == "alt_l"
    raw["keymap"] = {"mac-windows": {}}
    with pytest.raises(ConfigError):
        host_config(raw)
    raw["keymap"] = {"mac>windows": {"cmd": 3}}
    with pytest.raises(ConfigError):
        host_config(raw)


def test_agent_modifier_map_is_written_by_family():
    raw = load_raw(EXAMPLE)
    raw.setdefault("agent", {})["modifier_map"] = {"cmd": "ctrl"}
    assert agent_config(raw).modifier_map["cmd_r"] == "ctrl_r"


def test_forgetting_and_renaming_a_device_follows_its_references(tmp_path):
    from curslink.configedit import ConfigFile, new_host_config

    path = tmp_path / "curslink.toml"
    path.write_text(new_host_config("desk", "Desk", [{"id": "main", "x": 0, "y": 0, "width": 1920,
                                                       "height": 1080}]), encoding="utf-8")
    cfg = ConfigFile(path)
    cfg.add_device("phone", "Phone", [{"id": "phone", "x": 0, "y": 0, "width": 400, "height": 900}])
    cfg.set_link("desk/main", "right", (0, 900), "phone/phone", "left", (0, 900))
    cfg.set_fit("desk/main", "phone/phone", 10, 10, 0.5)
    cfg.set_mirror_joined({"desk/main": ["phone/phone"], "phone/phone": "all"})
    cfg.set("gaze", "jump_screens", ["desk/main", "phone/phone"])
    cfg.set_keymap("device:phone", {"cmd": "alt"})
    assert cfg.rename_device("phone", "phone2", "Phone 2")
    cfg.save()
    cfg = ConfigFile(path)
    assert {lk["b"] for lk in cfg.links()} == {"phone2/phone"}
    assert cfg.fits()[0]["front"] == "phone2/phone"
    assert cfg.get("layout", "mirror_joined") == {"desk/main": ["phone2/phone"], "phone2/phone": "all"}
    assert cfg.get("gaze", "jump_screens") == ["desk/main", "phone2/phone"]
    assert cfg.keymap("device:phone2") == {"cmd": "alt"} and cfg.keymap("device:phone") is None

    assert cfg.forget_device("phone2")
    cfg.save()
    cfg = ConfigFile(path)
    assert [d["id"] for d in cfg.devices()] == ["desk"]
    assert cfg.links() == [] and cfg.fits() == []
    assert cfg.get("layout", "mirror_joined") is None
    assert cfg.get("gaze", "jump_screens") == ["desk/main"]
    assert cfg.keymap("device:phone2") is None
    host_config(load_raw(path), base_dir=tmp_path)  # still a config that loads
