# Changelog

Notable changes. Versions follow [semver](https://semver.org): the package
lists and the JSON data formats are part of the public surface, so a breaking
change to those is a major bump.

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

First release. Verified against a Samsung Galaxy M21 (Android 12, API 31) and
a OnePlus Android TV (Android 11, API 30).

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
