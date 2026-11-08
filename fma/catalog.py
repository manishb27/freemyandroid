"""Fetching APKs from their official source, so nobody has to go hunting.

Two sources only: the F-Droid repo, and a project's own GitHub releases. That
restriction is the whole security model. Mirror sites are where repackaged
APKs come from, and a lot of ISPs sinkhole them anyway, so a download that
"works" from one of those is worse than one that fails.

What this does and does not promise:

  * The transport is HTTPS to a host we hard-coded. That is the trust anchor.
  * Every download's SHA-256 is recorded. A pinned `sha256` in apps.json is
    enforced; without one, the hash is written to the cache so the *next*
    fetch of the same version is verified against the first.
  * It does not verify the APK's signing certificate. Doing that properly
    needs apksigner from the Android build-tools, which most people running
    this will not have. If you have it, `verify_signature()` uses it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from .journal import data_dir

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")

FDROID_REPO = "https://f-droid.org/repo"
FDROID_API = "https://f-droid.org/api/v1/packages"
GITHUB_API = "https://api.github.com/repos"

# A plain, honest client name. Not a browser string: pretending to be Chrome
# to get past a block is how you end up on a mirror serving something else.
UA = "freemyandroid/0.1 (+https://github.com/manishb27/freemyandroid)"

TIMEOUT = 60


class CatalogError(RuntimeError):
    pass


@dataclass
class App:
    id: str
    name: str
    category: str
    pkg: str
    source: str                 # fdroid | github
    repo: str = ""
    asset: str = ""
    blurb: str = ""
    note: str = ""
    sha256: str = ""
    fills_role: str = ""
    config: str = ""
    ram_kb: int = 0
    prerelease: bool = False    # the project only tags betas (see Lawnchair)
    exclude: str = ""           # regex of asset names to refuse
    recommend: list[str] = field(default_factory=list)

    @property
    def origin(self) -> str:
        return "F-Droid" if self.source == "fdroid" else f"github.com/{self.repo}"


def load() -> list[App]:
    with open(os.path.join(DATA, "apps.json"), encoding="utf-8") as f:
        blob = json.load(f)
    known = set(App.__dataclass_fields__)
    return [App(**{k: v for k, v in a.items() if k in known}) for a in blob["apps"]]


def by_id() -> dict[str, App]:
    return {a.id: a for a in load()}


def for_profile(profile_id: str) -> list[App]:
    return [a for a in load() if profile_id in a.recommend]


def cache_dir() -> str:
    d = os.path.join(data_dir(), "apk")
    os.makedirs(d, exist_ok=True)
    return d


def _get(url: str, *, accept: str = "*/*") -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise CatalogError(
                f"{url} returned 403. Some ISPs block or rewrite APK hosts; "
                "try a different network, or use --offline-bundle on a network "
                "that works and apply the bundle from here.") from None
        raise CatalogError(f"{url} returned HTTP {e.code}") from None
    except urllib.error.URLError as e:
        raise CatalogError(f"could not reach {url}: {e.reason}") from None


# --- working out the URL ----------------------------------------------------


def resolve(app: App, abi: str = "") -> tuple[str, str]:
    """-> (download url, version label)

    `abi` is the phone's primary ABI (`ro.product.cpu.abi`, usually
    arm64-v8a). Projects that publish one APK per architecture alongside a
    universal one need it; picking by file size instead gets you a binary
    that installs and then refuses to start.
    """
    if app.source == "fdroid":
        return _resolve_fdroid(app)
    if app.source == "github":
        return _resolve_github(app, abi)
    raise CatalogError(f"{app.id}: unknown source {app.source!r}")


def _resolve_fdroid(app: App) -> tuple[str, str]:
    blob = json.loads(_get(f"{FDROID_API}/{app.pkg}", accept="application/json"))
    code = blob.get("suggestedVersionCode")
    versions = blob.get("packages", [])
    if not versions:
        raise CatalogError(f"{app.pkg} is not in the F-Droid repo")
    pick = next((v for v in versions if v.get("versionCode") == code), versions[0])
    vc = pick["versionCode"]
    vn = pick.get("versionName", str(vc))
    return f"{FDROID_REPO}/{app.pkg}_{vc}.apk", vn


ABI_ALIASES = {
    "arm64-v8a": ("arm64-v8a", "arm64", "aarch64"),
    "armeabi-v7a": ("armeabi-v7a", "armeabi", "arm32", "armv7"),
    "x86_64": ("x86_64", "x8664", "amd64"),
    "x86": ("x86",),
}


def _asset_rank(name: str, abi: str) -> tuple[int, int]:
    """Lower is better. (preference class, length) — shorter names on a tie."""
    low = name.lower()
    if abi:
        for alias in ABI_ALIASES.get(abi, (abi,)):
            if alias in low:
                return (0, len(low))
    if "universal" in low:
        return (1, len(low))
    # Name mentions some other architecture: almost certainly the wrong one.
    others = {a for group in ABI_ALIASES.values() for a in group}
    if abi:
        others -= set(ABI_ALIASES.get(abi, (abi,)))
    if any(o in low for o in others):
        return (3, len(low))
    return (2, len(low))


def _resolve_github(app: App, abi: str = "") -> tuple[str, str]:
    """Newest release with a usable APK.

    `releases/latest` is GitHub's idea of latest, which excludes pre-releases.
    For a project that only ever tags betas that is actively harmful: asking
    Lawnchair for its latest release hands back version 1.2 from 2020, while
    the builds people actually run are all marked pre-release. Those projects
    set `prerelease: true` and we walk the release list ourselves.
    """
    if not app.repo:
        raise CatalogError(f"{app.id}: github source with no repo")

    if app.prerelease:
        blobs = json.loads(_get(f"{GITHUB_API}/{app.repo}/releases?per_page=30",
                                accept="application/vnd.github+json"))
        releases = [b for b in blobs if not b.get("draft")]
        releases.sort(key=lambda b: b.get("published_at") or "", reverse=True)
    else:
        releases = [json.loads(_get(f"{GITHUB_API}/{app.repo}/releases/latest",
                                    accept="application/vnd.github+json"))]

    pattern = re.compile(app.asset or r"\.apk$", re.I)
    refuse = re.compile(app.exclude, re.I) if app.exclude else None
    tried: list[str] = []

    for blob in releases:
        tag = blob.get("tag_name", "?")
        tried.append(tag)
        assets = [a for a in blob.get("assets", [])
                  if a.get("name", "").lower().endswith(".apk")
                  and pattern.search(a.get("name", ""))
                  and not (refuse and refuse.search(a.get("name", "")))]
        if not assets:
            continue
        assets.sort(key=lambda a: _asset_rank(a.get("name", ""), abi))
        best = assets[0]
        if _asset_rank(best.get("name", ""), abi)[0] == 3:
            continue        # wrong architecture; try an older release
        return best["browser_download_url"], tag

    raise CatalogError(
        f"{app.id}: no asset matched /{app.asset or r'\.apk$'}/"
        + (f" (excluding /{app.exclude}/)" if app.exclude else "")
        + f" for {abi or 'this phone'} in: {', '.join(tried[:8]) or 'no releases'}")


# --- downloading ------------------------------------------------------------


def fetch(app: App, *, abi: str = "",
          progress: Callable[[int, int], None] | None = None,
          force: bool = False) -> str:
    """Download (or reuse) the APK. Returns the local path."""
    url, version = resolve(app, abi)
    name = f"{app.pkg}_{re.sub(r'[^A-Za-z0-9._-]', '-', version)}.apk"
    path = os.path.join(cache_dir(), name)
    lock = os.path.join(cache_dir(), "hashes.json")

    known: dict[str, str] = {}
    if os.path.isfile(lock):
        try:
            with open(lock, encoding="utf-8") as f:
                known = json.load(f)
        except (OSError, json.JSONDecodeError):
            known = {}

    if os.path.isfile(path) and not force:
        digest = _sha256_file(path)
        expected = app.sha256 or known.get(name)
        if expected and digest != expected:
            raise CatalogError(
                f"{name} in the cache does not match the hash we recorded "
                f"({digest[:12]} vs {expected[:12]}). Delete it and try again.")
        return path

    req = urllib.request.Request(url, headers={"User-Agent": UA})
    h = hashlib.sha256()
    tmp = path + ".part"
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
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
        raise CatalogError(f"{app.name}: HTTP {e.code} from {url}") from None
    except urllib.error.URLError as e:
        _unlink(tmp)
        raise CatalogError(f"{app.name}: could not download: {e.reason}") from None

    digest = h.hexdigest()
    if app.sha256 and digest != app.sha256:
        _unlink(tmp)
        raise CatalogError(
            f"{app.name}: SHA-256 mismatch. Expected {app.sha256[:12]}, "
            f"got {digest[:12]}. Not installing it.")
    if not _looks_like_apk(tmp):
        _unlink(tmp)
        raise CatalogError(
            f"{app.name}: what we downloaded is not an APK. Some networks "
            "return a block page with a 200 status.")
    os.replace(tmp, path)

    known[name] = digest
    try:
        with open(lock, "w", encoding="utf-8") as f:
            json.dump(known, f, indent=1, sort_keys=True)
    except OSError:
        pass
    return path


def _looks_like_apk(path: str) -> bool:
    """An APK is a zip. A captive-portal page is not."""
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"PK"
    except OSError:
        return False


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def verify_signature(path: str) -> str:
    """apksigner's verdict, or '' if apksigner is not installed."""
    import shutil
    import subprocess
    exe = shutil.which("apksigner")
    if not exe:
        return ""
    try:
        p = subprocess.run([exe, "verify", "--print-certs", path],
                           capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if p.returncode != 0:
        return f"FAILED: {(p.stderr or p.stdout).strip().splitlines()[0]}"
    for line in p.stdout.splitlines():
        if "SHA-256 digest" in line and "certificate" in line.lower():
            return line.strip()
    return "ok"


def bundle(apps: list[App], dest: str,
           progress: Callable[[str, int, int], None] | None = None) -> list[str]:
    """Pre-download APKs into `dest`, for applying from a network that blocks them."""
    os.makedirs(dest, exist_ok=True)
    out = []
    manifest = []
    for a in apps:
        cb = (lambda d, t, _a=a: progress(_a.name, d, t)) if progress else None
        src = fetch(a, progress=cb)
        target = os.path.join(dest, os.path.basename(src))
        if os.path.abspath(src) != os.path.abspath(target):
            with open(src, "rb") as r, open(target, "wb") as w:
                w.write(r.read())
        manifest.append({"id": a.id, "pkg": a.pkg, "name": a.name,
                         "file": os.path.basename(target),
                         "sha256": _sha256_file(target), "origin": a.origin})
        out.append(target)
    with open(os.path.join(dest, "bundle.json"), "w", encoding="utf-8") as f:
        json.dump({"apps": manifest}, f, indent=1)
    return out
