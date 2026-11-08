"""Getting `adb` for people who do not have it.

"Install the Android platform tools" is where most people give up, so the tool
fetches them itself. Google publishes a standalone zip per platform with a
stable URL and no account, no SDK manager and no Android Studio:

    https://dl.google.com/android/repository/platform-tools-latest-<os>.zip

That one host is the trust anchor, same rule as the APK catalogue. We do not
vendor the binaries into the repo: platform-tools is around 10 MB per platform
compressed, it is updated every few weeks, and a checked-in copy would be
stale and wrong by the time anyone cloned it.

What this cannot do is install a **Windows USB driver**. See docs/ADB.md —
short version: on Windows, use wireless debugging and the driver problem
disappears entirely.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import stat
import sys
import urllib.error
import urllib.request
import zipfile

from .journal import data_dir

BASE = "https://dl.google.com/android/repository"
UA = "freemyandroid/0.1 (+https://github.com/manishb27/freemyandroid)"

# Google's own names for the three builds.
PLATFORMS = {
    "linux": "platform-tools-latest-linux.zip",
    "darwin": "platform-tools-latest-darwin.zip",
    "windows": "platform-tools-latest-windows.zip",
}


class ToolsError(RuntimeError):
    pass


def host_platform() -> str:
    s = sys.platform
    if s.startswith("linux"):
        return "linux"
    if s == "darwin":
        return "darwin"
    if s in ("win32", "cygwin", "msys"):
        return "windows"
    raise ToolsError(f"no platform-tools build for {s!r}. Install adb by hand.")


def install_dir() -> str:
    return os.path.join(data_dir(), "platform-tools")


def installed_adb() -> str | None:
    """Path to an adb we installed earlier, or None."""
    exe = "adb.exe" if host_platform() == "windows" else "adb"
    path = os.path.join(install_dir(), exe)
    return path if os.access(path, os.X_OK) else None


def url_for(plat: str | None = None) -> str:
    plat = plat or host_platform()
    if plat not in PLATFORMS:
        raise ToolsError(f"unknown platform {plat!r}")
    return f"{BASE}/{PLATFORMS[plat]}"


def download(plat: str | None = None, dest_dir: str | None = None,
             progress=None) -> str:
    """Fetch the zip. Returns the path to it."""
    plat = plat or host_platform()
    url = url_for(plat)
    dest_dir = dest_dir or os.path.join(data_dir(), "download")
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, PLATFORMS[plat])
    tmp = path + ".part"

    req = urllib.request.Request(url, headers={"User-Agent": UA})
    h = hashlib.sha256()
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            with open(tmp, "wb") as f:
                while True:
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
    except urllib.error.HTTPError as e:
        _unlink(tmp)
        raise ToolsError(f"{url} returned HTTP {e.code}") from None
    except urllib.error.URLError as e:
        _unlink(tmp)
        raise ToolsError(f"could not reach {url}: {e.reason}") from None

    if not zipfile.is_zipfile(tmp):
        _unlink(tmp)
        raise ToolsError(
            "what we downloaded is not a zip. Some networks return a block "
            "page with a 200 status.")
    os.replace(tmp, path)
    return path


def extract(zip_path: str, target: str | None = None) -> str:
    """Unpack into `target`. Returns the directory holding adb."""
    target = target or install_dir()
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)
    staging = target + ".new"
    if os.path.isdir(staging):
        shutil.rmtree(staging)

    with zipfile.ZipFile(zip_path) as z:
        # The zip contains a single top-level `platform-tools/` directory.
        # Every member is checked against the destination before extraction:
        # a zip is untrusted input, and a member named ../../.bashrc would
        # otherwise be written outside the target.
        root = os.path.abspath(staging)
        for member in z.namelist():
            out = os.path.abspath(os.path.join(root, member))
            if out != root and not out.startswith(root + os.sep):
                raise ToolsError(f"zip tried to write outside the target: {member!r}")
        z.extractall(staging)

    inner = os.path.join(staging, "platform-tools")
    src = inner if os.path.isdir(inner) else staging

    if os.path.isdir(target):
        shutil.rmtree(target)
    shutil.move(src, target)
    if os.path.isdir(staging):
        shutil.rmtree(staging, ignore_errors=True)

    # The zip carries no POSIX permissions, so everything arrives non-executable.
    if host_platform() != "windows":
        for name in os.listdir(target):
            p = os.path.join(target, name)
            if os.path.isfile(p) and ("." not in name or name.endswith(".so")):
                st = os.stat(p)
                os.chmod(p, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        adb = os.path.join(target, "adb")
        if os.path.isfile(adb):
            st = os.stat(adb)
            os.chmod(adb, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


def ensure(progress=None) -> str:
    """The path to a working adb: the one on PATH, ours, or a fresh download."""
    from . import adb as adb_mod
    try:
        return adb_mod.find_adb()
    except adb_mod.AdbError:
        pass
    here = installed_adb()
    if here:
        return here
    zip_path = download(progress=progress)
    target = extract(zip_path)
    exe = "adb.exe" if host_platform() == "windows" else "adb"
    path = os.path.join(target, exe)
    if not os.access(path, os.X_OK):
        raise ToolsError(f"extracted platform-tools but {path} is not executable")
    return path


def bundle_all(dest: str, progress=None) -> list[str]:
    """Download all three platforms' zips into `dest`.

    For putting on a USB stick, or for helping someone on Windows from a Linux
    machine. Nothing is extracted; they get the official zip as published.
    """
    os.makedirs(dest, exist_ok=True)
    out = []
    for plat in PLATFORMS:
        cb = (lambda d, t, _p=plat: progress(_p, d, t)) if progress else None
        out.append(download(plat, dest_dir=dest, progress=cb))
    with open(os.path.join(dest, "README.txt"), "w", encoding="utf-8") as f:
        f.write(
            "Android platform-tools, downloaded from Google.\n\n"
            + "".join(f"  {name}\n    {BASE}/{name}\n" for name in PLATFORMS.values())
            + "\nUnzip the one for your computer. The 'adb' program is inside.\n"
              "Then either put that folder on your PATH, or point Free My\n"
              "Android at it:\n\n"
              "    FMA_ADB=/path/to/platform-tools/adb ./freemyandroid\n\n"
              "On Windows, prefer wireless debugging: it needs no USB driver.\n"
              "See docs/ADB.md.\n")
    return out


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
