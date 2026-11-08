"""Talking to a phone over ADB.

Everything that touches the device goes through here, so there is exactly one
place that knows how to run a command, and exactly one place that decides what
is allowed to be sent. Package names are validated before they are ever
interpolated into a shell string (`_pkg`), because a package name arriving from
a JSON list is still input we did not write.

No third-party modules. `adb` itself is the only external dependency.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field

# A package name Android would actually accept. Deliberately strict: this is
# the guard that lets us build shell strings without worrying about injection.
PKG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")

# Settings keys and values we are willing to write.
KEY_RE = re.compile(r"^[A-Za-z0-9_.]+$")
VAL_RE = re.compile(r"^[A-Za-z0-9_.:,/+-]*$")


class AdbError(RuntimeError):
    """adb could not be run, or refused to do what we asked."""


class DeviceGone(AdbError):
    """The phone went away mid-operation (cable pulled, Wi-Fi dropped, reboot)."""


def _pkg(name: str) -> str:
    if not PKG_RE.match(name or ""):
        raise AdbError(f"not a package name: {name!r}")
    return name


def find_adb() -> str:
    """The adb binary, or a clear explanation of how to get one."""
    env = os.environ.get("FMA_ADB")
    if env:
        if os.path.isfile(env) and os.access(env, os.X_OK):
            return env
        raise AdbError(f"FMA_ADB points at {env!r}, which is not an executable file")
    found = shutil.which("adb")
    if found:
        return found
    for guess in (
        os.path.expanduser("~/Android/Sdk/platform-tools/adb"),
        os.path.expanduser("~/Library/Android/sdk/platform-tools/adb"),
        "/usr/lib/android-sdk/platform-tools/adb",
        "/opt/android-sdk/platform-tools/adb",
    ):
        if os.access(guess, os.X_OK):
            return guess
    raise AdbError(
        "adb not found. Install the Android platform tools:\n"
        "  Debian/Ubuntu  sudo apt install adb\n"
        "  Fedora         sudo dnf install android-tools\n"
        "  Arch           sudo pacman -S android-tools\n"
        "  macOS          brew install android-platform-tools\n"
        "Or set FMA_ADB to the full path of an adb binary."
    )


@dataclass
class Result:
    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def text(self) -> str:
        return self.out.strip() or self.err.strip()


def _run(args: list[str], timeout: float = 30.0) -> Result:
    try:
        p = subprocess.run(
            args,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise AdbError(f"timed out after {timeout:g}s: {' '.join(args[1:])}") from None
    except OSError as e:
        raise AdbError(f"could not run adb: {e}") from None
    return Result(p.returncode, p.stdout, p.stderr)


# --- The server and the list of devices ------------------------------------


def start_server() -> None:
    _run([find_adb(), "start-server"], timeout=20)


@dataclass
class Listed:
    serial: str
    state: str          # device | unauthorized | offline | ...
    model: str = ""
    product: str = ""

    @property
    def wireless(self) -> bool:
        return ":" in self.serial and not self.serial.startswith("emulator-")

    @property
    def usable(self) -> bool:
        return self.state == "device"

    @property
    def label(self) -> str:
        name = (self.model or self.product or self.serial).replace("_", " ")
        how = "Wi-Fi" if self.wireless else "USB"
        return f"{name} ({how})"


def devices() -> list[Listed]:
    r = _run([find_adb(), "devices", "-l"], timeout=20)
    out: list[Listed] = []
    for line in r.out.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        bits = line.split()
        serial, state = bits[0], bits[1]
        kv = dict(b.split(":", 1) for b in bits[2:] if ":" in b)
        out.append(Listed(serial, state, kv.get("model", ""), kv.get("product", "")))
    return out


def connect(host_port: str) -> str:
    """`adb connect`. Returns adb's own message, which is the useful part."""
    r = _run([find_adb(), "connect", host_port], timeout=25)
    return r.text


def pair(host_port: str, code: str) -> str:
    """Android 11+ wireless pairing. The code is the 6 digits the phone shows."""
    if not re.match(r"^\d{6}$", code or ""):
        raise AdbError("the pairing code is 6 digits")
    r = _run([find_adb(), "pair", host_port, code], timeout=40)
    return r.text


def disconnect(host_port: str) -> str:
    return _run([find_adb(), "disconnect", host_port], timeout=15).text


# --- One phone --------------------------------------------------------------


@dataclass
class Device:
    serial: str
    _props: dict[str, str] = field(default_factory=dict, repr=False)

    # -- raw access

    def adb(self, *args: str, timeout: float = 30.0) -> Result:
        r = _run([find_adb(), "-s", self.serial, *args], timeout=timeout)
        low = (r.err or "").lower()
        if "device '" in low and "not found" in low:
            raise DeviceGone(f"{self.serial} is no longer connected")
        if "device offline" in low:
            raise DeviceGone(f"{self.serial} went offline")
        return r

    def shell(self, cmd: str, timeout: float = 30.0) -> Result:
        return self.adb("shell", cmd, timeout=timeout)

    # -- identity

    def prop(self, key: str, default: str = "") -> str:
        if key not in self._props:
            if not KEY_RE.match(key):
                raise AdbError(f"not a property name: {key!r}")
            self._props[key] = self.shell(f"getprop {key}", timeout=15).out.strip()
        return self._props[key] or default

    def props(self, *keys: str) -> dict[str, str]:
        return {k: self.prop(k) for k in keys}

    # -- packages

    def list_packages(self, *, only_enabled: bool = False,
                      only_disabled: bool = False,
                      system_only: bool = False) -> set[str]:
        flags = ""
        if only_enabled:
            flags += " -e"
        if only_disabled:
            flags += " -d"
        if system_only:
            flags += " -s"
        r = self.shell(f"pm list packages{flags}", timeout=60)
        if not r.ok and not r.out:
            raise AdbError(f"pm list packages failed: {r.text}")
        return {
            line.strip()[len("package:"):]
            for line in r.out.splitlines()
            if line.strip().startswith("package:")
        }

    def label_of(self, pkg: str) -> str:
        """Best-effort human name for a package. Costs one call, so cache upstream."""
        r = self.shell(f"dumpsys package {_pkg(pkg)} | grep -m1 -i 'applicationInfo\\|labelRes'",
                       timeout=20)
        return r.out.strip()

    # -- the two operations that change things, and their exact inverses

    def disable(self, pkg: str) -> Result:
        """`pm disable-user` keeps the APK on disk; `enable` brings it straight back."""
        return self.shell(f"pm disable-user --user 0 {_pkg(pkg)}", timeout=45)

    def enable(self, pkg: str) -> Result:
        return self.shell(f"pm enable --user 0 {_pkg(pkg)}", timeout=45)

    def is_installed(self, pkg: str) -> bool:
        r = self.shell(f"pm path {_pkg(pkg)}", timeout=20)
        return r.ok and "package:" in r.out

    def is_enabled(self, pkg: str) -> bool:
        """State 0 is 'default', 1 is 'enabled'; 2, 3 and 4 are the off states."""
        r = self.shell(f"dumpsys package {_pkg(pkg)} | grep -m1 enabled=", timeout=20)
        m = re.search(r"enabled=(\d+)", r.out)
        if m:
            return m.group(1) in ("0", "1")
        return pkg in self.list_packages(only_enabled=True)

    # -- settings, with the old value read first so undo is exact

    def get_setting(self, ns: str, key: str) -> str | None:
        if ns not in ("system", "secure", "global"):
            raise AdbError(f"not a settings namespace: {ns!r}")
        if not KEY_RE.match(key):
            raise AdbError(f"not a settings key: {key!r}")
        r = self.shell(f"settings get {ns} {key}", timeout=20)
        v = r.out.strip()
        return None if v in ("", "null") else v

    def put_setting(self, ns: str, key: str, value: str) -> Result:
        if ns not in ("system", "secure", "global"):
            raise AdbError(f"not a settings namespace: {ns!r}")
        if not KEY_RE.match(key):
            raise AdbError(f"not a settings key: {key!r}")
        if not VAL_RE.match(str(value)):
            raise AdbError(f"refusing to write this value: {value!r}")
        return self.shell(f"settings put {ns} {key} {value}", timeout=20)

    def delete_setting(self, ns: str, key: str) -> Result:
        if ns not in ("system", "secure", "global"):
            raise AdbError(f"not a settings namespace: {ns!r}")
        if not KEY_RE.match(key):
            raise AdbError(f"not a settings key: {key!r}")
        return self.shell(f"settings delete {ns} {key}", timeout=20)

    # -- battery optimisation exemptions (the doze whitelist)

    def doze_whitelist(self) -> set[str]:
        r = self.shell("cmd deviceidle whitelist 2>/dev/null "
                       "|| dumpsys deviceidle whitelist", timeout=25)
        out = set()
        for line in r.out.splitlines():
            # Lines look like "system,com.android.vending,10123" or just the package.
            for field in line.replace(":", ",").split(","):
                field = field.strip()
                if PKG_RE.match(field):
                    out.add(field)
        return out

    def doze_allow(self, pkg: str) -> Result:
        return self.shell(f"cmd deviceidle whitelist +{_pkg(pkg)}", timeout=25)

    def doze_deny(self, pkg: str) -> Result:
        return self.shell(f"cmd deviceidle whitelist -{_pkg(pkg)}", timeout=25)

    # -- installing

    def install(self, apk_path: str, timeout: float = 900.0) -> Result:
        # 900s, not 300. A 17 MB APK on a mid-range phone with slow flash can
        # genuinely take several minutes to verify and optimise, and a timeout
        # here looks exactly like a hang to whoever is watching.
        if not os.path.isfile(apk_path):
            raise AdbError(f"no such APK: {apk_path}")
        # -r replace, -g grant the runtime permissions it declares (launchers
        # need them to read the app list on some OEM builds).
        return self.adb("install", "-r", "-g", apk_path, timeout=timeout)

    def set_home(self, pkg: str) -> Result:
        """Nudge the home-screen chooser. Android will still ask the user to confirm."""
        return self.shell(
            "cmd package set-home-activity "
            f"{_pkg(pkg)} 2>/dev/null || true", timeout=25)

    # -- waiting

    def wait(self, timeout: float = 60.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for d in devices():
                if d.serial == self.serial and d.usable:
                    return True
            time.sleep(1.0)
        return False
