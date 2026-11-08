# Free My Android — guide for AI assistants

**Version 1.0.0.** MIT. Python 3.10+, standard library only, no build step.

This file is enough to get you running. [CLAUDE.md](CLAUDE.md) is the deeper
guide for changing the code; read it before you edit anything.

You are probably here because someone said *"debloat my phone"*. They likely
do not know what a terminal is. Explain each step plainly, and **ask before
anything that changes their phone.**

---

## The one thing to understand first

This tool disables packages on a phone someone depends on. Everything is built
around that being reversible and consented to:

- **`pm disable-user`, never `pm uninstall`.** The APK stays on disk.
- **Four guards** refuse to touch anything that would brick the phone or lock
  the owner out. They apply at every intensity. Do not work around them.
- **Every change is journalled** with its exact inverse, on the operator's
  computer, so Undo always works.

If you are about to suggest relaxing a guard, the suggestion is wrong.

---

## 1. Check the environment

```sh
./freemyandroid --version          # version, python, which adb
```

Needs Python 3.10+. If `adb` is missing, the tool fetches it from Google:

```sh
./freemyandroid adb get            # platform-tools for this computer
./freemyandroid adb where          # which adb is in use
./freemyandroid adb bundle ~/pt    # all three OSes, for a USB stick
```

Or point it at an existing one: `FMA_ADB=/path/to/adb ./freemyandroid`

## 2. Attach a phone

```sh
./freemyandroid devices            # what is connected
./freemyandroid scan               # find phones on this network
```

**USB.** Settings → About phone → tap *Build number* ×7 → Developer options →
*USB debugging*. Plug in, unlock, accept the prompt. A phone showing as
`unauthorized` means the prompt has not been accepted — the driver is fine.

**Wi-Fi** (Android 11+). Developer options → *Wireless debugging* → on. Then
pair, from either interface. Three things that waste everyone's afternoon,
worth telling the user up front:

1. The **pairing port and the connect port are different numbers.** The
   address under *Pair device with pairing code* is not the one on the main
   screen.
2. Android **stops advertising over mDNS when the screen sleeps.** Keep the
   Wireless debugging screen open.
3. The **pairing code expires**, and a new one is issued each time that dialog
   opens.

Do not rely on `adb mdns services` — Debian and Ubuntu ship adb with mDNS
compiled out and it returns an empty list rather than an error. This project
does its own discovery (`fma/discover.py`), including a unicast sweep for the
many home routers that drop multicast between wireless clients.

## 3. Look before you touch

Everything on the command line is **read-only**. None of it changes a phone.

```sh
./freemyandroid info                      # model, android, roles, launcher, AVF
./freemyandroid plan fast standard        # prints a plan, changes nothing
./freemyandroid plan elder ultra
./freemyandroid battery                   # what is actually draining power
./freemyandroid apps                      # the sideload catalogue
./freemyandroid undo                      # runs recorded for this phone
```

Show the user the `plan` output before applying anything. It lists every
change with its risk and what breaks, plus what the guards refused and why.

## 4. Apply — pick an interface

Applying requires a human click. There are two front ends over the same
engine; neither can plan anything the other could not.

### TUI — you and the user are at the same keyboard

```sh
./freemyandroid
```

Arrow keys and Enter do everything, the keys you need are written at the
bottom, Esc leaves any screen. Flow: choose phone → how it is used → profile →
intensity → review with checkboxes → Apply.

**Do not try to drive this from a script.** It is curses; it needs a real
terminal. If you are an agent working through a shell, use the web interface.

### GUI — the user decides in a browser

```sh
./freemyandroid web
#   http://127.0.0.1:8777/?t=<random-token>
```

**This is the right one for an agent.** Start it in the background, read the
URL from its output, and give the user the link. They tick what they want and
watch progress stream line by line. Startup output is line-buffered, so the
URL appears immediately:

```sh
(setsid nohup ./freemyandroid web --no-browser > /tmp/fma-web.log 2>&1 &)
sleep 4; cat /tmp/fma-web.log
```

Options: `--port N`, `--bind HOST`, `--lan`, `--no-browser`.

Bound to loopback with a per-run token on every request. **Do not use `--lan`
without being asked** — it makes a page that can disable apps reachable from
the whole network.

The page asks one question you cannot answer for the user:

| | Pre-selects |
|---|---|
| **Freshly reset phone** | everything, including risky |
| **Phone someone is using** | only changes nobody notices |

## 5. The three profiles

| | What it does |
|---|---|
| **Fast** | Strip ads, analytics and paid-for preloads. Same capability, less running. |
| **Elder** | Keep a few large obvious things, hide the rest. Bigger text, longer timeout, slower long-press. |
| **Developer / server** | Keep every capability; switch off what Android does to kill background work, so the phone can hold a service up. |

Each runs at **Standard / Extreme / Ultra**, which is a risk ceiling, not an
adjective: `safe` → `+caution` → `+risky`. Ultra asks twice and makes the user
type `apply`.

## 6. Installing apps and setting defaults

```sh
./freemyandroid apps                       # the catalogue
./freemyandroid bundle ~/apks termux       # pre-download, for blocked networks
```

Only two sources are allowed, and this is the security model: **the official
F-Droid repo** and **a project's own GitHub releases**. Never add a mirror or
an APK-download site; those serve repackaged binaries and many ISPs sinkhole
them, so a download that succeeds from one is worse than one that fails.

Anything with a `fills_role` is **made the default automatically** — the
launcher, browser, SMS app, dialer or keyboard. That works over plain ADB
without root, and the previous default is recorded so Undo restores it. Do not
tell users to go and tap through Settings; earlier versions of this project
did, and it was wrong.

There is no "default gallery" role in Android, so a gallery is installed and
then offered when an image is opened.

## 7. Undo

```sh
./freemyandroid undo                       # list runs
```

Reverse one from either interface. The journal lives on the operator's
computer:

```
~/.local/share/freemyandroid/journal/<serial>.json
```

Settings return to the value they actually had, not a guessed default.
Packages are re-enabled. Installed apps stay installed — removing an app is a
decision, not a rollback. Half-finished runs are recorded too, which is
exactly when Undo matters most.

---

## Rules for you, specifically

1. **Never add `pm uninstall`.** Anywhere.
2. **Never weaken the guards** in `fma/profiles.py` or `data/protected.json`
   to make a profile look more impressive.
3. **A package entry without a `note` saying what breaks is not finished.**
   CI fails on `caution`/`risky` entries with no note.
4. **Never add a generic `shell` action kind** that takes a command from a
   JSON data file. That is remote code execution dressed as a feature. Add a
   narrow kind, the way `doze_allow` is narrow.
5. **Do not guess at `dumpsys` output.** If a parser does not recognise the
   format, it must report nothing via `Report.missing` rather than invent a
   number. Three parser bugs in this repo were found only by running against
   a real phone.
6. **Say which device you tested on.** There is no test suite yet; that is the
   biggest gap in the repo. See [docs/ROADMAP.md](docs/ROADMAP.md).

## Checks before you commit

```sh
./tools/check.sh                           # everything CI runs. Run this.
./freemyandroid plan fast standard         # read-only, safe on any phone
```

`tools/check.sh` is exactly what CI runs — the workflow just calls it. Do not
reproduce a subset of it by hand: version 1.0.0 shipped with red CI because
that is precisely what happened, and the check that got skipped was the one
that found a real bug.

## Where things live

```
freemyandroid          entry point; subcommands print, never change a phone
fma/adb.py             every call to adb; the only place a shell string is built
fma/discover.py        DNS-SD, with a unicast sweep for hostile Wi-Fi
fma/device.py          what kind of phone; roles, launchers, AVF support
fma/profiles.py        profile + intensity -> Plan. The guards live here
fma/plan.py            Action, apply(), invert(), make_default()
fma/journal.py         the undo record, one file per phone
fma/battery.py         dumpsys parsing and the ranking
fma/catalog.py         fetching APKs from F-Droid and GitHub releases
fma/platform_tools.py  fetching adb itself
fma/tui.py  fma/app.py the terminal interface
fma/web.py  web/       the web interface
data/                  209 curated packages, 3 profiles, guards, roles, apps
docs/ADB.md            per-OS adb setup, including the Windows driver mess
docs/SERVER.md         running services on a phone; why Docker cannot
docs/ROADMAP.md        what needs doing, worst gap first
```

## Known limits, stated plainly

- **No test suite.** `tools/check.sh` covers data, guards and syntax, but
  nothing exercises a real device. Verified by hand against a Samsung Galaxy
  M21 (Android 12); see `docs/tested-devices/`.
- **Windows untested.** `curses` is not in the Windows standard library, so
  the TUI needs WSL there. The **web interface works anywhere.**
- Vendor lists cover Google/AOSP, Samsung, Xiaomi, ColorOS
  (OnePlus/OPPO/realme) and third-party preloads. vivo, Motorola and
  Transsion are missing.
- `verify_signature()` only works if `apksigner` happens to be installed; it
  does not pretend otherwise.
