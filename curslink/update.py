"""Updating an installed Curslink to the newest GitHub release.

Every release carries an installer per platform plus ``SHA256SUMS.txt``.
The updater finds the newest release, downloads the file that matches how
this copy was installed, checks it against ``SHA256SUMS.txt`` and runs it:

* Windows setup (per-user, no admin): the setup runs silently after
  Curslink exits, then Curslink starts again with the same arguments.
* macOS ``.pkg`` and Linux ``.deb``: installed with ``installer`` / ``dpkg``
  (asking for an admin password), then Curslink restarts in place.
* The portable archives: unpacked over the old folder on macOS and Linux;
  on Windows, unpacked beside it (a running program's folder can't be
  replaced there).

The repository is public, so no token is needed. ``CURSLINK_GITHUB_TOKEN``
(or ``GITHUB_TOKEN``) is sent if set, e.g. for a private fork or to lift
GitHub's rate limit for anonymous API calls.

The installers drop an ``install-kind`` file next to the executable, which
is how the updater knows which of these it is. ``curslink update`` does it
by hand; with ``[update] auto = true`` (the default) an installed build does
it by itself when ``curslink host`` or ``curslink agent`` starts.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from curslink import __version__

log = logging.getLogger(__name__)

REPO = "kylepantall/Curslink"
API = f"https://api.github.com/repos/{REPO}/releases"
SUMS_NAME = "SHA256SUMS.txt"
# Set to anything to switch updates off without touching the config. The
# updater sets it to "just-updated" in the restarted process, which skips the
# startup check but keeps the daily one.
SKIP_ENV = "CURSLINK_NO_UPDATE"

KINDS = ("windows-setup", "macos-pkg", "deb", "portable")


class UpdateError(Exception):
    pass


@dataclass
class UpdateSettings:
    check: bool = True        # look for a newer release when host/agent start
    auto: bool = True         # and install it (installed builds only)
    prerelease: bool = False  # consider release candidates too


def update_settings(raw: dict[str, Any]) -> UpdateSettings:
    t = raw.get("update", {})
    if not isinstance(t, dict):
        raise ValueError("[update] must be a table")
    s = UpdateSettings()
    for name in ("check", "auto", "prerelease"):
        if name in t:
            if not isinstance(t[name], bool):
                raise ValueError(f"[update] {name} must be true or false")
            setattr(s, name, t[name])
    return s


# --- Versions ------------------------------------------------------------

_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+.*)?$")


def parse_version(text: str) -> tuple:
    """A sortable key for ``1.2.3`` / ``v1.2.3-rc.1``; a release sorts after its pre-releases."""
    m = _VERSION.match(text.strip())
    if not m:
        raise ValueError(f"not a version: {text!r}")
    core = tuple(int(x) for x in m.group(1, 2, 3))
    pre = m.group(4)
    if pre is None:
        return core + (1, ())
    parts = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split("."))
    return core + (0, parts)


def is_newer(candidate: str, current: str = __version__) -> bool:
    try:
        return parse_version(candidate) > parse_version(current)
    except ValueError:
        return False


# --- Where and how this copy is installed ---------------------------------

def platform_key() -> str:
    machine = platform.machine().lower()
    arch = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(machine, machine)
    system = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
    return f"{system}-{arch}"


def app_dir() -> Path | None:
    """The folder of the packaged app, or None when running from source."""
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent


def app_bundle() -> Path | None:
    """macOS: the Curslink.app this packaged copy runs from, if it's one."""
    folder = app_dir()
    if sys.platform != "darwin" or folder is None:
        return None
    bundle = folder.parent.parent
    return bundle if folder.name == "MacOS" and bundle.suffix == ".app" else None


def install_kind(folder: Path | None = None) -> str | None:
    """``windows-setup``, ``macos-pkg``, ``deb``, ``portable``, or None from source."""
    folder = folder or app_dir()
    if folder is None:
        return None
    # In a macOS app bundle it's in Contents/Resources, beside Contents/MacOS.
    for path in (folder / "install-kind", folder.parent / "Resources" / "install-kind"):
        try:
            kind = path.read_text().strip()
            break
        except OSError:
            continue
    else:
        return "portable"
    return kind if kind in KINDS else "portable"


def asset_name(version: str, kind: str, plat: str | None = None) -> str:
    plat = plat or platform_key()
    base = f"curslink-{version}-{plat}"
    if kind == "windows-setup":
        return f"{base}-setup.exe"
    if kind == "macos-pkg":
        return f"{base}.pkg"
    if kind == "deb":
        return f"{base}.deb"
    return f"{base}.zip" if plat.startswith("windows") else f"{base}.tar.gz"


# --- Talking to GitHub -----------------------------------------------------

def github_token() -> str | None:
    """An optional token (CURSLINK_GITHUB_TOKEN or GITHUB_TOKEN), for a private fork or rate limits."""
    return os.environ.get("CURSLINK_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN") or None


def _get(url: str, timeout: float, accept: str = "application/octet-stream"):
    req = urllib.request.Request(url, headers={
        "User-Agent": f"curslink/{__version__}",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
    })
    if token := github_token():
        # Unredirected: downloads redirect to a signed storage URL that
        # refuses a second set of credentials.
        req.add_unredirected_header("Authorization", f"Bearer {token}")
    return urllib.request.urlopen(req, timeout=timeout)


@dataclass
class Release:
    version: str
    tag: str
    url: str                  # the release page
    assets: dict[str, str]    # name -> download URL

    def asset_for(self, kind: str, plat: str | None = None) -> tuple[str, str] | None:
        name = asset_name(self.version, kind, plat)
        return (name, self.assets[name]) if name in self.assets else None


def _release(data: dict[str, Any]) -> Release | None:
    tag = data.get("tag_name", "")
    if data.get("draft") or not _VERSION.match(tag):
        return None
    return Release(
        version=tag.removeprefix("v"),
        tag=tag,
        url=data.get("html_url", ""),
        # The API URL rather than browser_download_url: it serves the file
        # to a token holder when the repository is private, too.
        assets={a["name"]: a.get("url") or a["browser_download_url"] for a in data.get("assets", [])},
    )


def latest_release(prerelease: bool = False, timeout: float = 10.0) -> Release | None:
    """The newest published release (optionally including pre-releases)."""
    url = f"{API}?per_page=20" if prerelease else f"{API}/latest"
    try:
        with _get(url, timeout, "application/vnd.github+json") as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and not github_token():
            raise UpdateError(
                f"{REPO} has no releases, or is private: set CURSLINK_GITHUB_TOKEN to a token that can read it"
            ) from exc
        raise UpdateError(f"GitHub said {exc.code} {exc.reason}") from exc
    except Exception as exc:
        raise UpdateError(f"couldn't reach GitHub: {exc}") from exc
    releases = [_release(d) for d in (data if isinstance(data, list) else [data])]
    releases = [r for r in releases if r]
    if not releases:
        return None
    return max(releases, key=lambda r: parse_version(r.version))


def parse_sums(text: str) -> dict[str, str]:
    sums = {}
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            sums[parts[1].lstrip("*")] = parts[0].lower()
    return sums


def download(release: Release, name: str, dest_dir: Path, timeout: float = 60.0,
             progress: Callable[[int, int], None] | None = None) -> Path:
    """Download one asset into ``dest_dir`` and check it against SHA256SUMS.txt."""
    if SUMS_NAME not in release.assets:
        raise UpdateError(f"release {release.tag} has no {SUMS_NAME}; not installing an unverified file")
    if name not in release.assets:
        raise UpdateError(f"release {release.tag} has no {name}")
    with _get(release.assets[SUMS_NAME], timeout) as resp:
        sums = parse_sums(resp.read(1 << 20).decode("utf-8", "replace"))
    expected = sums.get(name)
    if not expected:
        raise UpdateError(f"{name} isn't listed in {SUMS_NAME}; not installing it")
    path = dest_dir / name
    digest = hashlib.sha256()
    with _get(release.assets[name], timeout) as resp, open(path, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while chunk := resp.read(1 << 16):
            out.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    if digest.hexdigest() != expected:
        path.unlink(missing_ok=True)
        raise UpdateError(f"{name} doesn't match {SUMS_NAME} (corrupt or tampered download); not installing it")
    return path


# --- Installing --------------------------------------------------------------

def _as_admin(cmd: list[str]) -> list[str]:
    """``cmd`` with whatever it takes to run it as root on macOS or Linux."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return cmd
    if sys.stdin and sys.stdin.isatty() and shutil.which("sudo"):
        return ["sudo", *cmd]
    if sys.platform == "darwin":
        script = " ".join(_sh_quote(c) for c in cmd)
        return ["osascript", "-e",
                f'do shell script "{script}" with prompt "Curslink wants to install an update." '
                "with administrator privileges"]
    if shutil.which("pkexec"):
        return ["pkexec", *cmd]
    raise UpdateError("installing needs admin rights; run `sudo curslink update`")


def _sh_quote(s: str) -> str:
    # For a string inside AppleScript's double quotes, run by /bin/sh.
    return "'" + s.replace("'", "'\\''").replace("\\", "\\\\").replace('"', '\\"') + "'"


def _relaunch_argv(argv: list[str] | None) -> list[str] | None:
    return None if argv is None else [sys.executable, *argv]


def _relaunch(argv: list[str], version: str) -> None:
    env = dict(os.environ, **{SKIP_ENV: "just-updated"})
    log.info("Restarting Curslink %s", version)
    os.execve(argv[0], argv, env)


def _windows_script(setup: Path, relaunch: list[str] | None, pid: int) -> str:
    """A batch file that waits for this process to exit, runs the setup, then restarts Curslink."""
    def q(s: str) -> str:
        return '"' + s.replace('"', '') + '"'

    lines = [
        "@echo off",
        # Wait for Curslink (this pid) to exit so the setup can replace it.
        ":wait",
        f'tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul && (ping -n 2 127.0.0.1 >nul & goto wait)',
        f"{q(str(setup))} /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS",
    ]
    if relaunch:
        lines.append(f"set {SKIP_ENV}=just-updated")
        lines.append(" ".join(q(a) for a in relaunch))
    lines.append('(goto) 2>nul & del "%~f0"')
    return "\r\n".join(lines) + "\r\n"


def install(path: Path, kind: str, version: str, relaunch: list[str] | None = None) -> str:
    """Install a downloaded, verified file. May not return (it restarts Curslink).

    Returns a message for the user when it does return.
    """
    if kind == "windows-setup":
        script = path.with_suffix(".cmd")
        script.write_text(_windows_script(path, _relaunch_argv(relaunch), os.getpid()))
        # No console window: the setup runs silently and Curslink restarts by itself.
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
        subprocess.Popen(["cmd", "/c", str(script)], creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # The setup can't replace curslink.exe while it runs: exit now.
        log.info("Curslink %s is installing; it will start again when done", version)
        _flush_streams()
        # Exiting under a running Qt app can fault in native teardown, which
        # wrote a false "crashed" report after every successful update.
        import faulthandler

        faulthandler.disable()
        os._exit(0)
    if kind == "macos-pkg":
        _run(_as_admin(["/usr/sbin/installer", "-pkg", str(path), "-target", "/"]))
    elif kind == "deb":
        _run(_as_admin(["dpkg", "-i", str(path)]))
    elif kind == "portable":
        return _install_portable(path, version, relaunch)
    else:
        raise UpdateError(f"don't know how to install a {kind!r} build")
    if relaunch is not None:
        _relaunch(_relaunch_argv(relaunch), version)
    return f"Installed Curslink {version}."


def _flush_streams() -> None:
    """Flush what's printed and logged before exiting. The windowed app
    (curslinkw.exe, the macOS app) has no console: ``sys.stdout`` is None."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.flush()
        except Exception:
            pass
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:
            pass


def _run(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise UpdateError(f"the installer failed: {exc}") from exc


def _unpack(archive: Path, dest: Path) -> Path:
    """Unpack a release archive into ``dest``; returns its ``curslink`` folder."""
    dest.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for member in z.namelist():
                if member.startswith(("/", "\\")) or ".." in Path(member).parts:
                    raise UpdateError(f"unsafe path in {archive.name}: {member}")
            z.extractall(dest)
    else:
        with tarfile.open(archive) as t:
            t.extractall(dest, filter="data")
    folder = dest / "curslink"
    if not folder.is_dir():
        raise UpdateError(f"{archive.name} doesn't contain a curslink folder")
    return folder


def _install_portable(archive: Path, version: str, relaunch: list[str] | None) -> str:
    current = app_dir()
    if current is None:
        raise UpdateError("not a packaged build")
    if sys.platform == "win32":
        # A running program's folder can't be renamed on Windows.
        target = current.parent / f"curslink-{version}"
        if target.exists():
            raise UpdateError(f"{target} already exists")
        tmp = Path(tempfile.mkdtemp(dir=current.parent))
        _unpack(archive, tmp).rename(target)
        shutil.rmtree(tmp, ignore_errors=True)
        return f"Unpacked Curslink {version} to {target}. Run it from there (or use the installer, which updates itself)."
    tmp = Path(tempfile.mkdtemp(prefix=".curslink-update-", dir=current.parent))
    try:
        new = _unpack(archive, tmp)
        old = current.with_name(current.name + ".old")
        shutil.rmtree(old, ignore_errors=True)
        current.rename(old)
        new.rename(current)
    except OSError as exc:
        raise UpdateError(f"couldn't replace {current}: {exc}") from exc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(current.with_name(current.name + ".old"), ignore_errors=True)
    if relaunch is not None:
        _relaunch([str(current / Path(sys.executable).name), *relaunch], version)
    return f"Updated Curslink in {current} to {version}."


# --- Entry points -------------------------------------------------------------

def check(settings: UpdateSettings | None = None, timeout: float = 10.0) -> Release | None:
    """The newest release if it's newer than this one, else None."""
    settings = settings or UpdateSettings()
    release = latest_release(settings.prerelease, timeout)
    return release if release and is_newer(release.version) else None


def update(release: Release, kind: str, relaunch: list[str] | None = None,
           progress: Callable[[int, int], None] | None = None) -> str:
    asset = release.asset_for(kind)
    if asset is None:
        raise UpdateError(
            f"release {release.tag} has no {asset_name(release.version, kind)}; "
            f"download it from {release.url}"
        )
    tmp = Path(tempfile.mkdtemp(prefix="curslink-update-"))
    try:
        path = download(release, asset[0], tmp, progress=progress)
        return install(path, kind, release.version, relaunch)
    finally:
        # (Not reached when Curslink restarts. The Windows setup runs after
        # this process exits, and its script deletes itself; the temporary
        # folder is left for the OS to clean up.)
        if kind != "windows-setup":
            shutil.rmtree(tmp, ignore_errors=True)


def on_start(raw: dict[str, Any], argv: list[str]) -> None:
    """Called as ``curslink host`` / ``agent`` start: update first if one is out.

    Never stops Curslink from starting: any failure is logged and skipped.
    """
    if os.environ.get(SKIP_ENV):
        return
    try:
        settings = update_settings(raw)
    except ValueError as exc:
        log.warning("Ignoring the update settings: %s", exc)
        return
    if not settings.check:
        return
    kind = install_kind()
    try:
        release = check(settings, timeout=4.0)
    except UpdateError as exc:
        log.debug("Update check skipped: %s", exc)
        return
    if release is None:
        return
    if kind is None or not settings.auto:
        how = "pip install -U" if kind is None else "curslink update"
        log.warning("Curslink %s is out (you have %s): %s, or `%s`", release.version, __version__, release.url, how)
        return
    log.info("Updating Curslink %s to %s", __version__, release.version)
    try:
        log.info(update(release, kind, relaunch=argv))
    except Exception as exc:
        log.warning("Couldn't update to %s (%s); carrying on with %s", release.version, exc, __version__)


def watch(raw: dict[str, Any], every_s: float = 24 * 3600) -> threading.Thread | None:
    """While Curslink runs, look for a release once a day and say when one is out."""
    try:
        settings = update_settings(raw)
    except ValueError:
        return None
    if not settings.check or os.environ.get(SKIP_ENV, "just-updated") != "just-updated":
        return None
    stop = threading.Event()
    told: set[str] = set()

    def loop():
        while not stop.wait(every_s):
            try:
                release = check(settings)
            except UpdateError:
                continue
            if release and release.version not in told:
                told.add(release.version)
                log.warning("Curslink %s is out; it installs the next time Curslink starts (or run `curslink update`)",
                            release.version)

    t = threading.Thread(target=loop, name="curslink-update-watch", daemon=True)
    t.start()
    return t
