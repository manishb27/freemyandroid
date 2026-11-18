# Free My Android: notes for Claude

## Always

- **Python 3.10+, standard library only.** No `pip`, no virtualenv, no build
  step, no `requirements.txt`. Someone clones the repo and runs
  `./freemyandroid`. If a feature needs a dependency, the feature is wrong.
  `curses`, `json`, `urllib`, `socket`, `hashlib` and `subprocess` cover
  everything here.
- **`adb` is the only external binary**, found through `fma.adb.find_adb()`
  (honours `$FMA_ADB`). Never shell out to anything else without a fallback.
- **Nothing is ever uninstalled.** `pm disable-user --user 0` is the strongest
  thing we do. If you find yourself writing `pm uninstall`, stop.
- **Every change records its inverse at apply time**, not at plan time —
  only then do we know the value being replaced. See `fma/plan.py:apply`.
- **Explain, then ask.** Most people who end up running this are being helped
  by someone else, or are following a link. Error text should say what to do
  next, not what failed.
- **Backing up only reads.** `fma/backup.py` never changes the phone, and
  nothing on the command line does either: `plan` prints, `backup` reads.
  Changing a phone needs an interface where it can be confirmed.
- **Do not invent results.** No device reports, timings or numbers that were
  not measured. If something has not been run on real hardware, the docs and
  the changelog say so in those words.

## The shape of it

```
freemyandroid          entry point; subcommands print, never change a phone
fma/adb.py             every call to adb; the only place that builds a shell string
fma/discover.py        DNS-SD over multicast, with a unicast sweep fallback
fma/device.py          what kind of phone this is; roles, launchers, AVF support
fma/profiles.py        profile + intensity -> Plan. The guards live here
fma/plan.py            Action, apply(), invert(). Add action kinds here
fma/journal.py         the undo record, one file per phone
fma/battery.py         dumpsys parsing and the ranking
fma/catalog.py         fetching APKs from F-Droid and GitHub releases
fma/tui.py             curses widgets: Screen, pick(), confirm(), busy()
fma/app.py             the screens
fma/backup.py          look around, let the owner choose, copy, verify
data/packages/*.json   the curated lists: 209 entries over 5 phone files, plus
                       androidtv.json (38) which is loaded *instead* on a TV
data/profiles/*.json   fast, elder, dev — each with 3 intensities
data/protected.json    the hard guard. 62 packages, 17 prefixes, 6 screen readers
data/roles.json        20 roles: what a phone needs to still be a phone
data/apps.json         32 sideloadable open-source apps
fma/web.py             the web interface: decisions and live progress
fma/platform_tools.py  fetching adb itself from Google, for all 3 platforms
web/                   index.html, app.js, style.css. No framework, no build
tools/check.sh         everything CI runs. Run this before saying "done"
tools/fake_adb.py      a stand-in for adb, so pipelines run with no phone
tools/test_backup.py   the backup pipeline, end to end, against the fake phone
tools/test_cli_backup.py   `backup --check --json`, `--only`, `--skip`, as an assistant runs them
tools/test_web.py      loads the web page the way a browser does
tools/test_tui.py      presses keys at the list widget in a pseudo-terminal
docs/BACKUP.md         the order of operations, and what to do before a reset
```

Current version: **1.2.0** (`fma/__init__.py`). The marketing site lives in a
separate repository, `freemyandroid-site`; `docs/` here is documentation only.

## The four guards

All of them apply at every intensity. **Ultra raises the risk ceiling; it does
not lower the guards.** They are implemented in `profiles.Context.guard()`:

1. `data/protected.json` — never, at all.
2. Role occupancy — the current dialer, SMS app, keyboard, browser and home
   screen are safe unless a replacement exists.
3. Accessibility — a screen reader is never disabled automatically.
4. Already-off — a package the user disabled themselves is left alone, so undo
   never re-enables something they chose.

If a change you are making needs a guard relaxed, that is a sign the change is
wrong. The one sanctioned way past them is the Advanced screen, where a human
hand-picks a package and confirms twice.

## Adding a package to a list

```json
{"id": "com.example.thing", "label": "Thing",
 "risk": "safe", "tags": ["ads"],
 "note": "What stops working. Written for someone deciding right now."}
```

- `risk` is `safe` | `caution` | `risky`. It *is* the intensity ceiling, so it
  is the most consequential field in the repo. `safe` means nothing a normal
  person would notice. `caution` means a visible feature goes. `risky` means
  someone will file an issue.
- `tags` drive profile selection. Existing ones: `ads telemetry privacy partner
  bloat store games ar assistant oem-cloud kids print health media office
  social browser sync google aosp launcher dialer sms system battery`.
- `note` is mandatory in review. A package name with no note is how people's
  phones get broken.
- `applies_to` in the file header matches the vendor key from
  `device.vendor_key()`, which normalises `ro.product.manufacturer` and
  `ro.product.brand`. `["*"]` means every phone.

## Default apps: it all works over ADB

Nobody should have to pick up the phone and tap through Settings. All of this
works over plain ADB without root, and is implemented in `fma/adb.py`:

```
cmd package set-home-activity <pkg>/<Activity>    the home screen
cmd role add-role-holder android.app.role.SMS <pkg>   sms, dialer, browser…
ime enable <id> && ime set <id>                   the keyboard
```

Two traps, both already paid for:

- `set-home-activity` needs a **full component**, not a package. Resolve it
  with `Device.home_activity()`; guessing `.MainActivity` silently does
  nothing.
- Samsung ships `cmd role` with `add-role-holder` and `remove-role-holder`
  but **without** `get-role-holders` or `list-roles`. The write side works
  while the read side does not, so `Device.role_holders()` falls back to
  `cmd shortcut get-default-launcher` for home and
  `settings get secure sms_default_application` for sms, and returns `[]`
  otherwise. When it returns `[]` the change is applied but recorded as not
  reversible, which the note on the action says out loud.

There is no "default gallery" role. `plan.make_default()` returns None for it
rather than producing an action that fails quietly.

**Role holders beat the candidate lists.** `data/roles.json` lists candidates
in preference order, which is a guess. Where the device knows the real
holder, `build_context()` overrides the guess — otherwise installing a new
launcher leaves the guard still naming the *stock* launcher as "the current
launcher", because it sits first in the list, and the one package you most
wanted to turn off stays protected for the wrong reason.

## Adding an action kind

In `fma/plan.py`: a branch in `apply()` that sets `a.undo` to the exact
inverse, and a matching branch for that inverse kind. Then a line in
`Action.describe()`. Journal entries are plain data, so an action kind that
cannot round-trip through `to_json`/`from_json` will break undo for runs
recorded before your change.

Do **not** add a generic `shell` action kind that takes a command from JSON.
The package lists are data; a data file that can execute arbitrary shell is a
code-execution hole dressed as a feature. Add a narrow kind instead, the way
`doze_allow` is narrow.

## dumpsys parsing

`fma/battery.py` parses output that is not a stable interface and differs
between OEMs and Android versions. The rule: **a reader that does not
recognise the format returns nothing and says so** via `Report.missing`.
Never guess. A thin report that looks thin is correct behaviour; a confident
report built from a half-matched regex is not.

Formats verified on a real device (OnePlus, API 30) — note the package comes
*last* in the alarm stats, which is easy to get wrong:

```
+2m15s788ms running, 0 wakeups, 61 alarms: 1000:android
JOB #1000/1: 50d1dc6 com.android.providers.settings/.WriteJobService
```

## APK sources

Two, and the restriction is the security model: the official F-Droid repo, and
a project's own GitHub releases. **Never add a mirror, an APK-download site or
an unofficial build.** Those are where repackaged binaries come from, and many
ISPs sinkhole them, so a download that succeeds from one of those is worse
than one that fails.

Gotchas already paid for, do not undo them:

- Termux and its add-ons must share a signing key, so they all come from
  F-Droid. The GitHub assets are debug-signed and incompatible.
- Fossify Calculator is `org.fossify.math`, not `.calculator`.
- Syncthing's original `com.nutomic.syncthingandroid` was discontinued in 2024
  and is gone from F-Droid; the maintained fork is
  `com.github.catfriend1.syncthingandroid` on GitHub.
- Aurora Store's `whyorean/AuroraStore` GitHub releases are gone; it moved to
  GitLab, so it comes from F-Droid.
- Pick GitHub assets by **ABI**, not by file size — see `_asset_rank()`. The
  smallest asset is often the wrong architecture, which installs and then
  refuses to start.

## Wireless ADB

- The pairing port and the connect port are different. This confuses everyone
  including us; error text should say so.
- Android stops advertising over mDNS when the screen sleeps.
- `adb mdns services` is useless on Debian and Ubuntu — they ship adb with
  mDNS compiled out and it returns an empty list rather than an error. That is
  why `fma/discover.py` exists.
- Multicast is dropped between wireless clients by many home access points, so
  `discover.find()` falls back to a unicast sweep of the /24. Verified: a
  unicast query to port 5353 gets answered where multicast gets nothing.

## Testing

```sh
./tools/check.sh       # compile, lists, guards, journal, backup, web. All of it.
```

It must end with "All checks passed." It needs no phone: `tools/fake_adb.py`
runs the real `find`, `stat` and copy against a folder laid out like a phone,
and exits 99 on any adb call it does not recognise, so a new call in the code
shows up as a failing test rather than a silent pass.

Two lessons already paid for:

- **Test the page, not your assumption about it.** The web interface loaded
  unstyled in every real browser from 1.0.0 to 1.0.2, because the server
  wanted the token on `/static/` and a browser does not send one. It had been
  "verified" with curl with the token added by hand. `tools/test_web.py` now
  requests each asset exactly as `index.html` names it.
- **A fake phone proves the pipeline, not the phone.** Whether a given
  manufacturer lets ADB read contacts or messages can only be learned on that
  phone. The refusal path ("could not be read on this phone") exists for that
  and is tested; it must never write an empty export and call it a success.

Read-only commands, safe against any real device:

```sh
./freemyandroid info
./freemyandroid plan fast standard      # prints, changes nothing
./freemyandroid backup --check          # only looks
./freemyandroid battery
```

Checking that a guard fires is still the useful hand test:

```python
ctx.guard("com.android.systemui")   # -> "protected: ..."
ctx.guard(ctx.current_launcher)     # -> "this is the current home screen"
```

What has met real hardware: the engine, on a Samsung Galaxy M21 (Android 12),
written up in `docs/tested-devices/`. What has not: **the backup**, and
television support beyond detection. Say so wherever it comes up.

## Backup

- Saved as ordinary files under `~/FreeMyAndroid-Backups/<phone>-<serial>/`
  (`files/` mirrors the phone, `snapshots/<date>/` holds contacts, messages,
  calls, the app list and a manifest). `FMA_BACKUP_DIR` moves it.
- Incremental, and verified by size: a file counts as saved only when it is
  on this computer at the size the phone reported.
- Email, app data, Wi-Fi passwords and authenticator codes cannot be saved by
  anything on an unrooted phone. The interfaces say so outright, authenticator
  codes first, because that is the one that locks people out.
- **It looks around** (since 1.2.0). `survey()` lists the fixed groups in
  `FILE_GROUPS` and then everything else it finds: other top-level folders
  (`folder:<name>`), other apps' `Android/media` folders (`appmedia:<app>`)
  and loose files (`loose`). `Android/data` and `Android/obb` are not read.
  Hidden names and empty folders are not listed.
- **The owner chooses.** `run(only=…)` copies, checks and counts room for the
  chosen items alone; the rest are recorded as `skipped`, and the manifest
  carries `everything` and `chosen`. A backup of a selection must never be
  reported as a backup of everything, in any front end. An assistant looks
  with `backup --check --json`, then *asks*; AGENTS.md section 3½ is the
  procedure.
- File and folder names come from the phone and are untrusted, three ways:
  every path that reaches `d.shell()` goes through `_q` (1.1.0 used bare
  single quotes, and was safe only because it sent nothing but its own fixed
  folder names; discovered names would have broken out of them); anything
  written locally goes through `_safe_join`; and item keys are made
  shell-safe by `_key_part`, because people and assistants type them after
  `--only`. Labels are for showing, never for running.

## Things that are deliberately missing

- **A launcher of our own.** There are 22 good open-source launchers already,
  with measured memory figures in `data/apps.json` to choose between them. We
  install one and make it the default instead of writing a 23rd.
- **Shizuku or an on-device app.** This is a terminal tool. Canta already does
  the on-device job well and we link to it.
- **A generic shell escape hatch.** See above.
- **The web panel.** Designed in `docs/SERVER.md`, not built. Start there if
  you are picking up that thread.
- **Restore.** A backup can be put back by hand today (`docs/BACKUP.md`). An
  automatic restore is on the roadmap, after the backup has met real phones.
