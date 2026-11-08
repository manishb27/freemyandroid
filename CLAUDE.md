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
data/packages/*.json   the curated lists, 179 entries over 5 files
data/profiles/*.json   fast, elder, dev — each with 3 intensities
data/protected.json    the hard guard. 57 packages, 11 prefixes
data/roles.json        19 roles: what a phone needs to still be a phone
data/apps.json         29 sideloadable open-source apps
```

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

There is no test suite yet, which is the biggest gap in the repo. Until there
is, verify against a real device and say which one:

```sh
./freemyandroid info
./freemyandroid plan fast standard      # prints, changes nothing
./freemyandroid plan fast ultra
./freemyandroid battery
python3 -m py_compile fma/*.py freemyandroid
```

`plan` and `battery` are read-only and safe to run against any phone. Checking
that a guard fires is the useful test:

```python
ctx.guard("com.android.systemui")   # -> "protected: ..."
ctx.guard(ctx.current_launcher)     # -> "this is the current home screen"
```

## Things that are deliberately missing

- **A launcher of our own.** Free My TV ships one because a TV has nowhere
  else to go. A phone has 22 good open-source launchers; we install one of
  those instead.
- **Shizuku or an on-device app.** This is a terminal tool. Canta already does
  the on-device job well and we link to it.
- **A generic shell escape hatch.** See above.
- **The web panel.** Designed in `docs/SERVER.md`, not built. Start there if
  you are picking up that thread.
