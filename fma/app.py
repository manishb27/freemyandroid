"""The screens, in the order someone walks through them."""

from __future__ import annotations

import curses
import time

from . import adb, battery, catalog, device, discover, journal, plan, profiles
from .tui import (Back, Quit, Row, Screen, attr, pick, C_BAD, C_CAUTION, C_DIM,
                  C_OK, C_RISKY, C_SAFE, C_TITLE, _init_colors)


# --- picking a phone --------------------------------------------------------


def choose_device(sc: Screen) -> device.Profile:
    while True:
        try:
            listed = sc.busy("Looking for phones…", adb.devices)
        except adb.AdbError as e:
            sc.message("adb problem", [str(e)], bad=True)
            raise Quit from None

        rows: list[Row] = []
        for d in listed:
            if d.usable:
                rows.append(Row(text=f"{d.label}  —  {d.serial}",
                                note="Ready. Enter to use this phone.",
                                key=("use", d.serial)))
            elif d.state == "unauthorized":
                rows.append(Row(
                    text=f"{d.label}  —  waiting for you to allow it",
                    note="Unlock the phone and tap 'Allow' on the "
                         "'Allow USB debugging?' dialog, then press r.",
                    risk="caution", selectable=False))
            else:
                rows.append(Row(text=f"{d.serial}  —  {d.state}",
                                note=f"adb reports this as '{d.state}'.",
                                risk="caution", selectable=False))

        if not rows:
            rows.append(Row(
                text="No phone connected yet",
                note="Plug one in over USB with USB debugging on, or press s "
                     "to look for one on the network.",
                selectable=False))

        rows.append(Row(text="", selectable=False))
        rows.append(Row(text="Find a phone on this network (Wi-Fi)",
                        note="Android 11 and newer: Developer options → "
                             "Wireless debugging. Keep that screen open.",
                        key=("scan", None)))
        rows.append(Row(text="Pair with a code (first time over Wi-Fi)",
                        note="On the phone: Wireless debugging → Pair device "
                             "with pairing code.",
                        key=("pair", None)))
        rows.append(Row(text="Type an address by hand",
                        note="For example 192.168.1.42:5555",
                        key=("manual", None)))

        def on_key(k: int, _i: int):
            if k in (ord("r"), ord("R")):
                return ("refresh", None)
            if k in (ord("s"), ord("S")):
                return ("scan", None)
            if k in (ord("p"), ord("P")):
                return ("pair", None)
            return None

        try:
            what, value = pick(
                sc, "Choose a phone",
                "Everything this tool does goes over ADB. Nothing is installed on the phone.",
                rows,
                [("↑↓", "move"), ("Enter", "choose"), ("r", "refresh"),
                 ("s", "scan network"), ("p", "pair"), ("q", "quit")],
                on_key=on_key)
        except Back:
            raise Quit from None

        if what == "refresh":
            continue
        if what == "use":
            prof = sc.busy("Reading the phone…", lambda: device.read(value))
            if prof.warnings and not sc.confirm(
                    "Have a look at this first", prof.warnings + [
                        "", "Continue anyway?"]):
                continue
            return prof
        if what == "scan":
            _scan_and_connect(sc)
            continue
        if what == "pair":
            _pair_flow(sc)
            continue
        if what == "manual":
            addr = _ask(sc, "Address of the phone",
                        "host:port, as shown under Wireless debugging")
            if addr:
                msg = sc.busy(f"Connecting to {addr}…",
                              lambda: adb.connect(addr))
                sc.message("adb connect", [msg])
            continue


def _scan_and_connect(sc: Screen) -> None:
    found = sc.busy("Asking the network…", lambda: discover.find(timeout=5.0))
    conn = discover.connectable(found)
    pairs = discover.pairable(found)
    if not conn and not pairs:
        sc.message("Nothing found", [
            "No phone on this network is advertising wireless debugging.",
            "",
            "Check that: Wireless debugging is on in Developer options; the "
            "screen is still awake, because Android stops advertising when it "
            "sleeps; and the phone is on this Wi-Fi rather than mobile data.",
            "",
            "Some access points also block the discovery traffic between "
            "wireless clients. If yours does, use 'Type an address by hand' "
            "with the address the phone shows."])
        return
    rows = [Row(text=f"{f.instance}  —  {f.host_port}",
                note={"wireless": "Wireless debugging. Needs pairing once.",
                      "plain": "Plain ADB on port 5555. Connects directly.",
                      "pairing": "Waiting to be paired."}.get(f.kind, ""),
                key=f.host_port)
            for f in conn]
    if pairs:
        rows.append(Row(text="", selectable=False))
        for f in pairs:
            rows.append(Row(text=f"Pair with {f.instance} at {f.host_port}",
                            note="This phone is showing a pairing code now.",
                            key=("pair", f.host_port)))
    try:
        chosen = pick(sc, "Phones on the network", "", rows,
                      [("↑↓", "move"), ("Enter", "connect"), ("Esc", "back")])
    except Back:
        return
    if isinstance(chosen, tuple):
        _pair_flow(sc, chosen[1])
        return
    msg = sc.busy(f"Connecting to {chosen}…", lambda: adb.connect(chosen))
    if "cannot" in msg.lower() or "failed" in msg.lower():
        sc.message("Could not connect", [
            msg, "",
            "A phone advertising _adb-tls-connect needs to be paired first. "
            "Use 'Pair with a code'."], bad=True)


def _pair_flow(sc: Screen, host_port: str | None = None) -> None:
    if not host_port:
        found = sc.busy("Looking for a phone showing a pairing code…",
                        lambda: discover.find(timeout=5.0))
        pairs = discover.pairable(found)
        if pairs:
            host_port = pairs[0].host_port
    if not host_port:
        host_port = _ask(sc, "Pairing address",
                         "The 'IP address & port' under 'Pair device with "
                         "pairing code' — not the one on the main screen")
        if not host_port:
            return
    code = _ask(sc, "Pairing code", "The six digits the phone is showing")
    if not code:
        return
    try:
        msg = sc.busy("Pairing…", lambda: adb.pair(host_port, code))
    except adb.AdbError as e:
        sc.message("Pairing failed", [str(e)], bad=True)
        return
    ok = "successfully" in msg.lower()
    sc.message("Paired" if ok else "Pairing failed", [
        msg, "",
        "Now go back to the main Wireless debugging screen and use "
        "'Find a phone on this network'. The pairing port and the connect "
        "port are different, which is the single most confusing thing about "
        "wireless ADB." if ok else
        "The code expires quickly and the dialog issues a new one each time it "
        "opens. Close it, open it again, and read both the address and the "
        "code fresh."], bad=not ok)


def _ask(sc: Screen, title: str, hint: str) -> str:
    h, w = sc.size
    box_w = min(w - 4, 70)
    win = curses.newwin(7, box_w, (h - 7) // 2, (w - box_w) // 2)
    win.box()
    try:
        win.addnstr(0, 2, f" {title} ", box_w - 4, attr(C_TITLE, True))
        win.addnstr(2, 2, hint, box_w - 4, attr(C_DIM))
        win.addnstr(4, 2, "> ", box_w - 4)
    except curses.error:
        pass
    win.refresh()
    curses.echo()
    curses.curs_set(1)
    try:
        raw = win.getstr(4, 4, box_w - 8)
    except curses.error:
        raw = b""
    finally:
        curses.noecho()
        curses.curs_set(0)
    return raw.decode("utf-8", "replace").strip()


# --- the main menu ----------------------------------------------------------


def main_menu(sc: Screen, prof: device.Profile, ctx: profiles.Context) -> None:
    while True:
        rows: list[Row] = []
        for p in profiles.load_profiles():
            rows.append(Row(text=p["name"], note=p["summary"],
                            key=("profile", p["id"])))
        rows.append(Row(text="", selectable=False))
        rows.append(Row(text="What is draining the battery",
                        note="Reads this phone's own accounting and ranks "
                             "what is actually costing you power.",
                        key=("battery", None)))
        rows.append(Row(text="Apps and launchers",
                        note="Fetch open-source apps from F-Droid or the "
                             "project's own releases and install them. "
                             "No mirrors.",
                        key=("apps", None)))
        rows.append(Row(text="Pick packages by hand",
                        note="Every package on the phone, with what we know "
                             "about it. For people who want the list.",
                        key=("advanced", None)))
        rows.append(Row(text="", selectable=False))
        past = journal.load(prof.serial)
        rows.append(Row(
            text=f"Undo something ({len(past)} run{'s' if len(past) != 1 else ''} recorded)",
            note="Every change is written down and can be put back exactly."
                 if past else "Nothing has been changed on this phone yet.",
            key=("undo", None), selectable=bool(past)))

        sub = (f"{prof.name} · {prof.android_label} · "
               f"{len(prof.installed)} packages, {len(prof.disabled)} already off")
        try:
            what, value = pick(sc, "What would you like to do?", sub, rows,
                               [("↑↓", "move"), ("Enter", "choose"),
                                ("q", "quit")])
        except Back:
            return

        if what == "profile":
            run_profile(sc, prof, ctx, value)
        elif what == "battery":
            show_battery(sc, prof, ctx)
        elif what == "apps":
            show_apps(sc, prof, ctx)
        elif what == "advanced":
            show_advanced(sc, prof, ctx)
        elif what == "undo":
            show_undo(sc, prof)


# --- profiles ---------------------------------------------------------------


def run_profile(sc: Screen, prof: device.Profile, ctx: profiles.Context,
                profile_id: str) -> None:
    spec = {p["id"]: p for p in profiles.load_profiles()}[profile_id]
    rows = []
    for key, label, blurb in profiles.describe_intensities(profile_id):
        tier = spec["intensity"][key]
        text = label
        if tier.get("confirm_twice"):
            text += "   (asks twice)"
        rows.append(Row(text=text, note=blurb,
                        risk={"standard": "safe", "extreme": "caution",
                              "ultra": "risky"}[key],
                        key=key))
    try:
        intensity = pick(sc, spec["name"], spec["summary"], rows,
                         [("↑↓", "move"), ("Enter", "choose"), ("Esc", "back")])
    except Back:
        return

    pl = sc.busy("Working out what to change…",
                 lambda: profiles.build(ctx, profile_id, intensity))
    tier = spec["intensity"][intensity]
    if not pl.actions:
        sc.message("Nothing to do", [
            f"{pl.title} found nothing on this phone that it would change.",
            "",
            f"{len(pl.skipped)} candidates were left alone; the by-hand "
            "screen shows why." if pl.skipped else ""])
        return
    review_and_apply(sc, prof, ctx, pl, tier, profile_id)


def review_and_apply(sc: Screen, prof: device.Profile, ctx: profiles.Context,
                     pl: plan.Plan, tier: dict, source: str) -> None:
    checked = {id(a): True for a in pl.actions}

    def rows_now() -> list[Row]:
        out = []
        for a in pl.actions:
            note = a.note or ""
            if a.source:
                note = f"{note}  [{a.source}]" if note else f"[{a.source}]"
            out.append(Row(text=a.describe(), note=note, risk=a.risk,
                           checked=checked[id(a)], key=a))
        return out

    while True:
        counts = pl.by_risk
        n = sum(1 for a in pl.actions if checked[id(a)])
        sub = (f"{pl.title} · {n} of {len(pl.actions)} selected · "
               f"{counts['safe']} safe, {counts['caution']} caution, "
               f"{counts['risky']} risky"
               + (f" · {len(pl.skipped)} left alone" if pl.skipped else ""))

        def on_key(k: int, i: int):
            if k == ord(" ") and pl.actions:
                a = pl.actions[i]
                checked[id(a)] = not checked[id(a)]
                return None
            if k in (ord("a"), ord("A")):
                for a in pl.actions:
                    checked[id(a)] = True
            elif k in (ord("n"), ord("N")):
                for a in pl.actions:
                    checked[id(a)] = False
            elif k in (ord("s"), ord("S")):
                for a in pl.actions:
                    checked[id(a)] = a.risk == "safe"
            elif k in (ord("w"), ord("W")):
                return ("why", None)
            elif k in (10, 13, curses.KEY_ENTER):
                return ("go", None)
            return None

        try:
            got = pick(sc, "Review before anything changes", sub, rows_now,
                       [("↑↓", "move"), ("Space", "include/exclude"),
                        ("a", "all"), ("n", "none"), ("s", "safe only"),
                        ("w", "what was left alone"), ("Enter", "apply"),
                        ("Esc", "back")],
                       checkboxes=True, on_key=on_key)
        except Back:
            return
        if isinstance(got, tuple) and got[0] == "why":
            show_skipped(sc, pl)
            continue
        if isinstance(got, plan.Action):
            checked[id(got)] = not checked[id(got)]
            continue
        break

    picked = [a for a in pl.actions if checked[id(a)]]
    if not picked:
        return

    lines = [
        f"{len(picked)} change{'s' if len(picked) != 1 else ''} on {prof.name}.",
        "",
        "Nothing is uninstalled. Packages are disabled, which leaves them on "
        "the phone, and every change is written down so it can be put back "
        "from the Undo screen.",
    ]
    if tier.get("warn"):
        lines += ["", tier["warn"]]
    danger = bool(tier.get("confirm_twice"))
    if not sc.confirm("Apply these changes?", lines, danger=danger,
                      yes_word="apply"):
        return

    apply_actions(sc, prof, picked, pl.title if hasattr(pl, "title") else source)


def show_skipped(sc: Screen, pl: plan.Plan) -> None:
    if not pl.skipped:
        sc.message("Nothing left alone", ["Everything that matched was included."])
        return
    rows = [Row(text=pkg, note=why, selectable=False) for pkg, why in pl.skipped]
    try:
        pick(sc, "Left alone on purpose",
             "These matched the profile but a guard stopped them.", rows,
             [("↑↓", "move"), ("Esc", "back")])
    except Back:
        return


def apply_actions(sc: Screen, prof: device.Profile, actions: list[plan.Action],
                  label: str) -> None:
    d = adb.Device(prof.serial)
    log: list[tuple[str, bool]] = []
    done = 0
    gone = False

    for a in actions:
        sc.s.erase()
        sc.header("Applying", f"{done} of {len(actions)}")
        h, _w = sc.size
        start = max(3, h - 6 - len(log))
        for i, (line, ok) in enumerate(log[-(h - 8):]):
            sc.put(3 + i, 2, line, attr(C_OK if ok else C_BAD))
        sc.put(h - 4, 2, f"→ {a.describe()}", attr(C_TITLE))
        sc.s.refresh()
        try:
            plan.apply(d, a)
        except adb.DeviceGone as e:
            gone = True
            log.append((f"✗ phone disconnected: {e}", False))
            break
        done += 1
        log.append((("✓ " if a.ok else "✗ ") + a.describe()
                    + ("" if a.ok else f"   ({a.result})"), a.ok))

    run = journal.record(prof.serial, label, {
        "name": prof.name, "model": prof.model, "android": prof.android,
        "manufacturer": prof.manufacturer, "vendor": prof.vendor_key,
    }, actions[:done] if gone else actions)

    good = sum(1 for a in actions if a.ok)
    bad = sum(1 for a in actions if not a.ok and a.result)
    lines = [f"{good} change{'s' if good != 1 else ''} applied."]
    if bad:
        lines.append(f"{bad} did not go through. Highlighted in red above; "
                     "the usual cause is an OEM build refusing to disable one "
                     "of its own packages.")
    if gone:
        lines.append("The phone disconnected part way through. What had been "
                     "done is recorded and can still be undone.")
    lines += ["", f"Written to {journal.path_for(prof.serial)}",
              f"Undo it any time from the Undo screen (run {run.id})."]
    if any(a.kind == "disable" and a.ok for a in actions):
        lines += ["", "Restart the phone when convenient. Some OEM builds keep "
                  "a disabled service running until the next boot."]
    sc.message("Done", lines, bad=bool(bad and not good))


# --- battery ----------------------------------------------------------------


def show_battery(sc: Screen, prof: device.Profile, ctx: profiles.Context) -> None:
    d = adb.Device(prof.serial)
    rep = sc.busy("Reading the phone's own power accounting (slow)…",
                  lambda: battery.report(d, ctx.entries, limit=40))

    rows: list[Row] = []
    for h in rep.hogs:
        guard = ctx.guard(h.pkg)
        mark = {"safe": "can be turned off", "caution": "think first",
                "risky": "risky"}.get(h.known, "")
        text = f"{h.score:7.1f}  {h.label[:40]:42} {h.why}"
        note = []
        if mark:
            note.append(f"Our lists call this {mark}.")
        if h.tags:
            note.append("Tagged: " + ", ".join(h.tags) + ".")
        if guard:
            note.append(f"Will not be touched: {guard}.")
        rows.append(Row(text=text, note=" ".join(note) or h.pkg,
                        risk=h.known, key=h,
                        selectable=not guard and h.known == "safe",
                        disabled_reason=guard if guard else ""))

    sub = "Sources: " + (", ".join(rep.sources) or "none")
    if rep.missing:
        sub += "  ·  no data from: " + ", ".join(rep.missing)

    actionable = [r.key for r in rows if r.selectable]

    def on_key(k: int, _i: int):
        if k in (ord("d"), ord("D")) and actionable:
            return ("disable_all", None)
        return None

    try:
        got = pick(sc, "What is costing you power", sub, rows,
                   [("↑↓", "move"),
                    ("d", f"turn off the {len(actionable)} safe ones"),
                    ("Esc", "back")], on_key=on_key)
    except Back:
        return

    hogs = actionable if (isinstance(got, tuple)) else [got]
    hogs = [h for h in hogs if h is not None and getattr(h, "known", "") == "safe"]
    if not hogs:
        return
    actions = [plan.Action(kind="disable", target=h.pkg, label=h.label,
                           note=f"Ranked {h.score:.0f} by power use: {h.why}",
                           risk="safe", source="battery report")
               for h in hogs]
    pl = plan.Plan(title="Battery offenders", actions=actions)
    review_and_apply(sc, prof, ctx, pl, {}, "battery report")


# --- apps -------------------------------------------------------------------


def show_apps(sc: Screen, prof: device.Profile, ctx: profiles.Context) -> None:
    apps = catalog.load()
    d = adb.Device(prof.serial)
    abi = prof.abi
    chosen: set[str] = set()

    order = ["launcher", "essential", "store", "server", "useful"]
    titles = {"launcher": "Home screens", "essential": "Replacements for what you turn off",
              "store": "App stores", "server": "Running things on the phone",
              "useful": "Worth having"}

    def rows_now() -> list[Row]:
        out: list[Row] = []
        for cat in order:
            group = [a for a in apps if a.category == cat]
            if not group:
                continue
            out.append(Row(text=f"── {titles.get(cat, cat)} ",
                           selectable=False))
            for a in group:
                here = a.pkg in prof.installed
                text = a.name + ("   (already installed)" if here else "")
                note = a.blurb
                if a.note:
                    note = f"{note}  {a.note}" if note else a.note
                note = f"{note}  From {a.origin}." if note else f"From {a.origin}."
                out.append(Row(text=text, note=note,
                               checked=a.id in chosen, key=a,
                               selectable=True))
        return out

    while True:
        sub = (f"{len(chosen)} selected · downloaded from F-Droid or the "
               f"project's own releases, checksummed, then installed over ADB")

        def on_key(k: int, i: int):
            rs = rows_now()
            if k == ord(" ") and 0 <= i < len(rs) and rs[i].selectable:
                a = rs[i].key
                chosen.symmetric_difference_update({a.id})
                return None
            if k in (10, 13, curses.KEY_ENTER):
                return ("go", None)
            if k in (ord("b"), ord("B")):
                return ("bundle", None)
            return None

        try:
            got = pick(sc, "Apps and launchers", sub, rows_now,
                       [("↑↓", "move"), ("Space", "select"),
                        ("Enter", "install selected"),
                        ("b", "save as offline bundle"), ("Esc", "back")],
                       checkboxes=True, on_key=on_key)
        except Back:
            return
        if isinstance(got, catalog.App):
            chosen.symmetric_difference_update({got.id})
            continue
        if isinstance(got, tuple) and got[0] == "bundle":
            _save_bundle(sc, [a for a in apps if a.id in chosen], abi)
            continue
        break

    picked = [a for a in apps if a.id in chosen]
    if not picked:
        return
    if not sc.confirm("Install these?", [
            f"{len(picked)} app{'s' if len(picked) != 1 else ''} will be "
            f"downloaded and installed on {prof.name}:",
            "", ", ".join(a.name for a in picked),
            "",
            "Each one comes from F-Droid or the project's own GitHub releases "
            "over HTTPS, and its SHA-256 is recorded. Installing an app is "
            "not undone by the Undo screen: removing an app is a decision, "
            "not a rollback."]):
        return

    log: list[tuple[str, bool]] = []
    actions: list[plan.Action] = []
    for a in picked:
        sc.s.erase()
        sc.header("Installing", f"{len(log)} of {len(picked)}")
        h, _w = sc.size
        for i, (line, ok) in enumerate(log[-(h - 8):]):
            sc.put(3 + i, 2, line, attr(C_OK if ok else C_BAD))
        sc.put(h - 4, 2, f"→ {a.name}", attr(C_TITLE))
        sc.s.refresh()
        try:
            path = catalog.fetch(a, abi=abi)
        except catalog.CatalogError as e:
            log.append((f"✗ {a.name}: {e}", False))
            continue
        act = plan.Action(kind="install", target=a.pkg, value=path,
                          label=a.name, risk="safe", source=a.origin)
        try:
            plan.apply(d, act)
        except adb.DeviceGone as e:
            log.append((f"✗ phone disconnected: {e}", False))
            break
        actions.append(act)
        log.append((("✓ " if act.ok else "✗ ") + a.name
                    + ("" if act.ok else f"   ({act.result})"), act.ok))
        if act.ok and a.fills_role:
            dflt = plan.make_default(d, a.pkg, a.fills_role, a.name)
            if dflt is not None:
                plan.apply(d, dflt)
                actions.append(dflt)
                log.append((("✓ " if dflt.ok else "✗ ") + dflt.describe()
                            + ("" if dflt.ok else f"   ({dflt.result})"),
                            dflt.ok))

    if actions:
        journal.record(prof.serial, "installed apps", {
            "name": prof.name, "android": prof.android}, actions)
    ok = sum(1 for a in actions if a.ok)
    sc.message("Done", [
        f"{ok} installed." if ok else "Nothing was installed.",
        "",
        "A new launcher has been made the home screen already, and the old "
        "one is recorded so Undo puts it back. It is now safe to turn off, "
        "which is usually the biggest single memory saving on the phone."
        if any(c.category == "launcher" for c in picked) else "",
    ] + [line for line, good in log if not good][:6],
        bad=not ok)


def _save_bundle(sc: Screen, apps: list[catalog.App], abi: str) -> None:
    if not apps:
        sc.message("Nothing selected", ["Select some apps with Space first."])
        return
    dest = _ask(sc, "Save the bundle where?", "A folder; it will be created")
    if not dest:
        return
    try:
        paths = sc.busy(f"Downloading {len(apps)} APKs…",
                        lambda: catalog.bundle(apps, dest))
    except catalog.CatalogError as e:
        sc.message("Could not build the bundle", [str(e)], bad=True)
        return
    sc.message("Bundle saved", [
        f"{len(paths)} APKs and a bundle.json written to {dest}.",
        "",
        "Copy that folder to a machine on the network that cannot reach "
        "F-Droid and install from it there."])


# --- by hand ----------------------------------------------------------------


def show_advanced(sc: Screen, prof: device.Profile, ctx: profiles.Context) -> None:
    known = ctx.entries
    chosen: set[str] = set()
    query = ""

    def rows_now() -> list[Row]:
        out: list[Row] = []
        for pkg in sorted(prof.installed):
            e = known.get(pkg)
            label = e.label if e else pkg
            if query and query.lower() not in f"{pkg} {label}".lower():
                continue
            guard = ctx.guard(pkg)
            off = pkg in prof.disabled
            text = f"{label[:38]:40} {pkg}"
            if off:
                text = f"{'(off) ' + label[:32]:40} {pkg}"
            note = (e.note if e else "Not in any curated list. "
                    "We know nothing about it beyond its name.")
            if e and e.tags:
                note += "  Tagged: " + ", ".join(e.tags) + "."
            out.append(Row(text=text, note=note,
                           risk=(e.risk if e else ""),
                           checked=pkg in chosen,
                           key=pkg,
                           selectable=not guard or guard == "already off",
                           disabled_reason=guard if guard and guard != "already off" else ""))
        return out

    while True:
        rs = rows_now()
        sub = (f"{len(rs)} shown · {len(chosen)} selected"
               + (f" · filter: {query!r}" if query else ""))

        def on_key(k: int, i: int):
            nonlocal query
            if k == ord(" ") and 0 <= i < len(rs) and rs[i].selectable:
                chosen.symmetric_difference_update({rs[i].key})
                return None
            if k == ord("/"):
                query = _ask(sc, "Filter", "Part of a name or a package")
                return None
            if k in (10, 13, curses.KEY_ENTER):
                return ("go", None)
            return None

        try:
            got = pick(sc, "Every package on the phone", sub, rows_now,
                       [("↑↓", "move"), ("Space", "select"), ("/", "filter"),
                        ("Enter", "turn off selected"), ("Esc", "back")],
                       checkboxes=True, on_key=on_key)
        except Back:
            return
        if isinstance(got, str):
            chosen.symmetric_difference_update({got})
            continue
        break

    picked = sorted(chosen)
    if not picked:
        return
    unknown = [p for p in picked if p not in known]
    lines = [f"Turn off {len(picked)} package{'s' if len(picked) != 1 else ''}."]
    if unknown:
        lines += ["",
                  f"{len(unknown)} of them are not in any curated list, which "
                  "means nobody has checked what breaks without them: "
                  + ", ".join(unknown[:6])
                  + (" …" if len(unknown) > 6 else "")]
    lines += ["", "All of it is reversible from the Undo screen."]
    if not sc.confirm("Turn these off?", lines, danger=bool(unknown),
                      yes_word="apply"):
        return
    actions = [plan.Action(
        kind="disable", target=p,
        label=known[p].label if p in known else p,
        note=known[p].note if p in known else "Hand-picked.",
        risk=known[p].risk if p in known else "risky",
        source="by hand") for p in picked]
    apply_actions(sc, prof, actions, "by hand")


# --- undo -------------------------------------------------------------------


def show_undo(sc: Screen, prof: device.Profile) -> None:
    runs = journal.load(prof.serial)
    if not runs:
        sc.message("Nothing recorded", ["No changes have been made to this phone."])
        return
    rows = []
    for run in reversed(runs):
        n = len(run.reversible)
        rows.append(Row(
            text=run.summary,
            note=(", ".join(a.describe() for a in run.reversible[:4])
                  + (" …" if n > 4 else "")) or "Nothing in this run can be reversed.",
            key=run, selectable=bool(n)))
    try:
        run = pick(sc, "Undo", f"{prof.name} · newest first", rows,
                   [("↑↓", "move"), ("Enter", "undo this run"), ("Esc", "back")])
    except Back:
        return
    actions = run.undo_plan()
    if not actions:
        return
    if not sc.confirm("Put these back?", [
            f"{len(actions)} change{'s' if len(actions) != 1 else ''} from "
            f"{run.when_text} will be reversed:",
            "", ", ".join(a.describe() for a in actions[:10])
            + (" …" if len(actions) > 10 else ""),
            "",
            "Settings go back to the exact value they had before. Packages are "
            "re-enabled. Apps that were installed are left installed."]):
        return
    apply_actions(sc, prof, actions, f"undo of {run.id}")


# --- entry ------------------------------------------------------------------


def run(stdscr: "curses._CursesWindow") -> None:
    curses.curs_set(0)
    _init_colors()
    stdscr.keypad(True)
    sc = Screen(stdscr)
    try:
        prof = choose_device(sc)
        ctx = sc.busy("Taking stock of the phone…",
                      lambda: profiles.build_context(prof))
        main_menu(sc, prof, ctx)
    except (Quit, Back):
        return
    except adb.DeviceGone as e:
        sc.message("The phone disconnected", [str(e)], bad=True)
    except adb.AdbError as e:
        sc.message("ADB problem", [str(e)], bad=True)


def main() -> int:
    try:
        adb.find_adb()
    except adb.AdbError as e:
        print(e)
        return 2
    try:
        curses.wrapper(run)
    except KeyboardInterrupt:
        return 130
    return 0
