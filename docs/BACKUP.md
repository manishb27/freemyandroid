# Save your things first

Free My Android never deletes your photos or contacts. It switches apps off,
and switching an app off leaves everything it stored exactly where it was.

A backup comes first anyway, because the phone in your hand is usually the
only copy of somebody's photographs, and "it should be fine" is not a plan.

```sh
./freemyandroid web          # step 3: one button, "Back up everything"
./freemyandroid              # menu: "Save your things first"
./freemyandroid backup       # or straight from the command line
./freemyandroid backup --check    # only look: what would be copied, how much
```

Backing up **reads** the phone. It changes nothing on it.

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
needs copying, it says so before starting rather than failing half-way.

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

Honestly: the whole pipeline — looking, copying, checking, re-running,
changed files, SD cards, awkward file names, and a phone that refuses — is
tested end to end on every commit against a stand-in for `adb`
(`tools/fake_adb.py`), which runs the real shell commands against a folder
laid out like a phone.

It has **not yet been run against a real phone**. The file-copying half uses
nothing but `find`, `stat` and `adb pull`, and should hold. Reading contacts
and messages depends on the manufacturer, which is exactly why the refusal
path exists and is tested. If you run it, a line in an issue saying which
phone and what the summary showed is genuinely useful.
