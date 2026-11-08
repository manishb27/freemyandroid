# What needs doing

Ordered by how much it matters, not by how hard it is. An agent picking this
up should read [CLAUDE.md](../CLAUDE.md) first — particularly the four guards,
which are the part that must not be weakened to make anything here easier.

## 1. A test suite — the biggest gap

There is none. Everything has been verified by hand against two devices. The
pieces that can be tested without a phone, in order of value:

- **Guards.** Build a `profiles.Context` from a recorded package list and
  assert that `guard()` refuses the right things. A fixture per vendor. This
  is the test that stops someone bricking a stranger's phone.
- **Plan monotonicity.** For every profile, `standard ⊆ extreme ⊆ ultra`.
  This has held on both test devices and nothing currently enforces it.
- **Journal round-trip.** `Action → to_json → from_json → invert` must survive,
  including for action kinds added after a run was recorded.
- **dumpsys parsers.** Check in real captured output from several Android
  versions as fixtures. Two of the three parsers were wrong on first write
  and only a real device caught it; see §4.
- **Catalogue resolution.** Hitting the network in CI is rude, so record the
  F-Droid and GitHub API responses and replay them.

No pytest — `unittest` is in the standard library and the no-dependency rule
applies to tests too.

## 2. The web panel for the server profile

Designed in [SERVER.md](SERVER.md), not built. Every existing panel in this
space (Dokploy, CasaOS, Portainer, Umbrel, Cosmos) is Docker-based and
therefore impossible on an unrooted phone, which is the argument for writing
one rather than giving up.

Decided: one stdlib-only Python file, JSON recipes instead of images, PID-file
supervision because proot has no usable PID 1, bound to the Tailscale
interface rather than `0.0.0.0`.

Open question: proot services only, or proot *and* udocker behind one
interface. Both is nicer and roughly doubles the supervision work. Start with
proot.

First recipes, in order of how well they actually run on a phone: Stirling
PDF, a static site via Caddy, FreshRSS, Syncthing, Gitea, Uptime Kuma.

## 3. More vendor package lists

Present: Google/AOSP, Samsung, Xiaomi, ColorOS (OnePlus/OPPO/realme),
third-party preloads. 209 entries.

Missing, roughly by install base: **vivo/iQOO**, **Motorola/Lenovo**,
**Transsion** (Tecno, Infinix, itel — enormous in India and Africa and almost
entirely uncatalogued), **Honor**, **Huawei**, **Nothing**, **Asus**.

A list is only useful if every entry has a `note` saying what breaks. Adding
forty package names with no notes makes the tool more dangerous, not more
capable.

## 4. dumpsys parsing is fragile

`fma/battery.py` reads output that is not a stable interface. Three bugs so
far, all found by running against a real phone and none by reading the code:

- `dumpsys alarm` puts the package name **after** the counts.
- `dumpsys jobscheduler` uses a bare uid, not the `u0a12` form.
- Android 12 writes `UID u0a119:` with a **capital** UID; the older format is
  `Uid u0a234:`. Case-sensitivity silently dropped every power figure while
  still reporting `batterystats` as a working source.

Needed: captured fixtures from Android 8 through 16 across several OEMs, and a
`--dump-raw` flag so someone reporting a bad report can attach the input.

## 5. Elder mode deserves more than package disabling

Right now it disables apps and changes seven settings. The things that would
actually help an elderly user and are reachable over ADB but not yet done:

- **Display size** (`wm density`), not just font scale. Riskier — a bad value
  needs ADB to undo — so it needs its own confirmation and a known-good
  fallback.
- **Configuring a large-tile launcher.** We can install Fossify Launcher and
  make it Home, but cannot set its icon size; Android gives no ADB route into
  another app's preferences. Possibly ship a settings backup and restore it.
- Setting defaults *is* possible over ADB and is now done
  (`cmd package set-home-activity`, `cmd role add-role-holder`, `ime set`).
  Earlier versions of this file claimed Android forbids it. It does not.
  What remains genuinely unreachable: a "default gallery", which is not a
  role at all — apps just register for image intents, so `make_default()`
  returns None rather than failing quietly.

## 6. The on-device interface

Open, and the biggest product question in the repo. See the discussion in
SERVER.md and CLAUDE.md's "deliberately missing" section. Two readings:

- **An on-device debloater**, so no computer is needed at all. Requires
  Shizuku or implementing Android 11 wireless-debugging pairing (SPAKE2 over
  TLS-PSK) inside an app. Note that Free My TV's `Adb.java` explicitly gives
  up here: it bails on `STLS` and tells you to use USB debugging, which works
  on a TV where port 5555 is open and does not work on a phone.
  [Canta](https://github.com/samolego/Canta) already does this job well.
- **A home screen in the Free My TV style.** A phone has 22 good open-source
  launchers and a TV has none, which is why Free My TV ships one and this does
  not. Installing an existing launcher is the current answer.

Do not start either without settling which one it is.

## Smaller things

- `ro.build.characteristics` is not reliable for TV detection on every box; the
  leanback-feature check is the fallback and should probably be primary.
- `installed_launchers()` counts `com.android.settings` as a launcher on
  Samsung, because it genuinely declares a HOME activity. Harmless (it is
  protected) but it makes the "only launcher left" guard less meaningful.
- No Windows testing at all. `curses` is not in the Windows standard library;
  it needs `windows-curses`, which breaks the no-dependency rule. Probably the
  answer is to detect Windows and recommend WSL.
- `verify_signature()` only works if `apksigner` happens to be installed. A
  pure-Python APK v2 signature check would remove the caveat in the README.
- The journal has no `prune`. It will grow forever.
