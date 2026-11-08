# Samsung Galaxy M21 (SM-M215F)

Indian retail unit, used as a daily phone. Run end to end on 2026-11-08 with
Free My Android 1.0.x over USB.

```
Samsung SM-M215F
  Android 12 (API 31), build SP1A.210812.016.M215FXXS3CWK1
  manufacturer samsung (list: samsung)
  3570 MB RAM, ABI arm64-v8a
  369 packages, 1 disabled
  Linux VM (AVF): unsupported
  wireless debugging supported: True
  phantom process killer present: True
  home screen: com.sec.android.app.launcher
```

**Result: 71 packages disabled, 0 failures, roughly 110 MB of memory freed,
nothing broken.** Everything is reversible from the journal.

---

## What was done, in order

| Step | Action | Changes | Failures |
|---|---|---|---|
| 1 | `fast/extreme` profile | 70 | 0 |
| 2 | Benchmarked 6 launchers, installed Neo Launcher + Fossify Gallery | 2 installs | 0 |
| 3 | Made Neo Launcher the home screen over ADB | 1 | 0 |
| 4 | Disabled One UI Home and the Google app | 2 | 0 |
| 5 | Disabled Samsung Gallery | 1 | 0 |

Packages went from **369 installed / 1 disabled** to **371 installed / 71
disabled** — the two extra installs being the launcher and the gallery.

## Step 1 — `fast/extreme`

70 changes, 67 package disables and 3 animation settings, **70 of 70 applied
with no failures**. The interesting part is less the count than what an Indian
retail Samsung actually carries:

**Ad and telemetry frameworks with no icon.** Four Facebook packages
(`appmanager`, `services`, `system`, `katana`) on a phone where Facebook was
never opened. `com.aura.oobe.samsung` — ironSource's "recommended apps"
installer, which keeps running after setup and was **exempt from battery
optimisation**. `com.sec.android.diagmonagent`, Samsung's log uploader.
`com.samsung.android.rubin.app`, its on-device behaviour profiling.

**Paid-for preloads.** Netflix plus a separate `netflix.partner.activation`
stub that exists only to claim a trial. Dream11 (real-money fantasy sports),
Uber, ZestMoney (consumer lending), Outlook, OneDrive, Opera Max, and two
games — `com.block.juggle` and
`com.bubbleshooter.popbubbles.collectcards`.

**Samsung's own promotional surface.** My Galaxy, Samsung Shop India, Samsung
Discover, Galaxy Themes, Update Center, and Dynamic Lock screen, which
downloads rotating lock-screen wallpapers over the network.

20 of the 38 apps with an icon were **not in any curated list** when the run
started. They were read off this phone and added, which is where 62 of the
project's 209 package entries came from.

## Step 2 — launchers, measured rather than assumed

Six launchers installed, launched, and their `dumpsys meminfo` total PSS read:

| Launcher | PSS | APK |
|---|---|---|
| KISS | **17.5 MB** | 2.2 MB |
| YAM Launcher | 20.3 MB | 2.9 MB |
| Olauncher | 47.0 MB | 2.5 MB |
| **Neo Launcher** (chosen) | **61.0 MB** | 23.3 MB |
| µLauncher | 71.2 MB | 4.1 MB |
| Lawnchair 15.0.0-beta3 | 79.8 MB | 17.3 MB |
| **One UI Home** (stock) | **111–123 MB** | — |
| Fossify Launcher | **159.5 MB** | 5.2 MB |

Two results worth stating plainly, because both contradict what the project's
own README said before this run:

- **Fossify Launcher uses more memory than Samsung's own launcher** — 159 MB
  against 111 MB — despite a 5 MB APK. It had been described as "the sane
  default replacement for an OEM launcher". That claim was withdrawn.
- **µLauncher's 4 MB APK uses three times KISS's memory.** APK size predicts
  runtime memory badly enough to be useless.

Neo Launcher was chosen over the lighter options because a Nova-style grid,
dock and app drawer was what was wanted, and over Lawnchair because it
measured 19 MB lighter and ships tagged stable releases where Lawnchair 15 is
still beta. The other five were uninstalled: two launchers installed at once
means Android asks which one you meant every time you press Home.

**Caveat on the method.** The stock launcher was measured while it *was* Home;
the others were not, and a launcher holding Home does more work. Neo Launcher
measured 61 MB as an app and **81 MB once it was actually the home screen**.
Treat the table as a ranking, not a budget.

Galleries, same method: **Fossify Gallery 32.2 MB** against **Samsung Gallery
42.5 MB**, with folders, an editor, favourites, a recycle bin and hidden
folders, and no cloud or ads.

## Steps 3–5 — the memory win

`cmd package set-home-activity` made Neo Launcher the home screen from the
laptop. That is what unlocked the two biggest items, because the Google search
app is only risky to remove *while a stock launcher embeds it*:

| Process | PSS before |
|---|---|
| `com.sec.android.app.launcher` | 123.2 MB |
| `com.google.android.googlequicksearchbox:search` | 84.6 MB |
| `com.google.android.googlequicksearchbox:interactor` | 27.5 MB |

Measured immediately before and after disabling those two:

| | Before | After | Change |
|---|---|---|---|
| `MemFree` | 104.2 MB | **189.7 MB** | **+85.4 MB** |
| `MemAvailable` | 1137.1 MB | **1248.5 MB** | **+111.4 MB** |

**How to read that.** `MemFree` and `MemAvailable` on Android drift by hundreds
of megabytes on their own as the system caches and evicts — earlier the same
day this phone reported 403 MB free. The only honest reading is a paired
before/after taken minutes apart around a single change, which is what the
table above is. The per-process PSS figures are the firmer evidence.

Top processes afterwards, with both former entries gone:

```
327,823K  system
300,800K  com.android.systemui
154,254K  com.google.android.gms
120,232K  surfaceflinger
102,490K  com.android.vending
 80,806K  com.saggitt.omega          <- Neo Launcher, as Home
 71,075K  com.samsung.android.honeyboard
 70,743K  com.android.settings
```

## What the guards refused, and whether they were right

| Package | Refused because | Right? |
|---|---|---|
| `com.phonepe.app` | current `banking_upi` holder | **Yes.** `fast/extreme` had planned to disable it. On an Indian phone that is a live UPI app, and you would discover it at a shop counter. |
| `com.android.chrome` | current browser | Yes — no other browser was installed, so links from every app would have gone nowhere. |
| `com.google.android.gms` | protected | Yes. |
| `com.sec.android.app.launcher` | current home screen | Yes, until Neo Launcher replaced it; then correctly released. |
| `com.google.android.googlequicksearchbox` | above the Extreme ceiling | Yes at that tier; disabled deliberately later, once safe. |

Verified individually: `com.android.systemui` → protected,
`com.qualcomm.*` → vendor prefix, `com.samsung.android.dialer` /
`.messaging` / `.honeyboard` → role occupancy,
`com.google.android.marvin.talkback` → accessibility. All 16 roles resolved
correctly against what was actually installed.

## What broke

**Nothing.** Checked after each step: the phone booted, Home opened Neo
Launcher, and dialer, messages, keyboard, Play Store, Play Services and
PhonePe were all still enabled. `image/jpeg` resolved to Fossify Gallery with
`isDefault=true` after Samsung Gallery was disabled, so photos still open.

Two things that are gone and might be missed, both one Undo away:

- **FM Radio** (`com.sec.android.app.fm`) — works without mobile data and
  needs wired headphones as an aerial.
- **YouTube** — the website works in any browser, and NewPipe is in the
  catalogue.

## Bugs this device found

Every one of these was found by running against this phone, and none by
reading the code:

| Bug | Why it mattered |
|---|---|
| `com.samsung.android.networkstack` was **allowed** despite a note reading "Wi-Fi and mobile data. Never." | Prose is not a guard. Network stacks are now protected by prefix. |
| `banking_upi` was a role in the data but not in the protected-role list | `fast/extreme` planned to disable PhonePe. |
| Android 12 writes `UID u0a119:` with a **capital** UID | A case-sensitive regex silently dropped every mAh figure *while still reporting `batterystats` as a working source*. |
| Wake-lock names pass any package regex | `IpReachabilityMonitor.wlan0` was being reported as an app. |
| Role holders resolved from a preference-ordered list | After installing a new launcher the guard still named the *stock* launcher "the current launcher", protecting the one package most wanted gone. |
| `install` timeout of 300 s | A 17 MB APK on this phone's flash takes minutes; the timeout looked exactly like a hang. |
| GitHub `releases/latest` excludes pre-releases | Lawnchair only tags betas, so the catalogue fetched **version 1.2 from 2020** and `pm install` hung on it. |
| `cmd package set-home-activity` **works** | The project had claimed Android forbids setting Home over ADB and told people to press Home and choose — which on One UI does not even offer a chooser. |

## Journal

```
written_by: free-my-android 1.0.0
  1794121433-1  fast/extreme                     70 actions
  1794126311-2  memory: launcher + google app     2 actions
  1794126570-3  switch off Samsung Gallery        1 action
```

In `~/.local/share/freemyandroid/journal/RZ8N61GHTFB.json`. All reversible
from either interface; settings return to the value they actually had.

## Not exercised on this device

- The `elder` and `dev` profiles were planned and reviewed but not applied.
- Wireless ADB. This run was over USB. The phone was not advertising over
  mDNS at the time, so pairing and wireless discovery were not exercised
  here.
- The battery report ran with all five sources reporting, but the phone had
  recently been charged, so the drain figures were small and the ranking was
  dominated by alarm and job counts rather than mAh.
