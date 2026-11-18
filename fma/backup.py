"""Saving what matters off the phone, before anything else happens.

Nothing in this module changes the phone. It only reads, and everything it
reads lands in an ordinary folder on this computer that the owner can open,
copy to a USB stick, or restore from by hand.

The honest scope, because a backup that overstates itself is worse than no
backup:

  saved      photos, videos, downloads, documents, music, recordings,
             WhatsApp media and WhatsApp's own chat-backup files,
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
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from . import adb

# --- what gets saved --------------------------------------------------------

# (key, plain label, folders on the phone). Paths are relative to shared
# storage; the same list is applied to an SD card if there is one, because a
# lot of budget phones save the camera roll there.
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
    status: str = "pending"         # ok | partial | empty | unreadable
    detail: str = ""
    failed: list[str] = field(default_factory=list)
    remote: list[RemoteFile] = field(default_factory=list, repr=False)

    @property
    def summary(self) -> str:
        if self.kind == "files":
            if not self.files:
                return "nothing found"
            s = f"{count_files(self.files)}, {human(self.bytes)}"
            if self.new_files != self.files:
                s += (f" — {self.new_files:,} new ({human(self.new_bytes)})"
                      if self.new_files else " — all already saved")
            return s
        if self.status == "unreadable":
            return "could not be read on this phone"
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

    @property
    def ok(self) -> bool:
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


def list_files(d: adb.Device, root: str, rel_prefix: str) -> list[RemoteFile] | None:
    """Every file under `root` on the phone, with size and modified time.

    Returns None when the folder exists but could not be listed (very old
    Android without `stat -c`), which the caller reports rather than treating
    as empty. A folder that simply is not there returns [].
    """
    probe = d.shell(f"[ -d '{root}' ] && echo yes || echo no", timeout=20)
    if "yes" not in probe.out:
        return []
    r = d.shell(
        f"find '{root}' -type f -exec stat -c '%s|%Y|%n' {{}} + 2>/dev/null",
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
        any_file = d.shell(f"find '{root}' -type f 2>/dev/null | head -1", timeout=60)
        if any_file.out.strip():
            return None
    return out


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


def survey(d: adb.Device, dest: str,
           say: Callable[[str], None] | None = None) -> Survey:
    """Look at the phone and work out what a backup would copy.

    Changes nothing anywhere. This is what lets the interface say "4,212
    photos and videos, 11.3 GB, about 2,100 of them new" before anyone
    commits to waiting for it.
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
        item.files = len(item.remote)
        item.bytes = sum(f.size for f in item.remote)
        new = [f for f in item.remote if not _is_saved(files_root, f)]
        item.new_files = len(new)
        item.new_bytes = sum(f.size for f in new)
        if unlistable:
            item.detail = ("Could not list " + ", ".join(unlistable)
                           + " on this version of Android.")
        sv.items.append(item)

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
        else:
            item.count = len(rows)
        sv.items.append(item)

    apps = d.shell("pm list packages -3", timeout=60)
    n = sum(1 for line in apps.out.splitlines() if line.startswith("package:"))
    sv.items.append(Item(key="apps", label="List of apps you installed",
                         kind="apps", count=n))
    return sv


# --- the backup itself ------------------------------------------------------


def _pull_one(d: adb.Device, f: RemoteFile, files_root: str) -> str:
    """Copy one file. Returns '' on success, or why not."""
    local = _safe_join(files_root, f.rel)
    if not local:
        return "unsafe file name"
    os.makedirs(os.path.dirname(local), exist_ok=True)
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
        workers: int = 3) -> Result:
    """Copy everything in the survey that is not already here, then check it.

    `say(text, ok)` gets one line per thing worth telling a person.
    `progress(done_files, total_files, done_bytes, total_bytes)` is called as
    files land, from worker threads, so it must be cheap and thread-safe.
    """
    say = say or (lambda _t, _ok=True: None)
    progress = progress or (lambda *_a: None)
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
    need = sv.new_bytes
    if need + 200 * 1024 * 1024 > free:
        raise BackupError(
            f"Not enough room on this computer. The backup needs about "
            f"{human(need)} and {human(free)} is free in {dest}. Free some "
            f"space, or choose another place with FMA_BACKUP_DIR.")

    todo: list[tuple[Item, RemoteFile]] = []
    for item in sv.items:
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
    for item in sv.items:
        if item.kind != "files":
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
    if item is not None:
        try:
            rows = query(d, "content://com.android.contacts/data", CONTACT_COLUMNS)
        except adb.AdbError:
            rows = None
        if rows is None:
            item.status = "unreadable"
            item.detail = ("This phone would not let contacts be read from a "
                           "computer. Open the Contacts app → Settings → "
                           "Export, and save the .vcf file it makes.")
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
    if item is not None:
        try:
            rows = query(d, "content://sms", SMS_COLUMNS)
        except adb.AdbError:
            rows = None
        if rows is None:
            item.status = "unreadable"
            item.detail = ("This phone would not let text messages be read "
                           "from a computer. An app such as SMS Backup & "
                           "Restore can save them from the phone itself.")
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
    if item is not None:
        try:
            rows = query(d, "content://call_log/calls", CALL_COLUMNS)
        except adb.AdbError:
            rows = None
        if rows is None:
            item.status = "unreadable"
            item.detail = "This phone would not let call history be read from a computer."
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
    if item is not None:
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

    finished = time.time()
    res = Result(dest=dest, snapshot=snapshot, items=sv.items, started=started,
                 finished=finished, copied_files=done["files"],
                 copied_bytes=done["bytes"],
                 failed_files=sum(len(i.failed) for i in sv.items))
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
        "complete": res.ok,
        "copied_this_run": {"files": res.copied_files, "bytes": res.copied_bytes},
        "items": [
            {"key": i.key, "label": i.label, "kind": i.kind, "status": i.status,
             "files": i.files, "bytes": i.bytes, "count": i.count,
             "detail": i.detail, "failed": i.failed}
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
        "    files, in the same folders they had on the phone. Copy a folder",
        "    back to a phone with a cable to restore it.",
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
        "    Exactly what was saved, what could not be, and why.",
        "",
        "NOT in this backup",
        "------------------",
    ]
    for what, why in NOT_SAVED:
        lines.append(f"* {what}")
        for chunk in _wrap(why, 68):
            lines.append(f"    {chunk}")
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
