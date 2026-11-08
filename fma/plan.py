"""Actions, and the one place that knows how to undo each of them.

Every change to a phone is an `Action`. An action is plain data, so it can be
shown in a list, written to the journal, and read back weeks later to be
reversed. Nothing here uninstalls anything: the worst we do is
`pm disable-user`, which leaves the APK on disk.

The inverse of an action is computed *at apply time*, not at plan time, because
only then do we know the value we are replacing.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

from . import adb

RISKS = ("safe", "caution", "risky")
RISK_ORDER = {r: i for i, r in enumerate(RISKS)}


@dataclass
class Action:
    kind: str                       # disable | enable | setting | install | home
    target: str = ""                # package name, or "ns/key"
    value: str = ""                 # for settings
    label: str = ""                 # what to show the user
    note: str = ""                  # why, and what breaks
    risk: str = "safe"
    source: str = ""                # which list or profile asked for this
    undo: dict[str, Any] | None = field(default=None, repr=False)
    result: str = ""                # filled in after apply: "" ok, else the error

    @property
    def ok(self) -> bool:
        return self.result == ""

    def describe(self) -> str:
        if self.kind == "disable":
            return f"Turn off  {self.label or self.target}"
        if self.kind == "enable":
            return f"Turn on   {self.label or self.target}"
        if self.kind == "setting":
            return f"Set       {self.target} = {self.value}"
        if self.kind == "install":
            return f"Install   {self.label or self.target}"
        if self.kind == "home":
            return f"Home      {self.label or self.target}"
        return f"{self.kind} {self.target}"

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Action":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


def apply(d: adb.Device, a: Action) -> Action:
    """Run one action. Records its inverse in `a.undo`. Never raises for a
    device-side refusal; that lands in `a.result` so the run can continue."""
    try:
        if a.kind == "disable":
            if not d.is_installed(a.target):
                a.result = "not installed"
                return a
            was_enabled = d.is_enabled(a.target)
            r = d.disable(a.target)
            # pm prints "new state: disabled-user" and exits 0; some OEM builds
            # exit 0 while printing an error, so check the text too.
            if not r.ok or "Error" in r.out or "Exception" in r.out:
                a.result = r.text or "pm refused"
            else:
                a.undo = {"kind": "enable", "target": a.target} if was_enabled else {}
            return a

        if a.kind == "enable":
            r = d.enable(a.target)
            if not r.ok or "Error" in r.out:
                a.result = r.text or "pm refused"
            else:
                a.undo = {"kind": "disable", "target": a.target}
            return a

        if a.kind == "setting":
            ns, _, key = a.target.partition("/")
            before = d.get_setting(ns, key)
            r = d.put_setting(ns, key, a.value)
            if not r.ok:
                a.result = r.text or "settings put refused"
            elif before is None:
                a.undo = {"kind": "unset", "target": a.target}
            else:
                a.undo = {"kind": "setting", "target": a.target, "value": before}
            return a

        if a.kind == "unset":
            ns, _, key = a.target.partition("/")
            before = d.get_setting(ns, key)
            r = d.delete_setting(ns, key)
            if not r.ok:
                a.result = r.text or "settings delete refused"
            elif before is not None:
                a.undo = {"kind": "setting", "target": a.target, "value": before}
            return a

        if a.kind == "doze_allow":
            if not d.is_installed(a.target):
                a.result = "not installed"
                return a
            was = a.target in d.doze_whitelist()
            r = d.doze_allow(a.target)
            if not r.ok:
                a.result = r.text or "deviceidle refused"
            else:
                a.undo = {} if was else {"kind": "doze_deny", "target": a.target}
            return a

        if a.kind == "doze_deny":
            r = d.doze_deny(a.target)
            if not r.ok:
                a.result = r.text or "deviceidle refused"
            else:
                a.undo = {"kind": "doze_allow", "target": a.target}
            return a

        if a.kind == "install":
            r = d.install(a.value)          # value is the local APK path
            if not r.ok or "Success" not in r.out:
                a.result = r.text or "install failed"
            else:
                # Deliberately no undo: we do not uninstall apps behind
                # someone's back. Removing it is a decision, not a rollback.
                a.undo = {}
            return a

        if a.kind == "home":
            r = d.set_home(a.target)
            a.result = "" if r.ok else (r.text or "could not set home")
            a.undo = {}
            return a

        a.result = f"unknown action: {a.kind}"
        return a

    except adb.DeviceGone:
        raise
    except adb.AdbError as e:
        a.result = str(e)
        return a


def invert(a: Action) -> Action | None:
    """The action that puts this one back, or None if there is nothing to do."""
    if not a.undo:
        return None
    u = dict(a.undo)
    return Action(
        kind=u.get("kind", ""),
        target=u.get("target", ""),
        value=u.get("value", ""),
        label=a.label,
        note=f"undo of: {a.describe()}",
        risk="safe",
        source="undo",
    )


# --- Building a plan --------------------------------------------------------


@dataclass
class Plan:
    title: str
    actions: list[Action] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (target, why)

    def __len__(self) -> int:
        return len(self.actions)

    @property
    def by_risk(self) -> dict[str, int]:
        out = {r: 0 for r in RISKS}
        for a in self.actions:
            out[a.risk] = out.get(a.risk, 0) + 1
        return out

    def sort(self) -> "Plan":
        # Settings first (they are instant and harmless), then packages by risk
        # then name, so the dangerous end of the list is always at the bottom.
        order = {"setting": 0, "unset": 0, "install": 1, "home": 2,
                 "enable": 3, "disable": 4}
        self.actions.sort(key=lambda a: (order.get(a.kind, 9),
                                         RISK_ORDER.get(a.risk, 9),
                                         a.label or a.target))
        return self
