import hashlib
import io
import sys
import tarfile

import pytest

from curslink import cli, update


def test_version_order():
    assert update.is_newer("0.2.0", "0.1.0")
    assert update.is_newer("v0.10.0", "0.9.9")
    assert update.is_newer("0.2.0", "0.2.0-rc.2")
    assert update.is_newer("0.2.0-rc.10", "0.2.0-rc.2")
    assert not update.is_newer("0.1.0", "0.1.0")
    assert not update.is_newer("0.2.0-rc.1", "0.2.0")
    assert not update.is_newer("nightly", "0.1.0")


def test_settings():
    assert update.update_settings({}) == update.UpdateSettings()
    s = update.update_settings({"update": {"auto": False, "prerelease": True}})
    assert (s.check, s.auto, s.prerelease) == (True, False, True)
    with pytest.raises(ValueError):
        update.update_settings({"update": {"auto": "yes"}})


def test_asset_names():
    assert update.asset_name("1.2.3", "windows-setup", "windows-x64") == "curslink-1.2.3-windows-x64-setup.exe"
    assert update.asset_name("1.2.3", "macos-pkg", "macos-arm64") == "curslink-1.2.3-macos-arm64.pkg"
    assert update.asset_name("1.2.3", "deb", "linux-x64") == "curslink-1.2.3-linux-x64.deb"
    assert update.asset_name("1.2.3", "portable", "windows-x64") == "curslink-1.2.3-windows-x64.zip"
    assert update.asset_name("1.2.3", "portable", "linux-x64") == "curslink-1.2.3-linux-x64.tar.gz"


def test_install_kind(tmp_path, monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert update.install_kind() is None
    assert update.install_kind(tmp_path) == "portable"
    (tmp_path / "install-kind").write_text("deb\n")
    assert update.install_kind(tmp_path) == "deb"
    (tmp_path / "install-kind").write_text("something-else")
    assert update.install_kind(tmp_path) == "portable"


def test_install_kind_in_a_macos_bundle(tmp_path):
    macos = tmp_path / "Curslink.app" / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    (macos.parent / "Resources").mkdir()
    assert update.install_kind(macos) == "portable"
    (macos.parent / "Resources" / "install-kind").write_text("macos-pkg\n")
    assert update.install_kind(macos) == "macos-pkg"


def test_app_bundle(tmp_path, monkeypatch):
    macos = tmp_path / "Curslink.app" / "Contents" / "MacOS"
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(update, "app_dir", lambda: macos)
    assert update.app_bundle() == tmp_path / "Curslink.app"
    monkeypatch.setattr(update, "app_dir", lambda: tmp_path / "curslink")
    assert update.app_bundle() is None
    monkeypatch.setattr(update, "app_dir", lambda: None)
    assert update.app_bundle() is None


def test_parse_sums():
    a, b = "a" * 64, "B" * 64
    sums = update.parse_sums(f"{a}  one.zip\n{b} *two.pkg\nnot a line\n")
    assert sums == {"one.zip": a, "two.pkg": b.lower()}


class FakeGitHub:
    """Stands in for urllib: serves the given URLs' bytes."""

    def __init__(self, files):
        self.files = files

    def __call__(self, url, timeout, accept="application/octet-stream"):
        body = self.files[url]
        resp = io.BytesIO(body)
        resp.headers = {"Content-Length": str(len(body))}
        return resp


def _release(files, tag="v9.0.0"):
    return update.Release(tag.removeprefix("v"), tag, "https://example/release",
                          {name: f"https://dl/{name}" for name in files})


def test_download_verifies_checksum(tmp_path, monkeypatch):
    payload = b"installer bytes"
    sums = f"{hashlib.sha256(payload).hexdigest()}  app.deb\n".encode()
    monkeypatch.setattr(update, "_get", FakeGitHub({"https://dl/app.deb": payload, "https://dl/SHA256SUMS.txt": sums}))
    path = update.download(_release(["app.deb", "SHA256SUMS.txt"]), "app.deb", tmp_path)
    assert path.read_bytes() == payload


def test_download_rejects_tampered_file(tmp_path, monkeypatch):
    sums = f"{hashlib.sha256(b'the real one').hexdigest()}  app.deb\n".encode()
    monkeypatch.setattr(update, "_get", FakeGitHub({"https://dl/app.deb": b"evil", "https://dl/SHA256SUMS.txt": sums}))
    with pytest.raises(update.UpdateError, match="doesn't match"):
        update.download(_release(["app.deb", "SHA256SUMS.txt"]), "app.deb", tmp_path)
    assert not (tmp_path / "app.deb").exists()


def test_download_needs_sums(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "_get", FakeGitHub({}))
    with pytest.raises(update.UpdateError, match="SHA256SUMS"):
        update.download(_release(["app.deb"]), "app.deb", tmp_path)
    sums = b"0" * 64 + b"  other.deb\n"
    monkeypatch.setattr(update, "_get", FakeGitHub({"https://dl/SHA256SUMS.txt": sums}))
    with pytest.raises(update.UpdateError, match="isn't listed"):
        update.download(_release(["app.deb", "SHA256SUMS.txt"]), "app.deb", tmp_path)


def test_latest_release_picks_newest(monkeypatch):
    import json

    listing = [
        {"tag_name": "v0.3.0-rc.1", "prerelease": True, "assets": [], "html_url": "u1"},
        {"tag_name": "v0.2.0", "assets": [{"name": "x", "url": "ax", "browser_download_url": "dx"}], "html_url": "u2"},
        {"tag_name": "v0.4.0", "draft": True, "assets": []},
        {"tag_name": "nightly", "assets": []},
    ]
    monkeypatch.setattr(update, "_get", FakeGitHub({f"{update.API}?per_page=20": json.dumps(listing).encode()}))
    r = update.latest_release(prerelease=True)
    assert (r.version, r.assets) == ("0.3.0-rc.1", {})


def test_portable_update_replaces_folder(tmp_path, monkeypatch):
    if sys.platform == "win32":
        pytest.skip("Windows unpacks beside the running copy instead")
    current = tmp_path / "curslink"
    current.mkdir()
    (current / "curslink").write_text("old")
    (current / "stale").write_text("left over")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(current / "curslink"))

    archive = tmp_path / "new.tar.gz"
    with tarfile.open(archive, "w:gz") as t:
        data = b"new"
        info = tarfile.TarInfo("curslink/curslink")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    msg = update.install(archive, "portable", "9.0.0")
    assert "9.0.0" in msg
    assert (current / "curslink").read_text() == "new"
    assert not (current / "stale").exists()
    assert not (tmp_path / "curslink.old").exists()


def test_windows_script_waits_installs_and_relaunches():
    script = update._windows_script(
        update.Path(r"C:\Temp\setup.exe"), [r"C:\Apps\curslink.exe", "host", "-c", "my config.toml"], 4242
    )
    assert 'PID eq 4242' in script
    assert '"C:\\Temp\\setup.exe" /VERYSILENT' in script
    assert script.index("/VERYSILENT") < script.index('"C:\\Apps\\curslink.exe" "host" "-c" "my config.toml"')
    assert "set CURSLINK_NO_UPDATE=just-updated" in script


def test_on_start_respects_settings(monkeypatch):
    calls = []
    monkeypatch.delenv(update.SKIP_ENV, raising=False)
    monkeypatch.setattr(update, "check", lambda *a, **k: calls.append("check") or _release([]))
    monkeypatch.setattr(update, "update", lambda *a, **k: calls.append("update") or "ok")

    update.on_start({"update": {"check": False}}, ["host"])
    assert calls == []

    monkeypatch.setattr(update, "install_kind", lambda: "deb")
    update.on_start({"update": {"auto": False}}, ["host"])
    assert calls == ["check"]

    calls.clear()
    update.on_start({}, ["host"])
    assert calls == ["check", "update"]

    calls.clear()
    monkeypatch.setenv(update.SKIP_ENV, "1")
    update.on_start({}, ["host"])
    assert calls == []


def test_on_start_never_blocks_startup(monkeypatch):
    monkeypatch.delenv(update.SKIP_ENV, raising=False)
    monkeypatch.setattr(update, "install_kind", lambda: "deb")
    monkeypatch.setattr(update, "check", lambda *a, **k: _release([]))

    def boom(*a, **k):
        raise update.UpdateError("no deb in this release")

    monkeypatch.setattr(update, "update", boom)
    update.on_start({}, ["agent"])  # logs and carries on


def test_update_command_check_only(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(update, "check", lambda *a, **k: _release([]))
    assert cli.main(["update", "--check", "-c", str(tmp_path / "none.toml")]) == 0
    assert "9.0.0 is out" in capsys.readouterr().out

    monkeypatch.setattr(update, "check", lambda *a, **k: None)
    assert cli.main(["update", "-c", str(tmp_path / "none.toml")]) == 0
    assert "newest release" in capsys.readouterr().out


def test_install_kind_command(monkeypatch, capsys):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert cli.main(["_install-kind"]) == 0
    assert capsys.readouterr().out.strip() == "source"


def test_windows_setup_install_works_without_a_console(tmp_path, monkeypatch):
    """Regression: v0.4.1 called sys.stdout.flush() in the windowed app,
    where sys.stdout is None, and opened a console window for the setup."""
    from curslink import update

    setup = tmp_path / "curslink-9.0.0-windows-x64-setup.exe"
    setup.write_bytes(b"MZ")
    started = {}
    monkeypatch.setattr(update.subprocess, "Popen", lambda cmd, **kw: started.update(cmd=cmd, **kw))
    monkeypatch.setattr(update.sys, "stdout", None)
    monkeypatch.setattr(update.sys, "stderr", None)

    class Exited(Exception):
        pass

    def fake_exit(code):
        raise Exited(code)

    monkeypatch.setattr(update.os, "_exit", fake_exit)
    with pytest.raises(Exited):
        update.install(setup, "windows-setup", "9.0.0", relaunch=["gui"])
    assert started["cmd"][:2] == ["cmd", "/c"]
    assert started["creationflags"] & 0x08000000  # CREATE_NO_WINDOW
    assert (tmp_path / "curslink-9.0.0-windows-x64-setup.cmd").exists()
