"""Saving what matters off the phone, before anything else happens.

Nothing in this module changes the phone. It only reads, and everything it
reads lands in an ordinary folder on this computer that the owner can open,
copy to a USB stick, or restore from by hand.

The honest scope, because a backup that overstates itself is worse than no
backup:

  saved      photos, videos, downloads, documents, music, recordings,
             WhatsApp media and WhatsApp's own chat-backup files,
             every other folder found in the phone's shared storage,
             contacts (as a standard .vcf), text messages, call history,
             and the list of apps the owner installed.

  not saved  email (it lives on the mail server, not on the phone), app data
             and game saves, Wi-Fi passwords, authenticator codes. Android
             does not let anything read those without root, and `adb backup`
             stopped working for nearly every app in Android 12. This module
             says so in the summary rather than leaving a silent gap,
             because the authenticator one in particular locks people out.

Design rules:

  * Incremental. A second run copies only what is new, so "back up" can be
    pressed as often as someone likes and the first run is the only slow one.
  * Verified. A file counts as saved only when it exists on this computer at
    the size the phone reported. Anything else is listed by name.
  * Degrades out loud. Some phones refuse the contacts or messages provider
    to ADB. That is reported as "could not be read on this phone", with what
    to do instead — never as an empty success.
  * Looks first, and the owner chooses. `survey()` reports everything it
    found, including folders this module has never heard of, and `run()`
    takes `only=` so a person can leave things out. A backup of a selection
    says so in its manifest; it must never pass for a backup of everything.
  * Names from the phone are not trusted. Every path that reaches the phone's
    shell goes through `_q`, every path written here through `_safe_join`,
    and the keys people type are never the raw folder name.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Iterable

from . import adb

# --- what gets saved --------------------------------------------------------

# (key, plain label, folders on the phone). Paths are relative to shared
# storage; the same list is applied to an SD card if there is one, because a
# lot of budget phones save the camera roll there.
#
# These are the folders worth a plain name. Everything else in shared storage
# is found by looking (see `survey`) and listed under the name it has on the
# phone, so a folder this list has never heard of is still offered.
FILE_GROUPS: list[tuple[str, str, list[str]]] = [
    ("photos", "Photos and videos", ["DCIM", "Pictures", "Movies"]),
    ("downloads", "Downloads", ["Download"]),
    ("documents", "Documents", ["Documents"]),
    ("audio", "Music and recordings", ["Music", "Recordings", "Audiobooks"]),
    ("whatsapp", "WhatsApp photos, videos and chat backup",
     ["Android/media/com.whatsapp/WhatsApp", "WhatsApp",
      "Android/media/com.whatsapp.w4b/WhatsApp Business"]),
]

# Caches the phone rebuilds on its own. Copying them wastes gigabytes.
SKIP_PARTS = ("/.thumbnails/", "/.thumbnail/", "/.cache/", "/.trash/")

# Said to the owner in plain words, in every summary. Order matters: the one
# that locks people out of accounts goes first.
NOT_SAVED: list[tuple[str, str]] = [
    ("Authenticator and banking app codes",
     "Apps like Google Authenticator keep their codes locked inside the app. "
     "Move them to your new phone from inside the app itself before you "
     "reset or give away this one."),
    ("Email",
     "Your email lives with your email provider, not on the phone. Sign in "
     "again anywhere and it is all there. There is nothing here to copy."),
    ("Things inside apps",
     "Game progress, app settings and chats inside apps other than WhatsApp "
     "cannot be copied off an Android phone from outside. Use each app's own "
     "backup or sign-in."),
    ("Wi-Fi passwords",
     "Android does not let these be read from a computer."),
    ("Picture messages (MMS) and contact photos",
     "Text messages and contact details are saved; pictures attached to "
     "them are not."),
]

# Why a provider that refuses ADB cannot be saved, and what to do instead.
UNREADABLE: dict[str, str] = {
    "contacts": "This phone would not let contacts be read from a computer. "
                "Open the Contacts app → Settings → Export, and save the "
                ".vcf file it makes.",
    "messages": "This phone would not let text messages be read from a "
                "computer. An app such as SMS Backup & Restore can save them "
                "from the phone itself.",
    "calls": "This phone would not let call history be read from a computer.",
}

SD_CARD_RE = re.compile(r"^[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}$")


class BackupError(RuntimeError):
    pass


# --- data -------------------------------------------------------------------


@dataclass
class RemoteFile:
    path: str           # full path on the phone
    size: int
    mtime: int
    rel: str            # where it goes under <backup>/files/


@dataclass
class Item:
    key: str
    label: str
    kind: str                       # files | contacts | messages | calls | apps
    files: int = 0                  # for kind == files
    bytes: int = 0
    new_files: int = 0              # not on this computer yet
    new_bytes: int = 0
    count: int = 0                  # records, for the other kinds
    status: str = "pending"         # ok | partial | empty | unreadable | skipped
    detail: str = ""
    discovered: bool = False        # found by looking, not one of FILE_GROUPS
    failed: list[str] = field(default_factory=list)
    remote: list[RemoteFile] = field(default_factory=list, repr=False)

    @property
    def summary(self) -> str:
        """What the survey found, for showing before anything is copied."""
        if self.status == "unreadable":
            return "could not be read on this phone"
        if self.kind == "files":
            if not self.files:
                return "nothing found"
            s = f"{count_files(self.files)}, {human(self.bytes)}"
            if self.new_files != self.files:
                s += (f" — {self.new_files:,} new ({human(self.new_bytes)})"
                      if self.new_files else " — all already saved")
            return s
        return f"{self.count:,}" if self.count else "none found"

    @property
    def outcome(self) -> str:
        """What became of it, for showing once a backup has run. "not chosen"
        and "none found" are different facts and must not look alike."""
        if self.status == "skipped":
            return "not chosen"
        if self.status == "unreadable":
            return "could not be read on this phone"
        if self.kind == "files" and self.files:
            return f"{count_files(self.files)}, {human(self.bytes)}"
        return f"{self.count:,}" if self.count else "none found"


@dataclass
class Survey:
    """What is on the phone and what a backup would copy. Read-only."""
    dest: str
    items: list[Item] = field(default_factory=list)
    sd_cards: list[str] = field(default_factory=list)

    @property
    def new_bytes(self) -> int:
        return sum(i.new_bytes for i in self.items)

    @property
    def new_files(self) -> int:
        return sum(i.new_files for i in self.items)

    @property
    def total_bytes(self) -> int:
        return sum(i.bytes for i in self.items)

    @property
    def total_files(self) -> int:
        return sum(i.files for i in self.items)

    def item(self, key: str) -> Item | None:
        return next((i for i in self.items if i.key == key), None)

    @property
    def keys(self) -> list[str]:
        return [i.key for i in self.items]

    def chosen(self, only: Iterable[str] | None) -> list[Item]:
        """The items a person picked; all of them when `only` is None.

        A key that is not in this survey is refused rather than ignored: a
        typo that quietly backed up nothing would look like a success.
        """
        if only is None:
            return list(self.items)
        want = set(only)
        unknown = sorted(want - set(self.keys))
        if unknown:
            raise BackupError(
                "There is nothing called " + ", ".join(repr(k) for k in unknown)
                + " to back up on this phone. The choices are: "
                + ", ".join(self.keys) + ".")
        return [i for i in self.items if i.key in want]


@dataclass
class Result:
    dest: str
    snapshot: str
    items: list[Item]
    started: float
    finished: float
    copied_files: int = 0
    copied_bytes: int = 0
    failed_files: int = 0
    chosen: list[str] = field(default_factory=list)   # keys that were asked for
    everything: bool = True         # False when the owner left something out

    @property
    def ok(self) -> bool:
        """Every file that was asked for is here. Says nothing about what was
        not chosen; `everything` is the answer to that."""
        return self.failed_files == 0

    @property
    def seconds(self) -> float:
        return self.finished - self.started


# --- helpers ----------------------------------------------------------------


def count_files(n: int) -> str:
    return f"{n:,} file" if n == 1 else f"{n:,} files"


def human(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("bytes", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def backup_root() -> str:
    """Where backups live. A visible folder in the home directory on purpose:
    this is somebody's photographs, not application state to hide in
    ~/.local/share."""
    return os.environ.get("FMA_BACKUP_DIR") or os.path.join(
        os.path.expanduser("~"), "FreeMyAndroid-Backups")


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-") or "phone"


def device_dir(name: str, serial: str, root: str | None = None) -> str:
    return os.path.join(root or backup_root(), f"{_slug(name)}-{_slug(serial)}")


def free_bytes(dest: str) -> int:
    """Room left where the backup would go. The folder may not exist yet, so
    this asks about the nearest one above it that does."""
    path = os.path.abspath(dest)
    while not os.path.isdir(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def _q(path: str) -> str:
    """Quote a path for the phone's shell.

    Folder and file names come from the phone, so they are input we did not
    write. Inside single quotes a shell takes every character literally
    except the single quote itself, so that one is closed, escaped and
    reopened. 1.1.0 wrapped paths in quotes without doing this. It got away
    with it because the only paths it sent were its own fixed folder names;
    now that every folder name on the phone is sent, an apostrophe in one
    would end the quoting early.

    Every path that reaches `d.shell()` in this module goes through here.
    """
    return "'" + path.replace("'", "'\\''") + "'"


def _key_part(name: str) -> str:
    """The part of an item key that stands for a folder name.

    Keys get typed after `--only`, by people and by assistants composing a
    command line, so they must be safe to paste into a shell unquoted. A
    plain name is used as it is. Anything else is reduced to its plain
    characters plus a few digits of a hash of the real name, which keeps two
    awkward names apart and gives the same key on every run.
    """
    if name and re.fullmatch(r"[A-Za-z0-9._-]+", name):
        return name
    digest = hashlib.sha1(name.encode("utf-8", "replace")).hexdigest()[:6]
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")[:40]
    return f"{slug}-{digest}" if slug else digest


def _plain(name: str) -> str:
    """A name from the phone, made safe to print: control characters and
    invisible direction marks could rewrite a terminal line or hide text."""
    return "".join(c if c.isprintable() else "?" for c in name)


def _safe_join(base: str, rel: str) -> str | None:
    """Join a path that came from the phone. A file name is input we did not
    write, so a `..` in one must not be able to climb out of the backup."""
    rel = rel.lstrip("/")
    if any(part in ("..", "") for part in rel.split("/")):
        return None
    out = os.path.normpath(os.path.join(base, rel))
    base_abs = os.path.abspath(base)
    if os.path.commonpath([os.path.abspath(out), base_abs]) != base_abs:
        return None
    return out


# --- reading the phone ------------------------------------------------------


def sd_cards(d: adb.Device) -> list[str]:
    r = d.shell("ls /storage 2>/dev/null", timeout=20)
    return [name for name in r.out.split() if SD_CARD_RE.match(name)]


def list_files(d: adb.Device, root: str, rel_prefix: str,
               probe: bool = True) -> list[RemoteFile] | None:
    """Every file under `root` on the phone, with size and modified time.

    Returns None when the folder exists but could not be listed (very old
    Android without `stat -c`), which the caller reports rather than treating
    as empty. A folder that simply is not there returns [].

    `probe=False` skips asking whether the folder exists, for a folder the
    phone has just told us about.
    """
    if probe:
        there = d.shell(f"[ -d {_q(root)} ] && echo yes || echo no", timeout=20)
        if "yes" not in there.out:
            return []
    r = d.shell(
        f"find {_q(root)} -type f -exec stat -c '%s|%Y|%n' {{}} + 2>/dev/null",
        timeout=600)
    out: list[RemoteFile] = []
    for line in r.out.splitlines():
        parts = line.rstrip("\r").split("|", 2)
        if len(parts) != 3 or not parts[0].isdigit():
            continue
        size, mtime, path = parts
        if not path.startswith(root):
            continue
        if any(skip in path + "/" for skip in SKIP_PARTS):
            continue
        rel = rel_prefix + path[len(root):]
        out.append(RemoteFile(path=path, size=int(size),
                              mtime=int(mtime) if mtime.isdigit() else 0,
                              rel=rel))
    if not out and r.out.strip() == "":
        # Either genuinely empty, or `stat -c` is unsupported. Tell them apart.
        any_file = d.shell(f"find {_q(root)} -type f 2>/dev/null | head -1", timeout=60)
        if any_file.out.strip():
            return None
    return out


def look_around(d: adb.Device, root: str) -> tuple[list[str], list[RemoteFile]] | None:
    """What sits directly inside `root`: folder names, and loose files.

    One level only, and nothing hidden: a `*` in the phone's shell does not
    match names that start with a dot. The shell's own tests tell folders
    from files, and `stat -c` prints the result, which is the one thing the
    rest of this module already depends on — so nothing here asks more of an
    old phone than listing a folder does.

    Returns None when there are things in `root` but they could not be
    described, so the caller can say so instead of reporting nothing found.
    The `rel` of each loose file is just its name; the caller places it.
    """
    q = _q(root)
    r = d.shell(
        f"for f in {q}/*; do "
        f"if [ -d \"$f\" ]; then stat -c 'd|%s|%Y|%n' \"$f\"; "
        f"elif [ -f \"$f\" ]; then stat -c 'f|%s|%Y|%n' \"$f\"; fi; "
        f"done 2>/dev/null", timeout=120)
    folders: list[str] = []
    loose: list[RemoteFile] = []
    for line in r.out.splitlines():
        parts = line.rstrip("\r").split("|", 3)
        if len(parts) != 4 or parts[0] not in ("d", "f") or not parts[1].isdigit():
            continue
        kind, size, mtime, path = parts
        if not path.startswith(root + "/"):
            continue
        name = path[len(root) + 1:]
        if not name or "/" in name or name.startswith("."):
            continue
        if kind == "d":
            folders.append(name)
        else:
            loose.append(RemoteFile(path=path, size=int(size),
                                    mtime=int(mtime) if mtime.isdigit() else 0,
                                    rel=name))
    if not folders and not loose and r.out.strip() == "":
        # Either nothing is there, or `stat -c` is unsupported. Tell them apart.
        anything = d.shell(f"ls {q} 2>/dev/null | head -1", timeout=60)
        if anything.out.strip():
            return None
    return folders, loose


def parse_rows(text: str, columns: list[str]) -> list[dict[str, str | None]]:
    """Parse `content query` output.

    The tool prints one record as

        Row: 0 _id=1, address=+911234, date=1700000000000, body=Hi, there

    with no quoting, so a value may itself contain commas or newlines. Two
    things make that tractable: we asked for the columns, so we know their
    order, and every caller puts its free-text column last, so everything
    after the final `name=` belongs to it.
    """
    rows: list[dict[str, str | None]] = []
    chunks = re.split(r"(?m)^Row: \d+ ", text)
    for chunk in chunks[1:]:
        chunk = chunk.rstrip("\r\n")
        row: dict[str, str | None] = {}
        pos = 0
        ok = True
        for i, col in enumerate(columns):
            marker = f"{col}="
            if not chunk.startswith(marker, pos):
                ok = False
                break
            start = pos + len(marker)
            if i + 1 < len(columns):
                nxt = chunk.find(f", {columns[i + 1]}=", start)
                if nxt < 0:
                    ok = False
                    break
                value = chunk[start:nxt]
                pos = nxt + 2
            else:
                value = chunk[start:]
            row[col] = None if value == "NULL" else value
        if ok:
            rows.append(row)
    return rows


def query(d: adb.Device, uri: str, columns: list[str],
          timeout: float = 180.0) -> list[dict[str, str | None]] | None:
    """Rows from a content provider, or None if this phone refuses ADB."""
    if not re.match(r"^content://[A-Za-z0-9._/-]+$", uri):
        raise BackupError(f"not a content uri: {uri!r}")
    for c in columns:
        if not re.match(r"^[A-Za-z0-9_]+$", c):
            raise BackupError(f"not a column name: {c!r}")
    r = d.shell(f"content query --uri {uri} --projection {':'.join(columns)}",
                timeout=timeout)
    text = r.out
    low = (r.out + r.err).lower()
    if "no result found" in low:
        return []
    if ("permission denial" in low or "securityexception" in low
            or "could not find provider" in low or "unknown url" in low
            or "error while accessing provider" in low):
        return None
    if "Row: " not in text:
        return None if (r.err.strip() or not r.ok) else []
    return parse_rows(text, columns)


# --- contacts ---------------------------------------------------------------

MIME = "vnd.android.cursor.item/"
PHONE_TYPES = {"1": "HOME", "2": "CELL", "3": "WORK", "4": "WORK,FAX",
               "5": "HOME,FAX", "6": "PAGER", "12": "PREF", "17": "WORK,CELL"}
EMAIL_TYPES = {"1": "HOME", "2": "WORK"}

# Free text (data1) last, for parse_rows.
CONTACT_COLUMNS = ["contact_id", "mimetype", "data2", "data3", "data4",
                   "data5", "data1"]


def _vesc(s: str | None) -> str:
    """Escape a value for vCard 3.0."""
    if not s:
        return ""
    return (s.replace("\\", "\\\\").replace("\r\n", "\\n").replace("\n", "\\n")
             .replace(",", "\\,").replace(";", "\\;"))


def contacts_to_vcf(rows: list[dict[str, str | None]]) -> tuple[str, int]:
    """Turn rows of the contacts `data` table into one vCard 3.0 file.

    3.0 rather than 4.0 because every phone and every mail provider imports
    it. Returns (text, number of contacts written).
    """
    people: dict[str, dict] = {}
    order: list[str] = []
    for r in rows:
        cid = r.get("contact_id") or ""
        if not cid:
            continue
        if cid not in people:
            people[cid] = {"name": None, "given": "", "family": "", "middle": "",
                           "phones": [], "emails": [], "org": None, "title": None,
                           "notes": [], "addresses": [], "urls": [], "bday": None}
            order.append(cid)
        p = people[cid]
        mt = (r.get("mimetype") or "").replace(MIME, "")
        d1 = r.get("data1")
        if mt == "name":
            p["name"] = d1 or p["name"]
            p["given"] = r.get("data2") or ""
            p["family"] = r.get("data3") or ""
            p["middle"] = r.get("data5") or ""
        elif mt == "phone_v2" and d1:
            p["phones"].append((d1, PHONE_TYPES.get(r.get("data2") or "", "VOICE")))
        elif mt == "email_v2" and d1:
            p["emails"].append((d1, EMAIL_TYPES.get(r.get("data2") or "", "INTERNET")))
        elif mt == "organization":
            p["org"] = d1 or p["org"]
            p["title"] = r.get("data4") or p["title"]
        elif mt == "note" and d1:
            p["notes"].append(d1)
        elif mt == "postal-address_v2" and d1:
            p["addresses"].append(d1)
        elif mt == "website" and d1:
            p["urls"].append(d1)
        elif mt == "contact_event" and d1 and (r.get("data2") == "3"):
            p["bday"] = d1

    out: list[str] = []
    written = 0
    for cid in order:
        p = people[cid]
        display = p["name"] or (p["phones"][0][0] if p["phones"] else
                                p["emails"][0][0] if p["emails"] else None)
        if not display:
            continue            # an empty shell of a contact: nothing to keep
        out.append("BEGIN:VCARD")
        out.append("VERSION:3.0")
        out.append(f"N:{_vesc(p['family'])};{_vesc(p['given'])};{_vesc(p['middle'])};;")
        out.append(f"FN:{_vesc(display)}")
        seen: set[str] = set()
        for number, kind in p["phones"]:
            key = re.sub(r"[^0-9+]", "", number)
            if key in seen:
                continue        # the same number synced from two accounts
            seen.add(key)
            out.append(f"TEL;TYPE={kind}:{_vesc(number)}")
        seen = set()
        for address, kind in p["emails"]:
            if address.lower() in seen:
                continue
            seen.add(address.lower())
            out.append(f"EMAIL;TYPE={kind}:{_vesc(address)}")
        if p["org"]:
            out.append(f"ORG:{_vesc(p['org'])}")
        if p["title"]:
            out.append(f"TITLE:{_vesc(p['title'])}")
        for a in p["addresses"]:
            out.append(f"ADR;TYPE=HOME:;;{_vesc(a)};;;;")
        for u in p["urls"]:
            out.append(f"URL:{_vesc(u)}")
        if p["bday"]:
            out.append(f"BDAY:{_vesc(p['bday'])}")
        if p["notes"]:
            out.append(f"NOTE:{_vesc(' / '.join(p['notes']))}")
        out.append("END:VCARD")
        written += 1
    return "\r\n".join(out) + ("\r\n" if out else ""), written


# --- messages and calls -----------------------------------------------------

SMS_COLUMNS = ["_id", "thread_id", "address", "date", "type", "read", "body"]
SMS_TYPES = {"1": "received", "2": "sent", "3": "draft", "4": "outbox",
             "5": "failed", "6": "queued"}
CALL_COLUMNS = ["number", "date", "duration", "type", "name"]
CALL_TYPES = {"1": "incoming", "2": "outgoing", "3": "missed", "4": "voicemail",
              "5": "rejected", "6": "blocked"}


def _iso(ms: str | None) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ms or 0) / 1000))
    except (ValueError, OverflowError, OSError):
        return ""


def messages_to_records(rows: list[dict[str, str | None]]) -> list[dict]:
    out = []
    for r in rows:
        out.append({
            "with": r.get("address") or "",
            "when": _iso(r.get("date")),
            "direction": SMS_TYPES.get(r.get("type") or "", "other"),
            "read": (r.get("read") == "1"),
            "text": r.get("body") or "",
            "thread": r.get("thread_id") or "",
            "date_ms": r.get("date") or "",
        })
    out.sort(key=lambda m: m["date_ms"])
    return out


def calls_to_records(rows: list[dict[str, str | None]]) -> list[dict]:
    out = []
    for r in rows:
        out.append({
            "number": r.get("number") or "",
            "name": r.get("name") or "",
            "when": _iso(r.get("date")),
            "seconds": int(r.get("duration") or 0) if (r.get("duration") or "0").isdigit() else 0,
            "kind": CALL_TYPES.get(r.get("type") or "", "other"),
            "date_ms": r.get("date") or "",
        })
    out.sort(key=lambda c: c["date_ms"])
    return out


# --- survey -----------------------------------------------------------------


def _is_saved(dest_files: str, f: RemoteFile) -> bool:
    local = _safe_join(dest_files, f.rel)
    if not local or not os.path.isfile(local):
        return False
    try:
        return os.path.getsize(local) == f.size
    except OSError:
        return False


def _covered() -> tuple[set[str], set[str]]:
    """What FILE_GROUPS already takes: top-level folder names, and app
    folders under Android/media. Lower case, because a phone's shared storage
    does not tell `Download` from `download`."""
    top: set[str] = set()
    media: set[str] = set()
    for _key, _label, folders in FILE_GROUPS:
        for folder in folders:
            parts = folder.split("/")
            top.add(parts[0].lower())
            if len(parts) > 2 and parts[:2] == ["Android", "media"]:
                media.add(parts[2].lower())
    return top, media


def _tally(item: Item, files_root: str, unlistable: list[str]) -> Item:
    item.files = len(item.remote)
    item.bytes = sum(f.size for f in item.remote)
    new = [f for f in item.remote if not _is_saved(files_root, f)]
    item.new_files = len(new)
    item.new_bytes = sum(f.size for f in new)
    if unlistable:
        item.detail = ("Could not list " + ", ".join(_plain(u) for u in unlistable)
                       + " on this version of Android.")
    return item


def survey(d: adb.Device, dest: str,
           say: Callable[[str], None] | None = None) -> Survey:
    """Look at the phone and work out what a backup would copy.

    Changes nothing anywhere. This is what lets the interface say "4,212
    photos and videos, 11.3 GB, about 2,100 of them new" before anyone
    commits to waiting for it.

    It looks further than FILE_GROUPS: every other folder at the top of
    shared storage, other apps' folders under Android/media, and files left
    loose at the top each become an item of their own, so the owner is
    choosing from what is actually on this phone rather than from a list
    written before anyone had seen it.
    """
    say = say or (lambda _t: None)
    files_root = os.path.join(dest, "files")
    sv = Survey(dest=dest)
    sv.sd_cards = sd_cards(d)

    stores = [("/sdcard", "")] + [(f"/storage/{c}", f"sd-card-{c}/") for c in sv.sd_cards]

    for key, label, folders in FILE_GROUPS:
        item = Item(key=key, label=label, kind="files")
        say(f"Looking at {label.lower()}…")
        unlistable = []
        for store, prefix in stores:
            for folder in folders:
                root = f"{store}/{folder}"
                found = list_files(d, root, f"{prefix}{folder}")
                if found is None:
                    unlistable.append(root)
                    continue
                item.remote.extend(found)
        sv.items.append(_tally(item, files_root, unlistable))

    say("Counting contacts, messages and calls…")
    for key, label, kind, uri in (
            ("contacts", "Contacts", "contacts", "content://com.android.contacts/contacts"),
            ("messages", "Text messages", "messages", "content://sms"),
            ("calls", "Call history", "calls", "content://call_log/calls")):
        item = Item(key=key, label=label, kind=kind)
        try:
            rows = query(d, uri, ["_id"], timeout=90)
        except adb.AdbError:
            rows = None
        if rows is None:
            item.status = "unreadable"
            item.detail = UNREADABLE[key]
        else:
            item.count = len(rows)
        sv.items.append(item)

    apps = d.shell("pm list packages -3", timeout=60)
    n = sum(1 for line in apps.out.splitlines() if line.startswith("package:"))
    sv.items.append(Item(key="apps", label="List of apps you installed",
                         kind="apps", count=n))

    # --- everything else in shared storage
    #
    # Listed after the familiar things, so photos and contacts stay at the
    # top however many folders apps have left behind. Android/data and
    # Android/obb are deliberately not looked at: modern Android does not let
    # ADB read them, and what is in them is app internals and game assets,
    # not something the owner made.
    say("Looking around the rest of the phone's storage…")
    covered_top, covered_media = _covered()
    found: dict[str, Item] = {}
    unlistable: dict[str, list[str]] = {}
    not_looked: list[str] = []

    def add(key: str, label: str, root: str, rel: str) -> None:
        item = found.setdefault(key, Item(key=key, label=label, kind="files",
                                          discovered=True))
        files = list_files(d, root, rel, probe=False)
        if files is None:
            unlistable.setdefault(key, []).append(root)
        else:
            item.remote.extend(files)

    for store, prefix in stores:
        where = " (on the SD card)" if prefix else ""
        top = look_around(d, store)
        if top is None:
            not_looked.append(store)
        else:
            folders, loose = top
            for name in sorted(folders, key=str.lower):
                # `Android` is skipped whole here; the one part of it worth
                # reading, Android/media, is handled just below.
                if name.lower() == "android" or name.lower() in covered_top:
                    continue
                add("folder:" + _key_part(name),
                    f"Folder: {_plain(name)}{where}",
                    f"{store}/{name}", f"{prefix}{name}")
            if loose:
                key = "loose-sd" if prefix else "loose"
                item = found.setdefault(key, Item(
                    key=key, kind="files", discovered=True,
                    label=("Loose files on the SD card" if prefix
                           else "Loose files in main storage")))
                for f in loose:
                    f.rel = prefix + f.rel
                    item.remote.append(f)
        media = look_around(d, f"{store}/Android/media")
        if media is None:
            not_looked.append(f"{store}/Android/media")
        else:
            for name in sorted(media[0], key=str.lower):
                if name.lower() in covered_media:
                    continue        # WhatsApp: already saved under its own name
                add("appmedia:" + _key_part(name),
                    f"Files kept by {_plain(name)}{where}",
                    f"{store}/Android/media/{name}",
                    f"{prefix}Android/media/{name}")

    def order(item: Item) -> tuple[int, str]:
        rank = 0 if item.key.startswith("folder:") else \
            1 if item.key.startswith("appmedia:") else 2
        return rank, item.label.lower()

    for item in sorted(found.values(), key=order):
        _tally(item, files_root, unlistable.get(item.key, []))
        # An empty folder is not worth a line on someone's screen. One that
        # is there but could not be listed is, because that is a gap.
        if item.files or item.detail:
            sv.items.append(item)

    if not_looked:
        sv.items.append(Item(
            key="other", label="Other folders on the phone", kind="files",
            status="unreadable", discovered=True,
            detail="Could not look around " + ", ".join(not_looked)
                   + " on this version of Android, so only the folders "
                     "listed above were checked. If you keep things anywhere "
                     "else on the phone, copy them off by hand."))
    return sv


def item_dict(i: Item) -> dict:
    """One item as plain data: what `backup --check --json` prints and what
    the web page draws its checklist from. Labels and details can contain
    names from the phone; they are text to show, never anything to run."""
    return {"key": i.key, "label": i.label, "kind": i.kind,
            "files": i.files, "bytes": i.bytes,
            "new_files": i.new_files, "new_bytes": i.new_bytes,
            "count": i.count, "status": i.status, "summary": i.summary,
            "detail": i.detail, "discovered": i.discovered}


# --- the backup itself ------------------------------------------------------


def _pull_one(d: adb.Device, f: RemoteFile, files_root: str) -> str:
    """Copy one file. Returns '' on success, or why not."""
    local = _safe_join(files_root, f.rel)
    if not local:
        return "unsafe file name"
    try:
        os.makedirs(os.path.dirname(local), exist_ok=True)
    except OSError as e:
        # A name this computer's disk will not take. Now that folders are
        # found by looking, any name can turn up; it costs that one file.
        return f"could not make a folder for it here: {e.strerror or e}"
    part = local + ".part"
    try:
        r = d.adb("pull", "-a", f.path, part, timeout=max(120.0, f.size / 200_000))
    except adb.DeviceGone:
        raise
    except adb.AdbError as e:
        _unlink(part)
        return str(e)
    if not os.path.isfile(part):
        return (r.text or "adb pull produced no file").splitlines()[-1][:160]
    if os.path.getsize(part) != f.size:
        got = os.path.getsize(part)
        _unlink(part)
        return f"size mismatch: phone says {f.size}, got {got}"
    os.replace(part, local)
    return ""


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def run(d: adb.Device, device_info: dict, sv: Survey, *,
        say: Callable[[str, bool], None] | None = None,
        progress: Callable[[int, int, int, int], None] | None = None,
        workers: int = 3,
        only: Iterable[str] | None = None) -> Result:
    """Copy everything in the survey that is not already here, then check it.

    `say(text, ok)` gets one line per thing worth telling a person.
    `progress(done_files, total_files, done_bytes, total_bytes)` is called as
    files land, from worker threads, so it must be cheap and thread-safe.

    `only` is the set of item keys the owner chose. Everything else is left
    alone and recorded as "skipped" — in the result and in the manifest — so
    a backup of a selection can never be read as a backup of everything.
    The room check, the copying and the checking all cover the chosen items
    and nothing else. An unknown key raises BackupError before anything is
    written.
    """
    say = say or (lambda _t, _ok=True: None)
    progress = progress or (lambda *_a: None)
    picked = sv.chosen(only)
    want = {i.key for i in picked}
    # Something the survey already could not read was never on offer, so
    # leaving it out is not the owner's choice and does not make this a
    # selection. It keeps saying "could not be read", which is the truth.
    left_out = [i for i in sv.items
                if i.key not in want and i.status != "unreadable"]
    started = time.time()

    dest = sv.dest
    files_root = os.path.join(dest, "files")
    snap_name = time.strftime("%Y-%m-%d_%H%M%S", time.localtime(started))
    snapshot = os.path.join(dest, "snapshots", snap_name)
    os.makedirs(files_root, exist_ok=True)
    os.makedirs(snapshot, exist_ok=True)

    # Room first. Running out of disk half-way through somebody's photographs
    # is the failure to prevent, not to report.
    free = shutil.disk_usage(dest).free
    need = sum(i.new_bytes for i in picked)
    if need + 200 * 1024 * 1024 > free:
        raise BackupError(
            f"Not enough room on this computer. The backup needs about "
            f"{human(need)} and {human(free)} is free in {dest}. Free some "
            f"space, or choose another place with FMA_BACKUP_DIR.")

    # Past this point the survey's items are written to. A survey can be
    # looked at, chosen from and then run, so nothing above may change it.
    for item in sv.items:
        item.failed = []
        if item.key not in want and item.status != "unreadable":
            item.status = "skipped"

    todo: list[tuple[Item, RemoteFile]] = []
    for item in picked:
        if item.kind != "files":
            continue
        for f in item.remote:
            if not _is_saved(files_root, f):
                todo.append((item, f))

    total_files = len(todo)
    total_bytes = sum(f.size for _i, f in todo)
    done = {"files": 0, "bytes": 0, "failed": 0}
    lock = threading.Lock()
    gone: list[BaseException] = []

    if total_files:
        say(f"Copying {count_files(total_files)}, {human(total_bytes)}. "
            f"The phone can be used meanwhile; keep it plugged in.", True)

    def work(pair: tuple[Item, RemoteFile]) -> None:
        item, f = pair
        if gone:
            return
        try:
            why = _pull_one(d, f, files_root)
        except adb.DeviceGone as e:
            gone.append(e)
            return
        with lock:
            if why:
                done["failed"] += 1
                item.failed.append(f"{f.rel}: {why}")
            else:
                done["files"] += 1
                done["bytes"] += f.size
            progress(done["files"] + done["failed"], total_files,
                     done["bytes"], total_bytes)

    if todo:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            list(pool.map(work, todo))
    if gone:
        raise gone[0]

    # Verify every file the phone listed, not only the ones copied just now:
    # the claim is "this is saved", and that should be true of all of it.
    for item in picked:
        if item.kind != "files" or item.status == "unreadable":
            continue
        missing = [f for f in item.remote if not _is_saved(files_root, f)]
        if not item.files:
            item.status = "empty"
        elif missing:
            item.status = "partial"
            say(f"{item.label}: {len(missing):,} of {item.files:,} could not "
                f"be saved. They are listed in the summary.", False)
        else:
            item.status = "ok"
            say(f"{item.label}: {count_files(item.files)} saved and checked "
                f"({human(item.bytes)}).", True)

    # --- contacts
    item = sv.item("contacts")
    if item is not None and item.key in want:
        try:
            rows = query(d, "content://com.android.contacts/data", CONTACT_COLUMNS)
        except adb.AdbError:
            rows = None
        if rows is None:
            item.status = "unreadable"
            item.detail = UNREADABLE["contacts"]
            say("Contacts could not be read on this phone. " + item.detail, False)
        else:
            text, n = contacts_to_vcf(rows)
            item.count = n
            if n:
                _write(os.path.join(snapshot, "contacts.vcf"), text)
                item.status = "ok"
                say(f"Contacts: {n:,} saved as contacts.vcf.", True)
            else:
                item.status = "empty"

    # --- messages
    item = sv.item("messages")
    if item is not None and item.key in want:
        try:
            rows = query(d, "content://sms", SMS_COLUMNS)
        except adb.AdbError:
            rows = None
        if rows is None:
            item.status = "unreadable"
            item.detail = UNREADABLE["messages"]
            say("Text messages could not be read on this phone. " + item.detail, False)
        else:
            recs = messages_to_records(rows)
            item.count = len(recs)
            if recs:
                _write(os.path.join(snapshot, "messages.json"),
                       json.dumps(recs, indent=1, ensure_ascii=False))
                item.status = "ok"
                say(f"Text messages: {len(recs):,} saved.", True)
            else:
                item.status = "empty"

    # --- calls
    item = sv.item("calls")
    if item is not None and item.key in want:
        try:
            rows = query(d, "content://call_log/calls", CALL_COLUMNS)
        except adb.AdbError:
            rows = None
        if rows is None:
            item.status = "unreadable"
            item.detail = UNREADABLE["calls"]
            say("Call history could not be read on this phone.", False)
        else:
            recs = calls_to_records(rows)
            item.count = len(recs)
            if recs:
                _write(os.path.join(snapshot, "calls.json"),
                       json.dumps(recs, indent=1, ensure_ascii=False))
                item.status = "ok"
                say(f"Call history: {len(recs):,} calls saved.", True)
            else:
                item.status = "empty"

    # --- apps
    item = sv.item("apps")
    if item is not None and item.key in want:
        r = d.shell("pm list packages -3", timeout=60)
        pkgs = sorted(line[len("package:"):].strip() for line in r.out.splitlines()
                      if line.startswith("package:"))
        item.count = len(pkgs)
        if pkgs:
            lines = ["Apps that were installed on this phone, so they can be "
                     "put back.", "Open a link on the new phone to install.", ""]
            lines += [f"{p}\n    https://play.google.com/store/apps/details?id={p}"
                      for p in pkgs]
            _write(os.path.join(snapshot, "apps.txt"), "\n".join(lines) + "\n")
            item.status = "ok"
            say(f"App list: {len(pkgs):,} apps noted.", True)
        else:
            item.status = "empty"

    # Only the ones that had something in them: "you left out an empty
    # folder" is noise. Their status still says "skipped" either way.
    missed = [i.label for i in left_out if i.files or i.count]
    if missed:
        say("Not chosen, so not copied this time: " + ", ".join(missed) + ".",
            True)

    finished = time.time()
    res = Result(dest=dest, snapshot=snapshot, items=sv.items, started=started,
                 finished=finished, copied_files=done["files"],
                 copied_bytes=done["bytes"],
                 failed_files=sum(len(i.failed) for i in picked),
                 chosen=[i.key for i in picked],
                 everything=not left_out)
    _write_manifest(res, device_info)
    _write_readme(res, device_info)
    return res


def _write(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _write_manifest(res: Result, device_info: dict) -> None:
    from . import __version__
    blob = {
        "written_by": f"free-my-android {__version__}",
        "device": device_info,
        "started": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(res.started)),
        "finished": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(res.finished)),
        # `complete` is about what was asked for: every chosen file is here
        # and checked. `everything` is the other question — whether the
        # owner left anything out — and anything reading this file to decide
        # "is this phone backed up?" has to look at both.
        "complete": res.ok,
        "everything": res.everything,
        "chosen": res.chosen,
        "copied_this_run": {"files": res.copied_files, "bytes": res.copied_bytes},
        "items": [
            {"key": i.key, "label": i.label, "kind": i.kind, "status": i.status,
             "files": i.files, "bytes": i.bytes, "count": i.count,
             "discovered": i.discovered, "detail": i.detail,
             "failed": i.failed}
            for i in res.items
        ],
        "not_saved": [{"what": w, "why": y} for w, y in NOT_SAVED],
    }
    _write(os.path.join(res.snapshot, "manifest.json"),
           json.dumps(blob, indent=1, ensure_ascii=False) + "\n")


def _write_readme(res: Result, device_info: dict) -> None:
    name = device_info.get("name", "your phone")
    lines = [
        f"Backup of {name}",
        "=" * (10 + len(name)),
        "",
        "Made by Free My Android. Everything here is an ordinary file; you do",
        "not need this program to open or use any of it.",
        "",
        "files/",
        "    Your photos, videos, downloads, documents, music and WhatsApp",
        "    files, and any other folders that were found on the phone, in",
        "    the same folders they had there. Copy a folder back to a phone",
        "    with a cable to restore it.",
        "",
        "snapshots/<date>/contacts.vcf",
        "    Your contacts. On any phone: Contacts app -> Import -> choose this",
        "    file. Gmail, Outlook and iCloud can import it too.",
        "",
        "snapshots/<date>/messages.json   and   calls.json",
        "    Your text messages and call history, readable in any text editor.",
        "",
        "snapshots/<date>/apps.txt",
        "    The apps you had installed, with links to install them again.",
        "",
        "snapshots/<date>/manifest.json",
        "    Exactly what was saved, what could not be, and why. If only some",
        "    things were chosen for a backup, the rest are marked \"skipped\".",
        "",
        "NOT in this backup",
        "------------------",
    ]
    for what, why in NOT_SAVED:
        lines.append(f"* {what}")
        for chunk in _wrap(why, 68):
            lines.append(f"    {chunk}")
        lines.append("")
    skipped = [i.label for i in res.items
               if i.status == "skipped" and (i.files or i.count)]
    if skipped:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(res.finished))
        lines += ["LEFT OUT OF THE MOST RECENT BACKUP", "-" * 34,
                  f"The backup made on {when} was of chosen things only.",
                  "These were on the phone and were not chosen that time, so",
                  "anything new in them since an earlier backup is not here:",
                  ""]
        lines += [f"* {label}" for label in skipped]
        lines.append("")
    _write(os.path.join(res.dest, "READ ME FIRST.txt"), "\n".join(lines) + "\n")


def _wrap(text: str, width: int) -> list[str]:
    import textwrap
    return textwrap.wrap(text, width) or [""]


# --- what the interfaces ask ------------------------------------------------


def last_backup(dest: str) -> dict | None:
    """The newest snapshot's manifest for this device, or None."""
    snaps = os.path.join(dest, "snapshots")
    if not os.path.isdir(snaps):
        return None
    for name in sorted(os.listdir(snaps), reverse=True):
        path = os.path.join(snaps, name, "manifest.json")
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    blob = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            blob["_path"] = os.path.join(snaps, name)
            return blob
    return None


def was_everything(manifest: dict | None) -> bool:
    """Whether that backup was of everything on offer, or of a selection.
    Manifests written before 1.2.0 have no such field, and there was no way
    to leave anything out then."""
    return bool(manifest) and bool(manifest.get("everything", True))


def age_text(manifest: dict | None) -> str:
    """'today', 'yesterday', '12 days ago' — or '' if there is none."""
    if not manifest:
        return ""
    try:
        then = time.mktime(time.strptime(manifest["finished"], "%Y-%m-%d %H:%M:%S"))
    except (KeyError, ValueError):
        return ""
    days = int((time.time() - then) // 86400)
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"
