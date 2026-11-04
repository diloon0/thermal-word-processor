"""Automatic updates for ThermalWordProcessor.exe, published as GitHub Releases.

Each release on GitHub carries two files:
  ThermalWordProcessor.exe          the new version
  ThermalWordProcessor.exe.sha256   its fingerprint (SHA-256), so a damaged or swapped download is refused

How an update is installed: Windows won't let a running .exe be overwritten, but it can be
renamed. So the running copy renames itself to "...old.exe", the new download takes its place,
the new version starts, and the old copy is deleted the next time the app runs.

Only the packaged .exe updates itself; running thermal_word_processor.py as a script never does.
"""

import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = os.environ.get("TWP_UPDATE_API", "https://api.github.com")   # tests point this at a local server
EXE_ASSET = "ThermalWordProcessor.exe"
HASH_ASSET = EXE_ASSET + ".sha256"
USER_AGENT = "ThermalWordProcessor-updater"
MAX_DOWNLOAD = 200 * 1024 * 1024


class UpdateError(Exception):
    """Something went wrong checking for or installing an update. The message is friendly."""


def parse_version(text):
    try:
        return tuple(int(part) for part in str(text).strip().lstrip("vV").split("."))
    except ValueError:
        return (0,)


def exe_path():
    return Path(sys.executable).resolve()


def old_copy_path():
    exe = exe_path()
    return exe.with_name(exe.stem + ".old" + exe.suffix)


def fetch(url, timeout=20, limit=MAX_DOWNLOAD):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(limit + 1)
    except urllib.error.HTTPError as err:
        if err.code == 404:
            raise UpdateError("No published versions were found yet.")
        raise UpdateError(f"GitHub answered with an error ({err.code}).")
    except (urllib.error.URLError, OSError) as err:
        raise UpdateError("Couldn't reach GitHub. Is this computer online?") from err
    if len(data) > limit:
        raise UpdateError("The download was unexpectedly large, so it was stopped.")
    return data


def latest_release(repo):
    """{"version", "notes", "exe_url", "hash_url"} for the newest published release."""
    try:
        info = json.loads(fetch(f"{API}/repos/{repo}/releases/latest", limit=5 * 1024 * 1024))
        assets = {a["name"]: a["browser_download_url"] for a in info.get("assets", [])}
        version = info["tag_name"]
    except (ValueError, KeyError, TypeError):
        raise UpdateError("GitHub's answer didn't make sense.")
    if EXE_ASSET not in assets or HASH_ASSET not in assets:
        raise UpdateError("The newest release is missing its files.")
    return {"version": version.lstrip("vV"), "notes": info.get("body") or "",
            "exe_url": assets[EXE_ASSET], "hash_url": assets[HASH_ASSET]}


def check(repo, current_version):
    """The newest release if it's newer than current_version, otherwise None."""
    release = latest_release(repo)
    return release if parse_version(release["version"]) > parse_version(current_version) else None


def download(release):
    """Download and verify the new .exe next to the running one. Returns its path."""
    expected = fetch(release["hash_url"], limit=4096).decode("ascii", "replace").split()
    if not expected or len(expected[0]) != 64:
        raise UpdateError("The release's fingerprint file is broken.")
    data = fetch(release["exe_url"], timeout=180)
    if hashlib.sha256(data).hexdigest().lower() != expected[0].lower():
        raise UpdateError("The download didn't match its fingerprint, so it wasn't used.")
    new = exe_path().with_name(exe_path().stem + ".new" + exe_path().suffix)
    try:
        new.write_bytes(data)
    except OSError as err:
        raise UpdateError("Couldn't save the update next to the app. Is its folder read-only?") from err
    return new


def install_and_restart(new_exe):
    """Swap the new .exe into place and start it. The caller should close its window afterwards."""
    exe, old = exe_path(), old_copy_path()
    try:
        if old.exists():
            old.unlink()
        os.replace(exe, old)          # a running .exe can be renamed, just not overwritten
    except OSError as err:
        raise UpdateError("Couldn't move the old version aside.") from err
    try:
        os.replace(new_exe, exe)
    except OSError as err:
        os.replace(old, exe)          # put things back as they were
        raise UpdateError("Couldn't put the new version in place.") from err
    env = dict(os.environ)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"   # start fresh, not as a child of this copy
    subprocess.Popen([str(exe)], env=env, close_fds=True,
                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)


def remove_old_copy():
    """Delete the copy an update left behind (it may still be closing, so failures are fine)."""
    try:
        old_copy_path().unlink()
    except OSError:
        pass
