# Changelog

Notable changes. Versions follow [semver](https://semver.org): the package
lists and the JSON data formats are part of the public surface, so a breaking
change to those is a major bump.

## 1.2.0 — 2026-11-18

### The backup looks around, and you choose

1.1.0 copied a fixed list of folders, all or nothing. A phone does not keep
to a list: there is a Telegram folder, whatever the scanner app left behind,
files in no folder at all. This release looks, reports, and asks.

- **It looks around the whole of the phone's storage.** As well as the
  familiar groups, the survey now reports every other top-level folder
  (`folder:Telegram`), other apps' folders under `Android/media`
  (`appmedia:org.telegram.messenger`) and loose files (`loose`), each with
  its file count and size and how much of it is new. The same on an SD card.
  `Android/data` and `Android/obb` are not read: modern Android does not
  allow it, and they hold app internals, not things the owner made. Hidden
  names and empty folders are not listed.
- **You choose what is saved, in all three front ends.** Everything is
  selected to begin with, so leaving something out is always a decision.
  - Web: "Back up everything" is still one click. Beside it, "See what is on
    this phone first" shows a checklist with a running total, and "Back up
    what is ticked".
  - Terminal: "Save your things first" lists what was found with Space to
    leave a line out. Enter straight away still backs up everything.
  - Command line: `backup --only photos,contacts` and
    `backup --skip downloads`. `backup --check` now ends each line with the
    key to use, like `[photos]`.
- **`backup --check --json`** prints one JSON object — the phone, where the
  backup would go, room on this computer, every item with its key, label,
  counts and sizes, and what cannot be saved. It is how an AI assistant looks
  before it asks. `AGENTS.md` section 3½ is rewritten around that: look, say
  what was found, say what cannot be saved, **ask what to keep**, back up,
  report. An assistant must never leave something out on its own.
- **A selection is never mistaken for everything.** Items left out are
  recorded as `skipped` and shown as "not chosen", distinctly from "none
  found". The manifest gains `everything` and `chosen`, and the interfaces
  say when the last backup was only of what was chosen. Room on the computer
  is counted for the chosen items only, and so is the checking afterwards.
- A key that does not exist — a typo after `--only`, or a request the web
  page was never shown — is refused with the list of valid ones, before
  anything is copied. So is an option `backup` does not know; 1.1.0 took it
  for a phone's serial number.

**Still not run against a real phone.** No device was attached for this
release either. Everything above is tested only against `tools/fake_adb.py`,
a stand-in for adb that runs the real shell commands against a folder laid
out like a phone. Looking around depends on a short loop in the phone's own
shell, which the stand-in runs in `bash`; that has not been tried on a
device. If a phone cannot do it, the summary says the other folders could
not be read and the usual ones are still saved.

### Fixed: paths sent to the phone's shell were not quoted properly

Since 1.1.0. Paths were put into shell commands wrapped in single quotes,
with nothing done about a single quote *inside* the path. A name containing
one — `Mum's photos` is enough — would have ended the quoting early: the
command fails and the folder looks empty, or the rest of the name is read by
the phone's shell as a command.

Plainly: 1.1.0 would have mis-handled a folder name with a single quote in
it, had one ever reached that code. None could. The only paths 1.1.0 put
into a shell command were its own fixed folder names (`DCIM`, `Download` and
so on); the names of the folders and files *inside* those came back as
output and were never sent. That was checked while writing this, by running
1.1.0 against the stand-in phone with folders named to break the quoting
placed inside `DCIM`: they were copied correctly and nothing was executed.
So 1.1.0 was safe by accident of what it was given, not by construction, and
looking around the phone removes the accident — every folder name on the
phone now goes into a command.

Every path that reaches the phone's shell now goes through one quoting
function. The test phone has top-level folders named `My 'stuff'; ls`,
`$(touch …)` and similar; they are copied like any other folder, and the
test fails if any of them is ever executed. With the quoting fix taken out,
it does fail.

Names from the phone are also kept out of two other places. The keys typed
after `--only` are built from letters, digits and `. _ : -` only, so a folder
with an awkward name gets a plain key and its real name stays in the label.
And labels have control characters replaced before they are printed.

### Also

- `tools/test_cli_backup.py` runs the backup command the way an assistant
  does, and checks that the phone is byte-for-byte unchanged afterwards.
- `tools/fake_adb.py` runs its shell inside the fixture, so anything a
  hostile name managed to execute would be seen by the test.
- A file whose folder cannot be created on this computer now costs that one
  file, listed by name, instead of stopping the backup.

### Fixed: Enter did not apply a plan in the terminal interface

Since 1.0.0. On the review screen the footer said "Enter  apply", and Enter
ticked or unticked the highlighted row instead. The list widget returned the
row under the cursor before the screen's own handler was asked, so the plan
could not be applied from the terminal interface. The same was true of
"install selected" on the apps screen and "turn off selected" on the by-hand
screen. The web interface was not affected.

It was found by reading the code while adding the backup checklist, not by a
user report, and nothing had caught it because nothing pressed a key.
`tools/test_tui.py` now drives the real widget in a pseudo-terminal: Enter
applies, Space ticks, Esc goes back. Putting the old code back makes it fail.

That test covers the key handling. It does not walk a whole screen against a
phone, and the review screen has still not been pressed through on real
hardware since this fix.

### Fixed: two backups in the same second shared one folder

Since 1.1.0. Each run's record goes in `snapshots/<date and time>/`, and the
name was only good to the second. Two runs that began in the same second
wrote into one folder, so a run where the phone refused contacts could sit
beside the previous run's `contacts.vcf` and look as if it had saved them.
A later run now gets its own folder (`…-2`).

This is the likely reason the checks had been failing on GitHub since 1.1.0
while passing on a laptop: the job logs could not be read from here, but the
collision is reproducible, and a fast runner is where two test runs would
share a second. The test now freezes the clock and forces it.

## 1.1.0 — 2026-11-18

### Save your things first

A backup step, in all three front ends, that comes before anything is
switched off. Nothing in this tool deletes data — a disabled app keeps
everything it stored — but the phone in someone's hand is usually the only
copy of their photographs, and that deserves better than "it should be fine".

- **One click in the web interface** (step 3, "Back up everything"), a menu
  item in the terminal one, and `./freemyandroid backup` on the command line.
  `backup --check` only looks and reports what would be copied.
- Saves photos, videos, downloads, documents, music, recordings, WhatsApp
  media and its chat-backup file, the same folders on an SD card, contacts as
  a standard `.vcf`, text messages, call history, and the list of installed
  apps. Into `~/FreeMyAndroid-Backups/`, as ordinary files that need no
  program to open.
- **Incremental**: a second run copies only what is new.
- **Verified**: a file counts as saved only when it is on this computer at
  the size the phone reported. Anything else is listed by name.
- **Says what it cannot save.** Authenticator codes, email, app data and
  Wi-Fi passwords are out of reach without root, and `adb backup` stopped
  working for nearly every app in Android 12. The summary says so, with the
  authenticator warning shown outright rather than folded away, because that
  is the one that locks people out of accounts.
- **Degrades out loud.** A phone that refuses the contacts or messages
  provider is reported as "could not be read on this phone", with what to do
  instead. It never writes an empty export and calls it a success.
- `docs/BACKUP.md` sets out the order to do things in, including the steps
  that must happen *before* a factory reset: moving authenticator codes, and
  removing the Google account so Factory Reset Protection does not lock the
  next owner out.

**Not yet run against a real phone.** No device was attached when this was
written. The pipeline is tested end to end on every commit against
`tools/fake_adb.py`, which runs the real shell commands against a folder laid
out like a phone. The file half should hold; reading contacts and messages
depends on the manufacturer.

### Fixed: the web interface loaded unstyled and inert in real browsers

Since 1.0.0. The server wanted the token on every request, and `index.html`
asks for `/static/style.css` and `/static/app.js` without one, so a browser
got a 403 for both and showed every section at once with nothing working.

It had been "verified" with `curl`, with the token appended to the static
URLs by hand, which tested an assumption instead of the page. It was found
the first time the page was opened in an actual browser and looked at.

The two static files are now served without the token, from an allowlist:
they are the same files anyone can read in the repository and hold no state.
The token still guards the page and every API call, which is where the
ability to change a phone lives.

`tools/test_web.py` now fetches the page, reads which assets *it* names, and
requests each exactly as written. Reintroducing the bug makes it fail.

### Also

- `tools/fake_adb.py`: a stand-in for adb, so pipelines can be tested with no
  hardware.
- The web test asserts that every element id the script reaches for exists in
  the page.
- Televisions are a supported device class, with their own package list and
  guards (38 entries; committed earlier, released here).

## 1.0.2 — 2026-11-08

Fixes a file that would not parse at all on Python 3.10, which is the floor
this project claims to support.

- `fma/catalog.py` had `r'\.apk$'` inside an f-string expression. PEP 701
  made that legal in 3.12; before 3.12 it is a `SyntaxError`. Development
  happened on 3.12, so it compiled locally and the module simply could not be
  imported on 3.10. Moved to a `DEFAULT_ASSET` constant.
- **`tools/check.sh` now catches this class of bug** on a new interpreter.
  `ast.parse(feature_version=(3, 10))` does not help, because the change is
  in the tokeniser rather than the grammar; `tokenize` does, because from
  3.12 the pieces of an f-string arrive as separate tokens, so a string
  literal holding a backslash inside an f-string is visible. Verified by
  reintroducing the bug and watching the check fail.

Worth recording that the 3.10 matrix job earned its place immediately: it was
added in 1.0.1 to fix a `setup-python` failure, and the first time it actually
ran it found this.

## 1.0.1 — 2026-11-08

A same-day fix release, because 1.0.0 shipped with failing CI.

- **Fixed a real bug** the CI caught and the release did not:
  `fma/plan.py` used `except AdbError` where the module is imported as `adb`,
  so setting a home screen on a device without `cmd role` would have raised
  `NameError` instead of being ignored.
- **`tools/check.sh`** — every check, in one command, run identically on a
  laptop and in CI. 1.0.0 was pushed after running *some* of the checks by
  hand, and the one that was skipped is the one that found the bug above.
  The workflow now just calls this script, so there is nothing to skip.
- Added a journal round-trip check covering all ten action kinds, and
  `com.samsung.android.networkstack` to the guard assertions.
- CI matrix fixed: Python 3.10 is pinned to `ubuntu-22.04`, because newer
  runner images no longer carry it and `setup-python` failed outright.
  `fail-fast: false` so one version failing no longer hides the other.
- `__pycache__` removed from the repository and added to `.gitignore`.
- `LICENSE` is now the unmodified MIT text so GitHub detects it; the
  attribution that used to sit inside it has moved to `NOTICE`.

## 1.0.0 — 2026-11-08

First release. Verified against a Samsung Galaxy M21 (Android 12, API 31).
See `docs/tested-devices/` for the full before-and-after report.

### The engine

- **Three profiles** — Fast, Elder, Developer/server — each at **three
  intensities**, Standard / Extreme / Ultra, where intensity is a risk ceiling
  (`safe` → `+caution` → `+risky`) rather than an adjective.
- **209 curated packages** over five lists: Google/AOSP, Samsung (One UI),
  Xiaomi (MIUI/HyperOS), ColorOS (OnePlus/OPPO/realme), and third-party
  preloads. Every entry carries what breaks without it.
- **Four guards**, applied at every intensity, Ultra included:
  1. 57 protected packages and 16 vendor prefixes, never touched.
  2. Role occupancy — the current dialer, SMS app, keyboard, browser, home
     screen or **payment app** is safe unless a replacement exists.
  3. A screen reader in use is never disabled automatically.
  4. Packages the owner disabled themselves are left alone.
- **`pm disable-user` only.** Nothing is ever uninstalled.
- **Undo journal** per phone on the operator's computer, recording each
  change's exact inverse at apply time, including for runs that failed
  half-way. Settings return to the value they actually had.

### Interfaces

- **Terminal interface** (curses, no dependencies).
- **Web interface** — `./freemyandroid web`. A decision maker and live
  progress view, for when the person deciding is not the person at the
  keyboard. Loopback-bound with a per-run token. Asks whether the phone is
  freshly reset or in use, and pre-selects accordingly.
- **Read-only command line** for scripting: `info`, `plan`, `battery`, `apps`,
  `scan`, `devices`, `undo`. None of it changes a phone.

### Getting connected

- **Own mDNS discovery**, because Debian and Ubuntu ship `adb` with mDNS
  compiled out and `adb mdns services` then returns an empty list rather than
  an error. Falls back to a **unicast sweep** of the subnet for the many home
  access points that drop multicast between wireless clients.
- **Fetches `adb` itself** from Google's published URLs, for Linux, macOS and
  Windows. `adb get`, `adb where`, `adb bundle <dir>`.

### Apps and defaults

- **32 sideloadable open-source apps** from two sources only: the official
  F-Droid repo, and a project's own GitHub releases. SHA-256 recorded,
  ABI-aware asset selection, `--offline-bundle` for blocked networks.
- **Sets default apps over ADB** — home screen, browser, SMS, dialer,
  keyboard — with the previous default recorded for Undo. Nobody has to pick
  up the phone.
- **Measured memory** for launchers, read off a real phone rather than
  claimed. The numbers are not what you would guess: Fossify Launcher is
  heavier than Samsung's own despite a 5 MB APK.

### Battery

- Ranks what is actually draining *this* phone from `batterystats`, the alarm
  stats, the job scheduler and the doze whitelist, cross-referenced against
  the curated lists so you can see which offenders are also safe to remove.
- Each reader reports missing data rather than guessing, so a thin report
  looks thin instead of being quietly wrong.

### Phone as a server

- The Developer profile turns off Android 12's **phantom process killer**
  (`settings_enable_monitor_phantom_procs`), which is the reason Termux
  servers die with nothing in any log, plus doze exemption and stay-awake.
- `docs/SERVER.md` covers all four routes honestly, including why real Docker
  cannot run on an unrooted phone.

### Known limits

- No test suite. This is the largest gap; see `docs/ROADMAP.md`.
- Windows: the TUI needs WSL because `curses` is not in the Windows standard
  library. The web interface works anywhere.
- No vivo, Motorola or Transsion package lists yet.
- The web panel for running services on the phone is designed, not built.

### Corrections made during development, kept here because they were wrong in
### public for a while

- Setting the default home screen **does** work over ADB
  (`cmd package set-home-activity`). Earlier versions said Android forbids it
  and told people to press Home and choose — which on One UI does not even
  offer a chooser, leaving them stuck on the stock launcher.
- Fossify Launcher was described as "the sane default replacement". Measured,
  it uses 159 MB against One UI Home's 111 MB.
- `com.samsung.android.networkstack` had a note reading "Never" and was not
  actually protected. Prose is not a guard.
- Role holders were resolved from a preference-ordered candidate list, so
  after installing a new launcher the guard still named the *stock* launcher
  as "the current launcher" and refused to disable it.
