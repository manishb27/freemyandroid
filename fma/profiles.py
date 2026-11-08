"""Turning a choice of profile and intensity into a list of actions.

This is where the judgement lives. The package lists say what a thing is; this
decides whether to touch it on *this* phone. Four guards sit between a package
list and the plan, and all four apply at every intensity:

  1. `protected.json` — packages that brick a phone. Never, at all.
  2. Role occupancy  — the app currently answering calls, sending texts,
                       drawing the home screen or typing is not disabled
                       unless something else can take over.
  3. Accessibility   — a screen reader in use is never turned off.
  4. Already-off     — a package the user disabled themselves is left alone,
                       so our undo never re-enables something they chose.

Ultra intensity raises the risk ceiling. It does not lower the guards.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from . import adb, device
from .plan import Action, Plan, RISK_ORDER

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")

INTENSITIES = ("standard", "extreme", "ultra")


# --- loading ---------------------------------------------------------------


def _read(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@dataclass
class Entry:
    id: str
    label: str
    risk: str
    tags: list[str]
    note: str
    source: str

    @property
    def tagset(self) -> set[str]:
        return set(self.tags)


def load_packages() -> dict[str, list[Entry]]:
    """-> {vendor_key or '*': [Entry, ...]}"""
    out: dict[str, list[Entry]] = {}
    d = os.path.join(DATA, "packages")
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".json"):
            continue
        blob = _read(os.path.join(d, fn))
        entries = [
            Entry(
                id=p["id"],
                label=p.get("label", p["id"]),
                risk=p.get("risk", "caution"),
                tags=list(p.get("tags", [])),
                note=p.get("note", ""),
                source=blob.get("name", blob.get("id", fn)),
            )
            for p in blob.get("packages", [])
        ]
        for key in blob.get("applies_to", ["*"]):
            out.setdefault(key, []).extend(entries)
    return out


def load_profiles() -> list[dict]:
    d = os.path.join(DATA, "profiles")
    profs = [_read(os.path.join(d, fn)) for fn in sorted(os.listdir(d))
             if fn.endswith(".json")]
    profs.sort(key=lambda p: p.get("order", 99))
    return profs


def load_protected() -> dict:
    return _read(os.path.join(DATA, "protected.json"))


def load_roles() -> dict:
    return _read(os.path.join(DATA, "roles.json"))


# --- the live picture of one phone -----------------------------------------


@dataclass
class Context:
    prof: device.Profile
    dev: adb.Device
    launchable: set[str] = field(default_factory=set)
    launchers: list[str] = field(default_factory=list)
    current_launcher: str = ""
    role_holders: dict[str, str] = field(default_factory=dict)
    doze: set[str] = field(default_factory=set)
    entries: dict[str, Entry] = field(default_factory=dict)
    protected: dict = field(default_factory=dict)
    roles: dict = field(default_factory=dict)

    def guard(self, pkg: str) -> str:
        """'' if the package may be disabled, otherwise why not."""
        p = self.protected
        if pkg in set(p.get("never_disable", [])):
            return "protected: the phone needs it to boot or to make calls"
        for pre in p.get("never_disable_prefixes", []):
            if pkg.startswith(pre):
                return f"protected: vendor/system component ({pre}*)"
        if pkg in set(p.get("accessibility", [])):
            return "accessibility: never disabled automatically"
        if pkg == self.current_launcher:
            return "this is the current home screen"
        if pkg in self.launchers and len(self.launchers) <= 1:
            return "the only home screen on the phone"
        for role, holder in self.role_holders.items():
            if holder == pkg and role in p.get("roles_that_need_a_replacement_first", {}):
                return f"it is the current {role}: {p['roles_that_need_a_replacement_first'][role]}"
        if pkg in self.prof.disabled:
            return "already off"
        if pkg not in self.prof.installed:
            return "not installed"
        return ""


def build_context(prof: device.Profile, *, quick: bool = False) -> Context:
    d = adb.Device(prof.serial)
    pkgs = load_packages()
    entries: dict[str, Entry] = {}
    for e in pkgs.get("*", []) + pkgs.get(prof.vendor_key, []):
        entries[e.id] = e          # vendor list wins on a clash, by ordering

    ctx = Context(
        prof=prof, dev=d, entries=entries,
        protected=load_protected(), roles=load_roles(),
    )
    if quick:
        return ctx

    ctx.launchable = device.launchable_packages(d)
    ctx.launchers = device.installed_launchers(d)
    ctx.current_launcher = device.current_launcher(d)
    ctx.doze = d.doze_whitelist()

    role_defs = ctx.roles.get("roles", {})
    for role, candidates in role_defs.items():
        for cand in candidates:
            if cand in prof.enabled:
                ctx.role_holders[role] = cand
                break
    return ctx


# --- building the plan ------------------------------------------------------


def _ops_for(spec: list[dict], ctx: Context, source: str) -> list[Action]:
    out: list[Action] = []
    for op in spec:
        if op.get("min_sdk") and ctx.prof.sdk < int(op["min_sdk"]):
            continue
        if op.get("only_if_installed") and op.get("target") not in ctx.prof.installed:
            continue
        out.append(Action(
            kind=op.get("kind", "setting"),
            target=op.get("target", ""),
            value=str(op.get("value", "")),
            label=op.get("label", op.get("target", "")),
            note=op.get("note", ""),
            risk=op.get("risk", "safe"),
            source=source,
        ))
    return out


def build(ctx: Context, profile_id: str, intensity: str) -> Plan:
    profs = {p["id"]: p for p in load_profiles()}
    if profile_id not in profs:
        raise KeyError(f"no such profile: {profile_id}")
    if intensity not in INTENSITIES:
        raise KeyError(f"no such intensity: {intensity}")
    prof = profs[profile_id]
    tier = prof["intensity"][intensity]

    plan = Plan(title=f"{prof['name']} — {tier['label']}")
    ceiling = RISK_ORDER.get(tier.get("max_risk", "safe"), 0)

    if prof.get("strategy") == "allowlist":
        _build_allowlist(plan, ctx, prof, tier, ceiling)
    else:
        _build_denylist(plan, ctx, prof, tier, ceiling)

    plan.actions.extend(_ops_for(prof.get("ops", []), ctx, prof["id"]))
    plan.actions.extend(_ops_for(tier.get("extra_ops", []), ctx, prof["id"]))

    # A setting already at the value we want is not a change worth listing.
    plan.actions = [a for a in plan.actions if not _already_set(ctx, a)]
    return plan.sort()


def _already_set(ctx: Context, a: Action) -> bool:
    if a.kind == "setting":
        ns, _, key = a.target.partition("/")
        try:
            return ctx.dev.get_setting(ns, key) == a.value
        except adb.AdbError:
            return False
    if a.kind == "doze_allow":
        return a.target in ctx.doze
    return False


def _build_denylist(plan: Plan, ctx: Context, prof: dict, tier: dict,
                    ceiling: int) -> None:
    want = set(tier.get("tags", []))
    never = set(tier.get("never_tags", []))
    for pkg, e in sorted(ctx.entries.items()):
        if not (e.tagset & want) or (e.tagset & never):
            continue
        if RISK_ORDER.get(e.risk, 9) > ceiling:
            plan.skipped.append((pkg, f"{e.risk}: above the {tier['label']} ceiling"))
            continue
        why = ctx.guard(pkg)
        if why:
            if why not in ("not installed", "already off"):
                plan.skipped.append((pkg, why))
            continue
        plan.actions.append(Action(
            kind="disable", target=pkg, label=e.label, note=e.note,
            risk=e.risk, source=e.source,
        ))


def _build_allowlist(plan: Plan, ctx: Context, prof: dict, tier: dict,
                     ceiling: int) -> None:
    roles = ctx.roles
    keep_roles = tier.get("keep_roles") or roles.get("elder_keep_roles", [])
    keep: set[str] = set(roles.get("elder_keep_extra", []))
    for role in keep_roles:
        # Keep every candidate for the role that is installed, not just the
        # one currently in use: disabling the spare dialer helps nobody.
        for cand in roles.get("roles", {}).get(role, []):
            if cand in ctx.prof.installed:
                keep.add(cand)
    if tier.get("keep_home_screen_apps", False):
        keep |= _home_screen_apps(ctx)
    keep |= set(ctx.launchers)

    for pkg in sorted(ctx.launchable):
        if pkg in keep:
            continue
        e = ctx.entries.get(pkg)
        risk = e.risk if e else "caution"
        if RISK_ORDER.get(risk, 9) > ceiling:
            plan.skipped.append((pkg, f"{risk}: above the {tier['label']} ceiling"))
            continue
        why = ctx.guard(pkg)
        if why:
            if why not in ("not installed", "already off"):
                plan.skipped.append((pkg, why))
            continue
        plan.actions.append(Action(
            kind="disable", target=pkg,
            label=e.label if e else pkg,
            note=e.note if e else "Not in any curated list; hidden because Elder keeps only the essentials.",
            risk=risk, source=e.source if e else "app drawer",
        ))


def _home_screen_apps(ctx: Context) -> set[str]:
    """Whatever is on the home screen now, so Standard does not hide it.

    There is no supported way to read another launcher's layout, so this reads
    the favourites the framework knows about and accepts that it will miss
    some. Missing one means Elder Standard leaves an app enabled that it could
    have turned off, which is the safe direction to be wrong in.
    """
    out: set[str] = set()
    r = ctx.dev.shell(
        "dumpsys package preferred-activities | grep -o "
        "'[a-z][a-z0-9_]*\\(\\.[a-z0-9_]\\+\\)\\+' | sort -u", timeout=30)
    for line in r.out.split():
        if adb.PKG_RE.match(line) and line in ctx.prof.installed:
            out.add(line)
    return out


def describe_intensities(profile_id: str) -> list[tuple[str, str, str]]:
    """-> [(intensity, label, blurb)] for the menu."""
    profs = {p["id"]: p for p in load_profiles()}
    prof = profs[profile_id]
    out = []
    for key in INTENSITIES:
        tier = prof["intensity"].get(key)
        if tier:
            out.append((key, tier.get("label", key.title()), tier.get("blurb", "")))
    return out
