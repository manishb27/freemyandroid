"""What is actually draining this particular phone.

A curated package list can only tell you what a thing is. It cannot tell you
that on *your* phone a weather widget has held a wake lock for four hours. So
this reads the device's own accounting and ranks what it finds.

Everything here is best-effort parsing of `dumpsys` output, which is not a
stable interface and differs between OEMs and Android versions. Each reader
returns nothing rather than guessing when it does not recognise the format,
and `report()` says which readers produced data, so a thin report is visibly
thin instead of quietly wrong.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import adb


@dataclass
class Hog:
    pkg: str
    label: str = ""
    mah: float = 0.0            # estimated power use since last charge
    wake_ms: int = 0            # partial wake lock time held
    wakeups: int = 0            # alarm wakeups (woke the device from sleep)
    alarms: int = 0             # alarms set, waking or not
    alarm_ms: int = 0           # time spent running alarm handlers
    jobs: int = 0               # scheduled jobs registered
    dozed: bool = False         # exempt from battery optimisation
    known: str = ""             # risk from the curated lists, if listed
    tags: list[str] = field(default_factory=list)
    shared_uid: int = 0         # other packages billed to the same uid

    @property
    def wake_min(self) -> float:
        return self.wake_ms / 60000.0

    @property
    def score(self) -> float:
        """A single number to sort by. Not a unit of anything.

        Weighted so that holding a wake lock counts for more than raw mAh:
        mAh is dominated by the screen and by whatever you actually used,
        while a wake lock on a background service is nearly always a bug or an
        ad SDK. Being doze-exempt multiplies the rest, because it is what lets
        the others keep happening overnight.
        """
        s = (self.mah * 1.0
             + self.wake_min * 2.0
             + self.wakeups * 0.5          # woke the CPU: the expensive kind
             + self.alarms * 0.02          # merely scheduled: cheap, but telling
             + self.alarm_ms / 60000.0     # time actually spent running
             + self.jobs * 0.2)
        return s * (1.5 if self.dozed else 1.0)

    @property
    def why(self) -> str:
        bits = []
        if self.mah >= 1:
            bits.append(f"{self.mah:.0f} mAh")
        if self.wake_min >= 1:
            bits.append(f"awake {self.wake_min:.0f} min")
        if self.wakeups:
            bits.append(f"{self.wakeups} wakeups")
        if self.alarms:
            bits.append(f"{self.alarms} alarms")
        if self.alarm_ms >= 60000:
            bits.append(f"{self.alarm_ms / 60000:.0f} min in alarms")
        if self.jobs:
            bits.append(f"{self.jobs} jobs")
        if self.dozed:
            bits.append("exempt from doze")
        if self.shared_uid:
            bits.append(f"uid shared with {self.shared_uid} other"
                        f"{'s' if self.shared_uid != 1 else ''}, so the power "
                        "is split evenly rather than measured")
        return ", ".join(bits) or "no measurable activity"


@dataclass
class Report:
    hogs: list[Hog] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    since: str = ""

    @property
    def actionable(self) -> list[Hog]:
        """Hogs that a curated list already considers safe to turn off."""
        return [h for h in self.hogs if h.known == "safe"]

    @property
    def thin(self) -> bool:
        return len(self.sources) <= 1


def uid_map(d: adb.Device) -> dict[str, list[str]]:
    """uid -> packages. Several packages can share a uid, and batterystats
    reports per uid, so a shared uid means we cannot attribute the drain to
    one of them; we list them all and say so."""
    r = d.shell("pm list packages -U", timeout=60)
    out: dict[str, list[str]] = {}
    for line in r.out.splitlines():
        m = re.match(r"package:(\S+)\s+uid:(\d+)", line.strip())
        if m:
            out.setdefault(m.group(2), []).append(m.group(1))
    return out


def _batterystats(d: adb.Device) -> tuple[dict[str, float], dict[str, int], str]:
    """-> (mAh by uid, wake-lock ms by package, 'since' description)"""
    r = d.shell("dumpsys batterystats --charged", timeout=120)
    text = r.out
    if not text.strip():
        return {}, {}, ""

    since = ""
    m = re.search(r"Statistics since last charge:.*?\n\s*(?:Time on battery: )?([^\n]*)",
                  text)
    if m:
        since = m.group(1).strip()[:80]

    mah: dict[str, float] = {}
    # The section is headed "Estimated power use (mAh):" and the per-app lines
    # differ by Android version. Android 12 writes, with a capital UID and a
    # breakdown in brackets:
    #     UID u0a119: 2.14 ( cpu=2.09 audio=0.00705 wakelock=0.0363 )
    #     UID 1000: 4.90 ( cpu=4.89 sensors=0.0101 )
    # Older releases write "Uid u0a234: 12.3" with no brackets. Matching both,
    # case-insensitively, because getting the case wrong here silently drops
    # every power figure and leaves a report that looks like it worked.
    block = re.search(r"Estimated power use \(mAh\):(.*?)(?:\n\s{0,2}\S|\Z)",
                      text, re.S)
    if block:
        for line in block.group(1).splitlines():
            m = re.match(r"\s*UID\s+(u\d+a\d+|\d+):\s*([\d.]+)", line, re.I)
            if m:
                mah[_normalise_uid(m.group(1))] = float(m.group(2))

    wake: dict[str, int] = {}
    # "All partial wake locks:" then "  Wake lock <uid> <name>: NNm NNs ... partial"
    block = re.search(r"All partial wake locks:(.*?)(?:\n\s*\n|\Z)", text, re.S)
    if block:
        for line in block.group(1).splitlines():
            m = re.search(r"Wake lock\s+\S+\s+(\S+).*?realtime", line)
            dur = _duration_ms(line)
            if m and dur:
                name = m.group(1).strip(":")
                pkg = name if adb.PKG_RE.match(name) else ""
                if pkg:
                    wake[pkg] = wake.get(pkg, 0) + dur
    return mah, wake, since


def _normalise_uid(raw: str) -> str:
    """'u0a119' means Android user 0, app 119, which is uid 10119.

    Bare numbers (1000, 1010, 0) are system uids and are already real uids.
    """
    m = re.match(r"^u(\d+)a(\d+)$", raw, re.I)
    if m:
        return str(int(m.group(1)) * 100000 + 10000 + int(m.group(2)))
    return raw.lstrip("uU")


def _duration_ms(text: str) -> int:
    total = 0
    for value, unit in re.findall(r"(\d+)(ms|s|m|h|d)\b", text):
        n = int(value)
        total += n * {"ms": 1, "s": 1000, "m": 60000,
                      "h": 3600000, "d": 86400000}[unit]
    return total


def _alarms(d: adb.Device) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    """-> (wakeups, alarm count, running ms) per package.

    The lines in the alarm stats put the package *last*, after the counts:

        +2m15s788ms running, 0 wakeups, 61 alarms: 1000:android
        +1m39s28ms running, 0 wakeups, 23 alarms: u0a5:com.google.android.gms

    The uid prefix before the colon is either a bare uid or the `u0a5` form,
    and is ignored here because the package name is right there.
    """
    r = d.shell("dumpsys alarm", timeout=90)
    wakeups: dict[str, int] = {}
    counts: dict[str, int] = {}
    running: dict[str, int] = {}
    pattern = re.compile(
        r"^\s*(\+\S+)\s+running,\s*(\d+)\s+wakeups,\s*(\d+)\s+alarms:\s*"
        r"[^:]+:([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)\s*$", re.M)
    for m in pattern.finditer(r.out):
        dur, wake, count, pkg = m.groups()
        wakeups[pkg] = wakeups.get(pkg, 0) + int(wake)
        counts[pkg] = counts.get(pkg, 0) + int(count)
        running[pkg] = running.get(pkg, 0) + _duration_ms(dur)
    return wakeups, counts, running


def _jobs(d: adb.Device) -> dict[str, int]:
    """Scheduled jobs currently registered, per package.

    Lines look like:

        JOB #1000/1: 50d1dc6 com.android.providers.settings/.WriteJobService

    The number after # is a plain uid, not the `u0a12` form used elsewhere.
    `dumpsys jobscheduler` is long enough that piping it through `head` on the
    device breaks the pipe, so it is read whole and filtered here.
    """
    r = d.shell("dumpsys jobscheduler", timeout=120)
    out: dict[str, int] = {}
    for m in re.finditer(
            r"^\s*JOB #\S+:\s+\S+\s+([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)/",
            r.out, re.M):
        pkg = m.group(1)
        out[pkg] = out.get(pkg, 0) + 1
    return out


def report(d: adb.Device, entries: dict | None = None, *,
           limit: int = 25) -> Report:
    """Build the ranked list. `entries` is profiles.Context.entries, used to
    mark which of the offenders a curated list already calls safe to remove."""
    rep = Report()
    hogs: dict[str, Hog] = {}

    def hog(pkg: str) -> Hog:
        if pkg not in hogs:
            e = (entries or {}).get(pkg)
            hogs[pkg] = Hog(
                pkg=pkg,
                label=getattr(e, "label", "") or pkg,
                known=getattr(e, "risk", "") or "",
                tags=list(getattr(e, "tags", []) or []),
            )
        return hogs[pkg]

    try:
        uids = uid_map(d)
        rep.sources.append("package uids")
    except adb.AdbError:
        uids = {}
        rep.missing.append("package uids")

    try:
        mah, wake, since = _batterystats(d)
        rep.since = since
        if mah or wake:
            rep.sources.append("batterystats")
        else:
            rep.missing.append("batterystats (no data; the phone may have "
                               "just been charged, which resets it)")
        for uid, value in mah.items():
            pkgs = uids.get(uid, [])
            if not pkgs:
                continue    # a system uid with no package of its own
            share = value / len(pkgs)
            for pkg in pkgs:
                h = hog(pkg)
                h.mah += share
                h.shared_uid = len(pkgs) - 1
        # A wake lock's *name* often looks exactly like a package name —
        # "IpReachabilityMonitor.wlan0" sails through any sane package regex.
        # Only names that are genuinely installed are attributed.
        real = {p for group in uids.values() for p in group}
        for pkg, ms in wake.items():
            if real and pkg not in real:
                continue
            hog(pkg).wake_ms += ms
    except adb.AdbError:
        rep.missing.append("batterystats")

    try:
        wakeups, counts, running = _alarms(d)
        for pkg, n in wakeups.items():
            hog(pkg).wakeups += n
        for pkg, n in counts.items():
            hog(pkg).alarms += n
        for pkg, ms in running.items():
            hog(pkg).alarm_ms += ms
        if wakeups or counts:
            rep.sources.append("alarms")
        else:
            rep.missing.append("alarms (no stats recorded)")
    except adb.AdbError:
        rep.missing.append("alarms")

    try:
        for pkg, n in _jobs(d).items():
            hog(pkg).jobs += n
        rep.sources.append("jobscheduler")
    except adb.AdbError:
        rep.missing.append("jobscheduler")

    try:
        for pkg in d.doze_whitelist():
            if pkg in hogs:
                hogs[pkg].dozed = True
        rep.sources.append("doze whitelist")
    except adb.AdbError:
        rep.missing.append("doze whitelist")

    ranked = sorted(hogs.values(), key=lambda h: h.score, reverse=True)
    rep.hogs = [h for h in ranked if h.score > 0][:limit]
    return rep
