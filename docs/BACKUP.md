# Save your things first

Free My Android never deletes your photos or contacts. It switches apps off,
and switching an app off leaves everything it stored exactly where it was.

A backup comes first anyway, because the phone in your hand is usually the
only copy of somebody's photographs, and "it should be fine" is not a plan.

```sh
./freemyandroid web          # step 3: one button, "Back up everything"
./freemyandroid              # menu: "Save your things first"
./freemyandroid backup       # or straight from the command line
./freemyandroid backup --check    # only look: everything found, and how much
```

Backing up **reads** the phone. It changes nothing on it.

## It looks first, and you choose

The backup does not work from a list of folders somebody wrote in advance.
It looks around the phone's storage and tells you what is actually there,
each thing by name with its size:

- the things every phone has: photos and videos, downloads, documents,
  music, WhatsApp, contacts, text messages, call history, your list of apps;
- **every other folder** at the top of the phone's storage — `Telegram`, a
  scanner app's folder, something you made yourself;
- **files other apps keep** in their own shared folder (`Android/media`);
- **loose files** that are in no folder at all.

Everything it finds is saved unless you say otherwise. Leaving something out
is always your decision, never a default.

**In the web interface**, "Back up everything" is still one click. Beside it
is "See what is on this phone first": it lists what was found with a tick
box on every line, all ticked, and a running total of what the ticked lines
add up to. Untick what you do not want, then "Back up what is ticked".

**In the terminal interface**, "Save your things first" shows the same list.
Everything starts selected and the cursor starts on "Back up what is
selected", so Enter straight away saves everything. Arrow up to a line and
press Space to leave it out.

**On the command line:**

```sh
./freemyandroid backup --check                  # the list, and nothing copied
./freemyandroid backup --only photos,contacts   # save just these
./freemyandroid backup --skip downloads         # save everything but these
```

`--check` ends each line with a word in brackets, like `[photos]` or
`[folder:Telegram]`. Those are the words `--only` and `--skip` take. A word
it does not recognise stops the command and lists the ones it does.
`--check --json` prints the same list for a program to read, which is how an
AI assistant looks before it asks you what to keep.

**A backup of some things is never called a backup of everything.** When you
leave something out, the summary shows it as "not chosen" — which is not the
same as "none found" — the record kept with the backup says it was a
selection, and the next time you open the tool it tells you the last backup
was only of what was chosen.

Three things it deliberately does not look at:

- **`Android/data` and `Android/obb`.** Modern Android does not let a
  computer read them, and what is in them is the inside of apps and game
  downloads, not things you made. See "Things inside apps" below.
- **Hidden folders and files** — the ones whose names start with a dot. Apps
  use them for caches and markers.
- **Empty folders.** They are not listed; there is nothing in them to save.

## The order to do things in

This is the process the interfaces walk you through, and the reason for each
step.

| | Step | Why it is here |
|---|---|---|
| 1 | **Connect the phone** | USB is simplest. Wi-Fi works on Android 11 and newer. |
| 2 | **Say how the phone is used** | A phone someone relies on gets cautious defaults. A freshly reset one gets everything. The tool cannot tell which it is. |
| 3 | **Back up** | One click. Before anything is switched off, not after. |
| 4 | **Look at the plan** | Every change is listed with what you would notice. Nothing has happened yet. |
| 5 | **Apply** | Each change is written down, with how to reverse it, as it is made. |
| 6 | **Restart and use the phone for a day** | Some things only show up in real use: a missing share option, a quiet notification. |
| 7 | **Undo anything you miss** | One button puts a whole run back. |

The web interface reminds you at step 5 if step 3 has not happened. It does
not block you — nothing here destroys data, so a hard stop would be theatre —
but it does say so.

### If the phone is being reset, sold or handed down

Do these **before** the factory reset, in this order. Two of them cannot be
done afterwards.

1. **Back up** (above).
2. **Move your authenticator codes.** Open Google Authenticator, Authy or your
   bank's app and use its own "transfer to a new phone" option. No backup tool
   can copy these — see below — and losing them locks you out of accounts.
3. **Check WhatsApp has made its own chat backup** (WhatsApp → Settings →
   Chats → Chat backup). The backup here copies that file, but only if
   WhatsApp has written one.
4. **Remove the Google account from the phone** (Settings → Accounts). If you
   reset a phone with the account still on it, Android's Factory Reset
   Protection locks it to that account, and the next owner cannot set it up.
5. Factory reset.
6. Run Free My Android on the fresh phone, choosing "freshly reset".

## What is saved

| | Where it ends up |
|---|---|
| Photos and videos (camera, screenshots, pictures, movies) | `files/DCIM`, `files/Pictures`, `files/Movies` |
| Downloads and documents | `files/Download`, `files/Documents` |
| Music and voice recordings | `files/Music`, `files/Recordings` |
| WhatsApp photos, videos and its chat-backup file | `files/Android/media/com.whatsapp/…` |
| Any other folder found at the top of the phone's storage | `files/<the same name>/…` |
| Files other apps keep in shared storage | `files/Android/media/<app>/…` |
| Loose files in no folder | `files/` itself |
| The same folders on an SD card, if there is one | `files/sd-card-XXXX-XXXX/…` |
| Contacts | `snapshots/<date>/contacts.vcf` |
| Text messages | `snapshots/<date>/messages.json` |
| Call history | `snapshots/<date>/calls.json` |
| The list of apps you installed | `snapshots/<date>/apps.txt` |

Everything lands in `~/FreeMyAndroid-Backups/<phone>/` — an ordinary folder
you can open, copy to a USB stick, or back up again somewhere else. Set
`FMA_BACKUP_DIR` to put it elsewhere. A `READ ME FIRST.txt` in the folder
explains what is there in plain words.

You do not need this program to use any of it. Photos are photos. The
contacts file is a standard vCard that every phone, Gmail, Outlook and iCloud
can import.

## What is not saved, and why

Said plainly, because a backup that quietly skips things is worse than none.

**Authenticator and banking app codes.** They are locked inside the app. Move
them from inside the app itself. This is the one that locks people out.

**Email.** Your email lives with your email provider, not on the phone. Sign
in anywhere and it is all there. There is nothing on the phone to copy.

**Things inside apps** — game progress, app settings, chats in apps other
than WhatsApp. Android does not let anything outside an app read its private
storage without root. The old `adb backup` command used to try; since
Android 12 nearly every app is excluded from it, so this tool does not
pretend.

**Wi-Fi passwords.** Not readable from a computer.

**Picture messages and contact photos.** The text of messages and the details
of contacts are saved; pictures attached to them are not.

## How it behaves

**It only copies what is new.** The first backup is the slow one. After that,
pressing the button again takes seconds, so press it as often as you like.

**It checks its own work.** A file counts as saved only when it is on your
computer at exactly the size the phone reported. Anything that could not be
saved is listed by name, and the backup is marked incomplete.

**It checks for room first.** If your computer does not have space for what
needs copying, it says so before starting rather than failing half-way. Only
what you chose is counted, so unticking the videos is a way to make it fit.

**It keeps a record of what was chosen.** Each backup writes a
`manifest.json` saying what was saved, what was not chosen, and what could
not be read. If something was left out, it says so there too.

**Odd folder names are copied, not obeyed.** Names on the phone can contain
quotes, semicolons and anything else. They are passed to the phone as names
and nothing more.

**It can be interrupted.** Unplug the phone mid-backup and whatever was copied
is kept. Run it again and it carries on.

**Some phones refuse.** A few manufacturers do not let a computer read
contacts or text messages over ADB. When that happens the summary says
"could not be read on this phone" and tells you what to do instead — the
Contacts app's own Export, or an app such as SMS Backup & Restore. It never
writes an empty file and calls it a success.

**Caches are skipped.** Thumbnail folders are rebuilt by the phone on its own
and can run to gigabytes.

## Putting things back

- **Photos, videos and files:** copy a folder from `files/` back to the phone
  with a cable, or with `adb push`.
- **Contacts:** copy `contacts.vcf` to the phone and open it, or import it in
  the Contacts app. Gmail and iCloud can import the same file.
- **Messages and calls:** these are kept as readable records. Putting text
  messages back into a phone needs an app on the phone; this tool does not do
  it yet.
- **Apps:** `apps.txt` has a link for each one.

An automatic restore is on the [roadmap](ROADMAP.md).

## How well tested this is

Honestly: the whole pipeline — looking around, choosing, copying, checking,
re-running, changed files, SD cards, awkward file names, folder names written
to break a shell, and a phone that refuses — is tested end to end on every
commit against a stand-in for `adb` (`tools/fake_adb.py`), which runs the
real shell commands against a folder laid out like a phone. The command line
and the web interface are tested the same way.

It has **not yet been run against a real phone**. The file-copying half uses
nothing but `find`, `stat` and `adb pull`, and should hold. Looking around
adds one more thing a real phone has to agree with: a short loop in the
phone's own shell that lists the top of its storage. On the stand-in that
loop runs in `bash`; a phone runs it in its own shell, and that has not been
tried. If a phone cannot do it, the summary shows "Other folders on the
phone — could not be read on this phone" and the usual folders are still
saved. Reading contacts and messages depends on the manufacturer, which is
exactly why the refusal path exists and is tested. If you run it, a line in
an issue saying which phone and what the summary showed is genuinely useful.
