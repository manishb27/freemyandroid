"""What kind of phone is this, and what is on it.

One `Profile` object is built once per session and handed to everything else,
so we pay for the slow `pm list packages` and `dumpsys` calls a single time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import adb

# Maps the many names a vendor uses for itself onto one key we use in the
# package lists. `ro.product.manufacturer` is wildly inconsistent.
VENDORS = {
    "samsung": "samsung",
    "xiaomi": "xiaomi", "redmi": "xiaomi", "poco": "xiaomi",
    "oneplus": "oneplus",
    "oppo": "oppo",
    "vivo": "vivo", "iqoo": "vivo",
    "realme": "realme",
    "motorola": "motorola", "lenovo": "motorola",
    "google": "google",
    "nothing": "nothing",
    "huawei": "huawei", "honor": "honor",
    "asus": "asus",
    "sony": "sony",
    "tecno": "transsion", "infinix": "transsion", "itel": "transsion",
    "nokia": "nokia", "hmd": "nokia",
}


@dataclass
class Profile:
    serial: str
    manufacturer: str = ""
    brand: str = ""
    model: str = ""
    device: str = ""
    android: str = ""
    sdk: int = 0
    build: str = ""
    abi: str = ""
    vendor_key: str = "generic"
    is_tv: bool = False
    is_watch: bool = False
    installed: set[str] = field(default_factory=set, repr=False)
    disabled: set[str] = field(default_factory=set, repr=False)
    avf: str = "unknown"      # supported | unsupported | unknown
    total_ram_mb: int = 0

    # -- derived

    @property
    def name(self) -> str:
        m = (self.model or self.device or self.serial).replace("_", " ")
        brand = (self.brand or self.manufacturer).strip()
        if brand and brand.lower() not in m.lower():
            return f"{brand.title()} {m}"
        return m

    @property
    def android_label(self) -> str:
        return f"Android {self.android}" + (f" (API {self.sdk})" if self.sdk else "")

    @property
    def enabled(self) -> set[str]:
        return self.installed - self.disabled

    @property
    def supports_wireless_debug(self) -> bool:
        return self.sdk >= 30

    @property
    def has_phantom_killer(self) -> bool:
        """Android 12 added the killer that quietly murders Termux's children."""
        return self.sdk >= 31

    @property
    def warnings(self) -> list[str]:
        w: list[str] = []
        if self.is_tv:
            w.append(
                "This is an Android TV, not a phone. Free My Android's profiles "
                "assume a phone; for a TV use Free My TV instead.")
        if self.is_watch:
            w.append("This is a watch. Nothing here has been tested on one.")
        if self.sdk and self.sdk < 26:
            w.append(
                f"Android {self.android} is older than anything this has been "
                "tested on. 'pm disable-user' may behave differently.")
        return w


def _int(s: str, default: int = 0) -> int:
    try:
        return int(str(s).strip())
    except (TypeError, ValueError):
        return default


def vendor_key(manufacturer: str, brand: str) -> str:
    for hay in (manufacturer, brand):
        low = (hay or "").lower()
        for needle, key in VENDORS.items():
            if needle in low:
                return key
    return "generic"


def read(serial: str, *, deep: bool = True) -> Profile:
    """Build the profile. `deep=False` skips the slow calls, for a quick list."""
    d = adb.Device(serial)
    p = d.props(
        "ro.product.manufacturer",
        "ro.product.brand",
        "ro.product.model",
        "ro.product.device",
        "ro.build.version.release",
        "ro.build.version.sdk",
        "ro.build.display.id",
        "ro.build.characteristics",
        "ro.product.cpu.abi",
    )
    prof = Profile(
        serial=serial,
        manufacturer=p["ro.product.manufacturer"],
        brand=p["ro.product.brand"],
        model=p["ro.product.model"],
        device=p["ro.product.device"],
        android=p["ro.build.version.release"],
        sdk=_int(p["ro.build.version.sdk"]),
        build=p["ro.build.display.id"],
        abi=p["ro.product.cpu.abi"],
    )
    prof.vendor_key = vendor_key(prof.manufacturer, prof.brand)

    chars = p["ro.build.characteristics"].lower()
    prof.is_tv = "tv" in chars.split(",") or bool(
        d.shell("pm list features | grep -c feature:android.software.leanback",
                timeout=20).out.strip() not in ("", "0"))
    prof.is_watch = "watch" in chars.split(",")

    if not deep:
        return prof

    prof.installed = d.list_packages()
    prof.disabled = d.list_packages(only_disabled=True)
    prof.avf = _avf_support(d)
    prof.total_ram_mb = _ram_mb(d)
    return prof


def _ram_mb(d: adb.Device) -> int:
    r = d.shell("cat /proc/meminfo | grep -m1 MemTotal", timeout=20)
    m = re.search(r"(\d+)", r.out)
    return _int(m.group(1)) // 1024 if m else 0


def _avf_support(d: adb.Device) -> str:
    """Can this phone run the Android 16 'Linux development environment' VM?

    It needs the Android Virtualization Framework with *protected* VM support.
    Plenty of Android 16 phones report the framework and still refuse, because
    the OEM left pKVM out, so treat a positive here as 'probably'.
    """
    if _int(d.prop("ro.build.version.sdk")) < 35:
        return "unsupported"
    hyp = d.prop("ro.boot.hypervisor.vm.supported")
    prot = d.prop("ro.boot.hypervisor.protected_vm.supported")
    if prot == "1":
        return "supported"
    if hyp == "1":
        return "unprotected-only"
    feats = d.shell("pm list features | grep -i virtualization", timeout=20).out
    if "virtualization_framework" in feats:
        return "unprotected-only"
    return "unsupported"


# --- The home screen --------------------------------------------------------


def current_launcher(d: adb.Device) -> str:
    """Package name of whatever is Home right now, or ''.

    The output is a component, not a package: `ComponentInfo{io.example/
    io.example.HomeActivity}`. Taking the part before the slash matters,
    because this string is what stops us disabling the live home screen, and
    a package name that never matches is a guard that never fires.
    """
    r = d.shell(
        "cmd shortcut get-default-launcher 2>/dev/null "
        "|| dumpsys package preferred-activities | grep -m1 -A3 android.intent.category.HOME",
        timeout=25)
    text = r.out
    m = re.search(r"([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)/", text)
    if m:
        return m.group(1)
    m = re.search(r"([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+){2,})", text)
    return m.group(1) if m else ""


def launchable_packages(d: adb.Device) -> set[str]:
    """Packages with an icon in the app drawer.

    Elder mode works from this set rather than from the full package list: the
    goal there is to reduce what someone can see and tap, and a package with
    no icon was never part of that problem.
    """
    r = d.shell(
        "cmd package query-activities --brief "
        "-a android.intent.action.MAIN -c android.intent.category.LAUNCHER 2>/dev/null",
        timeout=45)
    found = {
        m.group(1)
        for line in r.out.splitlines()
        if (m := re.search(r"([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)/", line))
    }
    if found:
        return found
    # Android 8/9 have no `query-activities`. Fall back to the slower resolve.
    r = d.shell(
        "for p in $(pm list packages -e | sed 's/package://'); do "
        "pm resolve-activity --brief -a android.intent.action.MAIN "
        "-c android.intent.category.LAUNCHER $p 2>/dev/null | tail -1; done",
        timeout=180)
    return {
        m.group(1)
        for line in r.out.splitlines()
        if (m := re.search(r"^([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)/", line.strip()))
    }


def installed_launchers(d: adb.Device) -> list[str]:
    """Every package that offers a home screen. Never disable the only one left."""
    r = d.shell(
        "cmd package query-activities --brief "
        "-a android.intent.action.MAIN -c android.intent.category.HOME 2>/dev/null",
        timeout=30)
    found = set()
    for line in r.out.splitlines():
        m = re.search(r"([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)/", line)
        if m:
            found.add(m.group(1))
    if not found:
        # Older builds: fall back to resolving the intent one activity at a time.
        r = d.shell("pm list packages -e | sed 's/package://'", timeout=60)
        for pkg in r.out.split():
            if "launcher" in pkg.lower() or "home" in pkg.lower():
                found.add(pkg)
    return sorted(found)
