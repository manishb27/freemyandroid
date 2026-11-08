"""A local web interface: the decision maker and the progress view.

The terminal interface assumes the person driving it is the person deciding.
That breaks down in the two situations this project actually gets used in:
an assistant is doing the work and the owner of the phone has to approve it,
and someone is debloating a phone for a relative who wants to see what is
being taken away.

So: a page the assistant can start and hand over. It lists what is proposed,
the owner ticks what they are comfortable with, presses Apply, and watches it
happen line by line. Nothing is applied without a click in this page.

Safety properties, in order of importance:

  * **Bound to 127.0.0.1 by default.** This page can disable apps on a phone.
    It is not something to expose to a network by accident.
  * **A random token is required** on every request, including the page
    itself. Printed once in the terminal. Without it the server answers 403,
    so another program on the same machine cannot drive it blindly.
  * **The guards still apply.** This is a second front end over the same
    `profiles.build()`; it cannot plan anything the TUI could not.
  * **Everything is journalled** to the same per-phone file on this computer,
    so Undo works across both interfaces and across restarts.

Standard library only: `http.server` with a worker thread, and the browser
polls. No websockets, no framework, no build step.
"""

from __future__ import annotations

import html
import json
import mimetypes
import os
import queue
import secrets
import sys
import threading
import time
import traceback
import webbrowser
from dataclasses import dataclass, field, asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import adb, battery, catalog, device, discover, journal, plan, profiles

WEB_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")

# How the phone is being used. This is the one question that changes what a
# sensible default looks like, and it is not something we can detect.
MODES = {
    "fresh": {
        "label": "Freshly reset phone",
        "blurb": "No data on it that anyone would miss. Everything that can "
                 "be turned off is pre-selected, including the aggressive "
                 "choices.",
        "default_intensity": "ultra",
        "preselect": ("safe", "caution", "risky"),
    },
    "inuse": {
        "label": "Phone someone is using",
        "blurb": "Someone's photos, messages and accounts are on it. Only "
                 "the changes nobody notices are pre-selected; anything that "
                 "removes a feature is left for you to tick.",
        "default_intensity": "standard",
        "preselect": ("safe",),
    },
}


@dataclass
class Step:
    text: str
    ok: bool = True
    done: bool = False
    at: float = field(default_factory=time.time)


@dataclass
class Session:
    """Everything the page needs to render, behind one lock."""
    mode: str = "inuse"
    serial: str = ""
    prof: device.Profile | None = None
    ctx: profiles.Context | None = None
    plan: plan.Plan | None = None
    plan_label: str = ""
    steps: list[Step] = field(default_factory=list)
    busy: str = ""
    error: str = ""
    finished: str = ""

    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # -- progress

    def say(self, text: str, ok: bool = True, done: bool = False) -> None:
        with self.lock:
            self.steps.append(Step(text=text, ok=ok, done=done))
            if len(self.steps) > 500:
                del self.steps[:-500]

    def reset_steps(self) -> None:
        with self.lock:
            self.steps = []
            self.finished = ""
            self.error = ""


class Jobs:
    """One worker thread. adb is not safe to hammer from several at once, and
    a queue of depth one keeps the page honest about what is happening."""

    def __init__(self, session: Session) -> None:
        self.q: queue.Queue = queue.Queue()
        self.session = session
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def submit(self, label: str, fn) -> bool:
        with self.session.lock:
            if self.session.busy:
                return False
            self.session.busy = label
        self.q.put((label, fn))
        return True

    def _run(self) -> None:
        while True:
            label, fn = self.q.get()
            try:
                fn()
            except adb.DeviceGone as e:
                self.session.say(f"The phone disconnected: {e}", ok=False)
                self.session.error = str(e)
            except Exception as e:                      # noqa: BLE001
                self.session.say(f"{type(e).__name__}: {e}", ok=False)
                self.session.error = f"{type(e).__name__}: {e}"
                traceback.print_exc()
            finally:
                with self.session.lock:
                    self.session.busy = ""
                self.q.task_done()


# --- the work the page can ask for -----------------------------------------


class Api:
    def __init__(self) -> None:
        self.s = Session()
        self.jobs = Jobs(self.s)

    # -- read

    def state(self) -> dict:
        s = self.s
        with s.lock:
            out: dict = {
                "mode": s.mode,
                "modes": MODES,
                "busy": s.busy,
                "error": s.error,
                "finished": s.finished,
                "steps": [asdict(x) for x in s.steps],
                "serial": s.serial,
                "device": None,
                "plan": None,
                "profiles": [
                    {"id": p["id"], "name": p["name"], "summary": p["summary"],
                     "intensities": [
                         {"id": k, "label": lbl, "blurb": blurb}
                         for k, lbl, blurb in profiles.describe_intensities(p["id"])
                     ]}
                    for p in profiles.load_profiles()
                ],
                "journal": [],
            }
            if s.prof:
                out["device"] = {
                    "name": s.prof.name,
                    "android": s.prof.android_label,
                    "vendor": s.prof.vendor_key,
                    "abi": s.prof.abi,
                    "ram_mb": s.prof.total_ram_mb,
                    "installed": len(s.prof.installed),
                    "disabled": len(s.prof.disabled),
                    "warnings": s.prof.warnings,
                    "launcher": s.ctx.current_launcher if s.ctx else "",
                    "launchers": s.ctx.launchers if s.ctx else [],
                    "avf": s.prof.avf,
                }
                out["journal"] = [
                    {"id": r.id, "when": r.when_text, "profile": r.profile,
                     "summary": r.summary, "count": len(r.reversible)}
                    for r in reversed(journal.load(s.serial))
                ]
            if s.plan:
                pre = MODES[s.mode]["preselect"]
                out["plan"] = {
                    "title": s.plan.title,
                    "label": s.plan_label,
                    "actions": [
                        {"i": i, "what": a.describe(), "risk": a.risk,
                         "note": a.note, "source": a.source,
                         "target": a.target, "kind": a.kind,
                         "preselect": a.risk in pre}
                        for i, a in enumerate(s.plan.actions)
                    ],
                    "skipped": [{"target": t, "why": w} for t, w in s.plan.skipped],
                    "by_risk": s.plan.by_risk,
                }
            return out

    def devices(self) -> dict:
        return {"devices": [
            {"serial": d.serial, "state": d.state, "label": d.label,
             "usable": d.usable, "wireless": d.wireless}
            for d in adb.devices()]}

    def catalog(self) -> dict:
        apps = catalog.load()
        installed = self.s.prof.installed if self.s.prof else set()
        return {"apps": [
            {"id": a.id, "name": a.name, "category": a.category, "pkg": a.pkg,
             "blurb": a.blurb, "note": a.note, "origin": a.origin,
             "ram_kb": getattr(a, "ram_kb", 0),
             "fills_role": a.fills_role,
             "installed": a.pkg in installed}
            for a in apps]}

    # -- write

    def set_mode(self, mode: str) -> dict:
        if mode not in MODES:
            return {"error": f"unknown mode {mode!r}"}
        with self.s.lock:
            self.s.mode = mode
        return {"ok": True}

    def scan(self) -> dict:
        def work() -> None:
            self.s.reset_steps()
            self.s.say("Looking for phones on this network…")
            found = discover.find(timeout=6.0)
            if not found:
                self.s.say("Nothing found. Turn on Wireless debugging and keep "
                           "that screen open: Android stops advertising when "
                           "the screen sleeps.", ok=False)
                return
            for f in found:
                self.s.say(f"{f.kind}: {f.instance} at {f.host_port}")
            for f in discover.connectable(found):
                msg = adb.connect(f.host_port)
                self.s.say(f"connect {f.host_port}: {msg}",
                           ok="connected" in msg.lower())
            self.s.say("Done.", done=True)
        return {"queued": self.jobs.submit("Scanning the network", work)}

    def pick(self, serial: str) -> dict:
        def work() -> None:
            self.s.reset_steps()
            self.s.say(f"Reading {serial}…")
            prof = device.read(serial)
            self.s.say(f"{prof.name}, {prof.android_label}, "
                       f"{len(prof.installed)} packages")
            self.s.say("Working out roles, launchers and what is already off…")
            ctx = profiles.build_context(prof)
            with self.s.lock:
                self.s.serial = serial
                self.s.prof = prof
                self.s.ctx = ctx
                self.s.plan = None
            for w in prof.warnings:
                self.s.say(w, ok=False)
            self.s.say("Ready.", done=True)
        return {"queued": self.jobs.submit("Reading the phone", work)}

    def build_plan(self, profile_id: str, intensity: str) -> dict:
        if not self.s.ctx:
            return {"error": "pick a phone first"}

        def work() -> None:
            self.s.reset_steps()
            self.s.say(f"Building the {profile_id}/{intensity} plan…")
            pl = profiles.build(self.s.ctx, profile_id, intensity)
            with self.s.lock:
                self.s.plan = pl
                self.s.plan_label = f"{profile_id}/{intensity}"
            self.s.say(f"{len(pl.actions)} changes proposed, "
                       f"{len(pl.skipped)} left alone by the guards.", done=True)
        return {"queued": self.jobs.submit("Building a plan", work)}

    def apply(self, indexes: list[int]) -> dict:
        if not self.s.plan or not self.s.prof:
            return {"error": "nothing to apply"}
        chosen = [self.s.plan.actions[i] for i in indexes
                  if 0 <= i < len(self.s.plan.actions)]
        if not chosen:
            return {"error": "nothing selected"}

        def work() -> None:
            self.s.reset_steps()
            d = adb.Device(self.s.serial)
            self.s.say(f"Applying {len(chosen)} changes. Nothing is "
                       f"uninstalled; every change is recorded so it can be "
                       f"put back.")
            done = []
            for a in chosen:
                plan.apply(d, a)
                done.append(a)
                self.s.say(a.describe() + ("" if a.ok else f"  —  {a.result}"),
                           ok=a.ok)
            run = journal.record(self.s.serial, self.s.plan_label or "web", {
                "name": self.s.prof.name, "model": self.s.prof.model,
                "android": self.s.prof.android,
                "manufacturer": self.s.prof.manufacturer,
                "vendor": self.s.prof.vendor_key}, done)
            ok = sum(1 for a in done if a.ok)
            self.s.say(f"{ok} of {len(done)} applied. Saved on this computer "
                       f"as run {run.id} in {journal.path_for(self.s.serial)} "
                       f"— use Undo to put any of it back.", done=True)
            with self.s.lock:
                self.s.finished = run.id
                # Re-read, so the page stops offering changes already made.
                self.s.prof = device.read(self.s.serial)
                self.s.ctx = profiles.build_context(self.s.prof)
                self.s.plan = None
        return {"queued": self.jobs.submit("Applying changes", work)}

    def undo(self, run_id: str) -> dict:
        if not self.s.serial:
            return {"error": "pick a phone first"}
        runs = {r.id: r for r in journal.load(self.s.serial)}
        run = runs.get(run_id)
        if not run:
            return {"error": f"no run {run_id!r} for this phone"}
        actions = run.undo_plan()
        if not actions:
            return {"error": "nothing in that run can be reversed"}

        def work() -> None:
            self.s.reset_steps()
            d = adb.Device(self.s.serial)
            self.s.say(f"Putting back {len(actions)} changes from {run.when_text}.")
            for a in actions:
                plan.apply(d, a)
                self.s.say(a.describe() + ("" if a.ok else f"  —  {a.result}"),
                           ok=a.ok)
            journal.record(self.s.serial, f"undo of {run.id}", run.device, actions)
            self.s.say("Done. Settings went back to the value they had, and "
                       "packages are enabled again.", done=True)
            with self.s.lock:
                self.s.prof = device.read(self.s.serial)
                self.s.ctx = profiles.build_context(self.s.prof)
                self.s.plan = None
        return {"queued": self.jobs.submit("Undoing", work)}

    def install(self, app_ids: list[str]) -> dict:
        if not self.s.prof:
            return {"error": "pick a phone first"}
        by_id = catalog.by_id()
        apps = [by_id[i] for i in app_ids if i in by_id]
        if not apps:
            return {"error": "nothing selected"}
        launchers = [a for a in apps if a.category == "launcher"]
        if len(launchers) > 1:
            return {"error": "pick one launcher, not " + str(len(launchers))
                             + ". Two home screens means Android asks you "
                               "which one every time you press Home."}

        def work() -> None:
            self.s.reset_steps()
            d = adb.Device(self.s.serial)
            abi = self.s.prof.abi
            actions = []
            for a in apps:
                self.s.say(f"Fetching {a.name} from {a.origin}…")
                try:
                    path = catalog.fetch(a, abi=abi)
                except catalog.CatalogError as e:
                    self.s.say(f"{a.name}: {e}", ok=False)
                    continue
                act = plan.Action(kind="install", target=a.pkg, value=path,
                                  label=a.name, risk="safe", source=a.origin)
                plan.apply(d, act)
                actions.append(act)
                self.s.say(f"{a.name} installed" if act.ok
                           else f"{a.name}: {act.result}", ok=act.ok)
                if not act.ok or not a.fills_role:
                    continue
                # Make it the default straight away: the point of this tool
                # is that nobody has to pick things up and tap through
                # Settings.
                dflt = plan.make_default(d, a.pkg, a.fills_role, a.name)
                if dflt is None:
                    self.s.say(f"{a.name} is installed, but Android has no "
                               f"'default {a.fills_role}' setting to point at "
                               f"it; it will be offered when you open an "
                               f"image or file.")
                    continue
                plan.apply(d, dflt)
                actions.append(dflt)
                self.s.say(dflt.describe() + ("" if dflt.ok
                                              else f"  —  {dflt.result}"),
                           ok=dflt.ok)
            if actions:
                journal.record(self.s.serial, "installed apps", {
                    "name": self.s.prof.name,
                    "android": self.s.prof.android}, actions)
            if launchers:
                self.s.say(f"{launchers[0].name} is now the home screen. "
                           "The old launcher is recorded, so Undo puts it "
                           "back, and it is now safe to turn off — which is "
                           "usually the single biggest memory saving on the "
                           "phone.", done=True)
            else:
                self.s.say("Done.", done=True)
            with self.s.lock:
                self.s.prof = device.read(self.s.serial)
                self.s.ctx = profiles.build_context(self.s.prof)
        return {"queued": self.jobs.submit("Installing apps", work)}

    def battery(self) -> dict:
        if not self.s.ctx:
            return {"error": "pick a phone first"}

        def work() -> None:
            self.s.reset_steps()
            self.s.say("Reading the phone's own power accounting. This is the "
                       "slow one.")
            rep = battery.report(adb.Device(self.s.serial),
                                 self.s.ctx.entries, limit=40)
            self.s.say("Sources: " + (", ".join(rep.sources) or "none"))
            for m in rep.missing:
                self.s.say("No data from " + m, ok=False)
            actions = []
            for h in rep.hogs:
                guard = self.s.ctx.guard(h.pkg)
                self.s.say(f"{h.score:7.1f}  {h.label}  —  {h.why}"
                           + (f"  [kept: {guard}]" if guard else ""))
                if h.known == "safe" and not guard:
                    actions.append(plan.Action(
                        kind="disable", target=h.pkg, label=h.label,
                        note=f"Ranked {h.score:.0f} by power use: {h.why}",
                        risk="safe", source="battery report"))
            with self.s.lock:
                self.s.plan = plan.Plan(title="Battery offenders",
                                        actions=actions)
                self.s.plan_label = "battery report"
            self.s.say(f"{len(actions)} of these are already known to be safe "
                       "to turn off and are proposed above.", done=True)
        return {"queued": self.jobs.submit("Reading power use", work)}


# --- HTTP -------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "freemyandroid"
    api: Api
    token: str

    def log_message(self, fmt: str, *args) -> None:
        pass            # the progress view is the log

    # -- helpers

    def _authed(self, qs: dict) -> bool:
        got = (qs.get("t") or [""])[0] or self.headers.get("X-Token", "")
        return secrets.compare_digest(got, self.token)

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _text(self, text: str, code: int = 200,
              ctype: str = "text/plain; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if n <= 0 or n > 1_000_000:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {}

    # -- routes

    def do_GET(self) -> None:
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if not self._authed(qs):
            self._text("403 — this page needs the link printed in the "
                       "terminal, token included.", 403)
            return

        if u.path in ("/", "/index.html"):
            self._serve_static("index.html")
            return
        if u.path.startswith("/static/"):
            self._serve_static(os.path.basename(u.path))
            return
        if u.path == "/api/state":
            self._json(self.api.state())
            return
        if u.path == "/api/devices":
            try:
                self._json(self.api.devices())
            except adb.AdbError as e:
                self._json({"error": str(e)}, 500)
            return
        if u.path == "/api/catalog":
            self._json(self.api.catalog())
            return
        self._text("404", 404)

    def do_POST(self) -> None:
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if not self._authed(qs):
            self._json({"error": "forbidden"}, 403)
            return
        body = self._body()
        routes = {
            "/api/mode": lambda: self.api.set_mode(body.get("mode", "")),
            "/api/scan": self.api.scan,
            "/api/pick": lambda: self.api.pick(body.get("serial", "")),
            "/api/plan": lambda: self.api.build_plan(body.get("profile", ""),
                                                     body.get("intensity", "")),
            "/api/apply": lambda: self.api.apply(
                [int(i) for i in body.get("indexes", []) if str(i).isdigit()]),
            "/api/undo": lambda: self.api.undo(body.get("run", "")),
            "/api/install": lambda: self.api.install(
                [str(x) for x in body.get("apps", [])]),
            "/api/battery": self.api.battery,
        }
        fn = routes.get(u.path)
        if not fn:
            self._json({"error": "no such endpoint"}, 404)
            return
        try:
            self._json(fn() or {"ok": True})
        except adb.AdbError as e:
            self._json({"error": str(e)}, 500)

    def _serve_static(self, name: str) -> None:
        safe = os.path.basename(name)
        path = os.path.join(WEB_ROOT, safe)
        if not os.path.isfile(path):
            self._text(f"404 — {html.escape(safe)} is missing from web/", 404)
            return
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def serve(host: str = "127.0.0.1", port: int = 8777,
          open_browser: bool = True) -> None:
    api = Api()
    token = secrets.token_urlsafe(18)

    handler = type("BoundHandler", (Handler,), {"api": api, "token": token})
    try:
        httpd = ThreadingHTTPServer((host, port), handler)
    except OSError as e:
        if e.errno in (48, 98):          # EADDRINUSE on BSD/macOS and Linux
            print(f"Port {port} on {host} is already in use. Either Free My "
                  f"Android is already running (look for the link in that "
                  f"terminal), or something else has the port.\n\n"
                  f"  ./freemyandroid web --port {port + 1}\n", flush=True)
            raise SystemExit(1) from None
        print(f"Could not start the server on {host}:{port}: {e}", flush=True)
        raise SystemExit(1) from None
    url = f"http://{host}:{httpd.server_port}/?t={token}"

    # Line buffering, because this is usually started in the background by
    # something that needs to read the URL out immediately. A buffered
    # startup banner that appears minutes later is the same as no banner.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, OSError):
        pass

    lines = ["Free My Android — web interface", "", f"  {url}", ""]
    if host not in ("127.0.0.1", "localhost", "::1"):
        lines += [f"  ! Bound to {host}, so anything on your network that has",
                  "    this link can disable apps on the attached phone.", ""]
    lines += [
        "  The link includes a one-time token for this run. Nothing is",
        "  applied to a phone until someone clicks Apply on that page.",
        "  Undo history is kept on this computer, in",
        f"    {journal.journal_dir()}",
        "", "  Ctrl-C to stop.", "",
    ]
    print("\n".join(lines), flush=True)

    if open_browser:
        threading.Thread(target=lambda: (time.sleep(0.4),
                                         webbrowser.open(url)),
                         daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
