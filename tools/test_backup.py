#!/usr/bin/env python3
"""End-to-end test of the backup pipeline against a fake phone.

No hardware needed: tools/fake_adb.py stands in for adb and runs the real
shell commands against a fixture directory. Run by tools/check.sh.

What this proves: survey, copy, verify, incremental re-runs, changed files,
SD cards, hostile file names, skipped caches, and a phone that refuses the
contacts and messages providers. Then the looking-around: folders nobody
listed in advance, other apps' Android/media folders, loose files, folder
names written to break a shell, and backing up a selection without it ever
passing for a backup of everything.

What it cannot prove: that a given real phone lets ADB read its contacts.
Some do not, which is exactly why the refusal path is tested here.
"""

import collections
import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

phone = tempfile.mkdtemp(prefix="fakephone-")
dest_root = tempfile.mkdtemp(prefix="fakebackup-")
os.environ["FMA_ADB"] = os.path.join(HERE, "fake_adb.py")
os.environ["FAKE_PHONE"] = phone
os.environ.pop("FAKE_DENY", None)

from fma import adb, backup  # noqa: E402


def put(rel: str, data: bytes) -> None:
    p = os.path.join(phone, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)


def main() -> int:
    # ---- unit: parsing content-provider output
    text = (
        "Row: 0 _id=1, thread_id=4, address=+911234567890, date=1700000000000, type=1, read=1, body=Hi, there\n"
        "second line, with a comma\n"
        "Row: 1 _id=2, thread_id=9, address=NULL, date=1700000200000, type=2, read=1, body=NULL\n")
    rows = backup.parse_rows(text, backup.SMS_COLUMNS)
    assert len(rows) == 2
    assert rows[0]["body"] == "Hi, there\nsecond line, with a comma"
    assert rows[1]["address"] is None and rows[1]["body"] is None
    print("   content rows: multi-line values, commas and NULL")

    # ---- unit: nothing from the phone can climb out of the backup folder
    base = tempfile.mkdtemp()
    for bad in ("../../etc/passwd", "DCIM/../../x", "a//b"):
        assert backup._safe_join(base, bad) is None, bad
    shutil.rmtree(base)
    for bad_uri in ("content://sms; rm -rf /", "file:///x", "content://sms --where 1"):
        try:
            backup.query(None, bad_uri, ["_id"])
        except backup.BackupError:
            pass
        else:
            raise AssertionError("accepted " + bad_uri)
    print("   hostile paths and uris refused")

    # ---- unit: quoting for the phone's shell, and keys safe to type
    assert backup._q("/sdcard/it's") == "'/sdcard/it'\\''s'"
    assert backup._key_part("Telegram") == "Telegram"
    odd = backup._key_part("My 'stuff'; ls")
    assert odd.startswith("My-stuff-ls-") and re.fullmatch(r"[A-Za-z0-9._-]+", odd), odd
    assert backup._key_part("a b") != backup._key_part("a-b"), "two names, one key"
    assert backup._plain("a\x1b[2Kb\u202ec") == "a?[2Kb?c"
    print("   shell quoting, shell-safe keys, printable labels")

    # ---- the fake phone
    put("sdcard/DCIM/Camera/IMG_0001.jpg", b"A" * 5000)
    put("sdcard/DCIM/Camera/IMG 0002 (copy).jpg", b"B" * 7000)
    put("sdcard/DCIM/Camera/VID_0003.mp4", b"C" * 90000)
    put("sdcard/DCIM/.thumbnails/t1.jpg", b"T" * 100)
    put("sdcard/Pictures/Screenshots/s|odd'name.png", b"D" * 1234)
    put("sdcard/Download/bill.pdf", b"E" * 4321)
    put("sdcard/Android/media/com.whatsapp/WhatsApp/Databases/msgstore.db.crypt14", b"F" * 8888)
    put("storage/1A2B-3C4D/DCIM/Camera/SD_0001.jpg", b"G" * 3000)
    put("content/contacts_ids.txt", b"Row: 0 _id=1\nRow: 1 _id=2\n")
    put("content/contacts_data.txt",
        b"Row: 0 contact_id=1, mimetype=vnd.android.cursor.item/name, data2=Asha, data3=Rao, data4=NULL, data5=NULL, data1=Asha Rao\n"
        b"Row: 1 contact_id=1, mimetype=vnd.android.cursor.item/phone_v2, data2=2, data3=NULL, data4=NULL, data5=NULL, data1=+91 98765 43210\n"
        b"Row: 2 contact_id=1, mimetype=vnd.android.cursor.item/phone_v2, data2=2, data3=NULL, data4=NULL, data5=NULL, data1=+919876543210\n"
        b"Row: 3 contact_id=1, mimetype=vnd.android.cursor.item/note, data2=NULL, data3=NULL, data4=NULL, data5=NULL, data1=Met at Pune; likes tea, not coffee\n"
        b"Row: 4 contact_id=2, mimetype=vnd.android.cursor.item/phone_v2, data2=1, data3=NULL, data4=NULL, data5=NULL, data1=0112345678\n"
        b"Row: 5 contact_id=3, mimetype=vnd.android.cursor.item/photo, data2=NULL, data3=NULL, data4=NULL, data5=NULL, data1=NULL\n")
    put("content/sms_ids.txt", b"Row: 0 _id=1\n")
    put("content/sms.txt",
        b"Row: 0 _id=1, thread_id=1, address=+911234, date=1700000000000, type=1, read=1, body=Hello, world\nline two\n")
    put("packages.txt", b"package:com.whatsapp\npackage:org.videolan.vlc\n")

    d = adb.Device("FAKE0001")
    dest = backup.device_dir("Fake Phone", "FAKE0001", dest_root)
    files = os.path.join(dest, "files")

    # ---- run 1: everything is new
    sv = backup.survey(d, dest)
    assert sv.sd_cards == ["1A2B-3C4D"]
    photos = sv.item("photos")
    assert photos.files == 5, photos.files
    assert not any(".thumbnails" in f.path for f in photos.remote)
    assert sv.item("contacts").count == 2 and sv.item("messages").count == 1
    res = backup.run(d, {"name": "Fake Phone"}, sv)
    assert res.ok and res.copied_files == 7, (res.copied_files, [i.failed for i in res.items])
    with open(os.path.join(files, "Pictures/Screenshots/s|odd'name.png"), "rb") as f:
        assert f.read() == b"D" * 1234
    assert os.path.isfile(os.path.join(files, "sd-card-1A2B-3C4D/DCIM/Camera/SD_0001.jpg"))
    assert not os.path.exists(os.path.join(files, "DCIM/.thumbnails"))
    assert not [x for _p, _d, fs in os.walk(dest) for x in fs if x.endswith(".part")]
    with open(os.path.join(res.snapshot, "contacts.vcf"), newline="") as f:
        vcf = f.read()
    assert vcf.count("BEGIN:VCARD") == 2            # the photo-only shell is dropped
    assert vcf.count("TEL;TYPE=CELL") == 1          # same number from two accounts
    assert "NOTE:Met at Pune\\; likes tea\\, not coffee" in vcf
    assert "FN:0112345678" in vcf                   # nameless contact keeps its number
    with open(os.path.join(res.snapshot, "messages.json")) as f:
        assert json.load(f)[0]["text"] == "Hello, world\nline two"
    with open(os.path.join(res.snapshot, "manifest.json")) as f:
        man = json.load(f)
    assert man["complete"] is True and len(man["not_saved"]) == len(backup.NOT_SAVED)
    assert os.path.isfile(os.path.join(dest, "READ ME FIRST.txt"))
    print("   run 1: 7 files copied and verified, vcf + json + manifest written")

    # ---- run 2: nothing new
    sv = backup.survey(d, dest)
    assert sv.new_files == 0
    assert backup.run(d, {"name": "Fake Phone"}, sv).copied_files == 0
    print("   run 2: incremental, nothing copied")

    # ---- run 3: one new file and one that changed size
    put("sdcard/DCIM/Camera/IMG_0004.jpg", b"H" * 2222)
    put("sdcard/Download/bill.pdf", b"E" * 9999)
    sv = backup.survey(d, dest)
    assert sv.new_files == 2
    res = backup.run(d, {"name": "Fake Phone"}, sv)
    assert res.copied_files == 2 and res.ok
    assert os.path.getsize(os.path.join(files, "Download/bill.pdf")) == 9999
    print("   run 3: one new and one changed file picked up")

    # ---- run 4: a phone that refuses contacts and messages
    os.environ["FAKE_DENY"] = "contacts,sms"
    sv = backup.survey(d, dest)
    assert sv.item("contacts").status == "unreadable"
    log: list[tuple[bool, str]] = []
    res = backup.run(d, {"name": "Fake Phone"}, sv,
                     say=lambda t, ok=True: log.append((ok, t)))
    c = next(i for i in res.items if i.key == "contacts")
    assert c.status == "unreadable" and "Export" in c.detail
    assert any((not ok) and "Contacts could not be read" in t for ok, t in log)
    assert not os.path.exists(os.path.join(res.snapshot, "contacts.vcf")), \
        "an empty vcf was written as if the export had worked"
    del os.environ["FAKE_DENY"]
    print("   run 4: refused providers reported as unreadable, no empty export")

    assert backup.age_text(backup.last_backup(dest)) == "today"
    # Everything so far was a backup of everything, and says so.
    assert backup.was_everything(backup.last_backup(dest)) is True

    # ---- run 5: looking around the rest of the phone
    put("sdcard/Telegram/Telegram Images/1.jpg", b"I" * 1500)
    put("sdcard/Telegram/.thumbnails/t.jpg", b"T" * 10)
    put("sdcard/Android/media/org.telegram.messenger/Telegram/v.mp4", b"J" * 2500)
    put("sdcard/Android/data/com.game/files/save.dat", b"K" * 10)
    put("sdcard/Android/obb/com.game/main.obb", b"L" * 10)
    put("sdcard/.hidden/secret.txt", b"M" * 10)
    put("sdcard/.nomedia", b"")
    put("sdcard/notes.txt", b"N" * 300)
    put("sdcard/shopping list.txt", b"O" * 40)
    put("storage/1A2B-3C4D/Backups/old.zip", b"P" * 600)
    os.makedirs(os.path.join(phone, "sdcard/EmptyFolder"))
    # Folder names written to break out of a quoted shell string. If any of
    # them is ever executed, it leaves a file called canary* in the fixture,
    # which is where tools/fake_adb.py runs its shell.
    hostile = ["My 'stuff'; ls", "x'; touch canary1; echo '",
               "$(touch canary2)", "`touch canary3`", "a\"; touch canary4; \""]
    for n, name in enumerate(hostile):
        put(f"sdcard/{name}/it's here.txt", bytes([97 + n]) * (50 + n))

    sv = backup.survey(d, dest)
    keys = sv.keys
    fixed = [k for k, _l, _f in backup.FILE_GROUPS]
    assert keys[:len(fixed)] == fixed, "the familiar things come first"
    assert sv.item("documents") is not None and sv.item("documents").files == 0, \
        "a fixed group stays listed when empty"
    tg = sv.item("folder:Telegram")
    assert tg and tg.label == "Folder: Telegram" and tg.files == 1 and tg.bytes == 1500, tg
    assert tg.new_files == 1 and tg.new_bytes == 1500 and tg.kind == "files"
    am = sv.item("appmedia:org.telegram.messenger")
    assert am and am.label == "Files kept by org.telegram.messenger" and am.files == 1, am
    loose = sv.item("loose")
    assert loose and loose.label == "Loose files in main storage"
    assert sorted(f.rel for f in loose.remote) == ["notes.txt", "shopping list.txt"]
    sd = sv.item("folder:Backups")
    assert sd and sd.label == "Folder: Backups (on the SD card)" and sd.files == 1
    assert not any("whatsapp" in k.lower() for k in keys if k != "whatsapp"), \
        "WhatsApp's Android/media folder listed twice"
    assert sv.item("whatsapp").files == 1
    every = [f.path for i in sv.items for f in i.remote]
    assert len(every) == len(set(every)), "a file is counted under two items"
    assert not any("/Android/data/" in p or "/Android/obb/" in p for p in every)
    assert not any("/.hidden/" in p or p.endswith("/.nomedia") or "/.thumbnails/" in p
                   for p in every)
    assert not any("EmptyFolder" in i.label for i in sv.items), "an empty folder was listed"
    assert all(re.fullmatch(r"[A-Za-z0-9._:-]+", k) for k in keys), \
        f"a key that is not safe to type in a shell: {keys}"
    assert len(keys) == len(set(keys))
    labels = {i.label: i for i in sv.items}
    for n, name in enumerate(hostile):
        item = labels.get("Folder: " + name)
        assert item and item.files == 1 and item.bytes == 50 + n, (name, item)
    assert sv.item("other") is None
    print(f"   run 5: found {len(keys) - 9} more things by looking: folders, "
          "app folders, loose files, an SD-card folder")

    res = backup.run(d, {"name": "Fake Phone"}, sv)
    assert res.ok and res.everything, [i.failed for i in res.items]
    for n, name in enumerate(hostile):
        with open(os.path.join(files, name, "it's here.txt"), "rb") as f:
            assert f.read() == bytes([97 + n]) * (50 + n)
    for rel, size in (("Telegram/Telegram Images/1.jpg", 1500),
                      ("Android/media/org.telegram.messenger/Telegram/v.mp4", 2500),
                      ("notes.txt", 300), ("shopping list.txt", 40),
                      ("sd-card-1A2B-3C4D/Backups/old.zip", 600)):
        assert os.path.getsize(os.path.join(files, rel)) == size, rel
    assert not os.path.exists(os.path.join(files, "Android/data"))
    assert not os.path.exists(os.path.join(files, ".hidden"))
    stray = [x for _p, ds, fs in os.walk(phone) for x in ds + fs if x.startswith("canary")]
    assert not stray, f"a folder name was run as a command on the phone: {stray}"
    with open(os.path.join(res.snapshot, "manifest.json")) as f:
        man = json.load(f)
    assert man["everything"] is True and man["complete"] is True
    assert sorted(man["chosen"]) == sorted(keys)
    print(f"   run 5: all of it copied under files/, {len(hostile)} hostile "
          "folder names copied and none of them executed")

    # ---- run 6: choosing. A fresh destination, so "absent" means something.
    dest2 = backup.device_dir("Fake Phone", "FAKE0002", dest_root)
    files2 = os.path.join(dest2, "files")
    sv = backup.survey(d, dest2)
    try:
        backup.run(d, {"name": "Fake Phone"}, sv, only={"photos", "nope"})
    except backup.BackupError as e:
        assert "'nope'" in str(e) and "photos" in str(e) and "folder:Telegram" in str(e), e
    else:
        raise AssertionError("an unknown key was accepted")
    assert not os.path.exists(dest2), "a refused backup still wrote something"
    assert all(i.status == "pending" for i in sv.items), "a refused backup changed the survey"

    # Room is counted for what was chosen, not for the whole phone.
    Usage = collections.namedtuple("Usage", "total used free")
    real_usage = backup.shutil.disk_usage
    margin = 200 * 1024 * 1024
    backup.shutil.disk_usage = lambda _p: Usage(0, 0, margin + sv.item("photos").new_bytes + 1)
    try:
        try:
            backup.run(d, {"name": "Fake Phone"}, sv)
        except backup.BackupError as e:
            assert "Not enough room" in str(e)
        else:
            raise AssertionError("everything fitted in room for the photos alone")
        log = []
        res = backup.run(d, {"name": "Fake Phone"}, sv, only={"photos", "contacts"},
                         say=lambda t, ok=True: log.append((ok, t)))
    finally:
        backup.shutil.disk_usage = real_usage
    assert res.ok and res.everything is False
    assert res.chosen == ["photos", "contacts"]
    status = {i.key: i.status for i in res.items}
    assert status["photos"] == "ok" and status["contacts"] == "ok"
    assert all(v == "skipped" for k, v in status.items() if k not in ("photos", "contacts")), status
    assert res.copied_files == sv.item("photos").files
    assert os.path.isfile(os.path.join(files2, "DCIM/Camera/IMG_0001.jpg"))
    assert sorted(os.listdir(files2)) == ["DCIM", "Pictures", "sd-card-1A2B-3C4D"], os.listdir(files2)
    assert os.listdir(os.path.join(files2, "sd-card-1A2B-3C4D")) == ["DCIM"]
    assert sorted(os.listdir(res.snapshot)) == ["contacts.vcf", "manifest.json"], \
        "something that was not chosen was exported"
    assert sv.item("downloads").outcome == "not chosen"
    assert sv.item("documents").outcome == "not chosen", "not chosen must not read as none found"
    said = [t for _ok, t in log if t.startswith("Not chosen")]
    assert len(said) == 1 and "Folder: Telegram" in said[0] and "Documents" not in said[0], said
    with open(os.path.join(res.snapshot, "manifest.json")) as f:
        man = json.load(f)
    assert man["everything"] is False and man["chosen"] == ["photos", "contacts"]
    assert man["complete"] is True
    mstatus = {i["key"]: i["status"] for i in man["items"]}
    assert mstatus["folder:Telegram"] == "skipped" and mstatus["photos"] == "ok"
    assert backup.was_everything(backup.last_backup(dest2)) is False
    with open(os.path.join(dest2, "READ ME FIRST.txt")) as f:
        assert "chosen things only" in f.read()
    print("   run 6: only= copies what was chosen, skips the rest, and the "
          "manifest says it was a selection")

    # ---- run 7: something that could not be read is not "not chosen"
    os.environ["FAKE_DENY"] = "contacts"
    sv = backup.survey(d, dest2)
    c = sv.item("contacts")
    assert c.status == "unreadable" and "Export" in c.detail
    assert c.summary == "could not be read on this phone"
    readable = [i.key for i in sv.items if i.status != "unreadable"]
    res = backup.run(d, {"name": "Fake Phone"}, sv, only=readable)
    assert sv.item("contacts").status == "unreadable"
    assert sv.item("contacts").outcome == "could not be read on this phone"
    assert res.everything is True, "leaving out what cannot be read made it a selection"
    assert os.path.isfile(os.path.join(files2, "Telegram/Telegram Images/1.jpg"))
    del os.environ["FAKE_DENY"]
    print("   run 7: an unreadable item stays unreadable, never 'not chosen'")

    # ---- run 8: a phone that cannot be looked around says so, out loud
    os.environ["FAKE_DENY"] = "look"
    sv = backup.survey(d, dest2)
    other = sv.item("other")
    assert other and other.status == "unreadable" and "/sdcard" in other.detail, other
    assert other.summary == "could not be read on this phone"
    assert sv.item("folder:Telegram") is None and sv.item("photos").files == 6
    res = backup.run(d, {"name": "Fake Phone"}, sv)
    assert res.ok and sv.item("other").status == "unreadable"
    del os.environ["FAKE_DENY"]
    print("   run 8: when looking around fails, the usual folders are still "
          "saved and the gap is reported")

    # ---- run 9: a name this computer cannot store costs one file, not the run
    put("sdcard/Clash/inside.txt", b"Q" * 77)
    dest3 = backup.device_dir("Fake Phone", "FAKE0003", dest_root)
    os.makedirs(os.path.join(dest3, "files"))
    with open(os.path.join(dest3, "files", "Clash"), "w") as f:
        f.write("a file sitting where the folder needs to go")
    sv = backup.survey(d, dest3)
    res = backup.run(d, {"name": "Fake Phone"}, sv, only={"folder:Clash", "downloads"})
    clash = sv.item("folder:Clash")
    assert not res.ok and clash.status == "partial" and len(clash.failed) == 1, clash
    assert clash.failed[0].startswith("Clash/inside.txt: ")
    assert sv.item("downloads").status == "ok"
    print("   run 9: a file that cannot be stored here is listed by name and "
          "the rest still saved")

    # ---- two runs that begin in the same second must not share a snapshot.
    # The clock is frozen rather than hoped for: on a fast machine a good run
    # and a refused one really did land in one folder, and the refused run
    # appeared to hold the contacts file the good run had written. This
    # failed on GitHub's runners and never on a laptop, which is how a
    # timing bug behaves and why it is forced here.
    os.environ.pop("FAKE_DENY", None)
    dest4 = backup.device_dir("Fake Phone", "FAKE0004", dest_root)
    real_time = backup.time.time
    frozen = real_time()
    backup.time.time = lambda: frozen
    try:
        good = backup.run(d, {"name": "Fake Phone"}, backup.survey(d, dest4))
        os.environ["FAKE_DENY"] = "contacts"
        refused = backup.run(d, {"name": "Fake Phone"}, backup.survey(d, dest4))
    finally:
        backup.time.time = real_time
        os.environ.pop("FAKE_DENY", None)
    assert good.snapshot != refused.snapshot, "two runs wrote into one snapshot"
    assert os.path.isfile(os.path.join(good.snapshot, "contacts.vcf")), \
        "the fixture has no readable contacts, so this test proves nothing"
    assert not os.path.exists(os.path.join(refused.snapshot, "contacts.vcf")), \
        "a run that could not read contacts sits beside another run's contacts.vcf"
    assert backup.last_backup(dest4)["_path"] == refused.snapshot, \
        "the newest snapshot is not the one reported as the last backup"
    print("   run 10: two runs in the same second get a snapshot each")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(phone, ignore_errors=True)
        shutil.rmtree(dest_root, ignore_errors=True)
    sys.exit(code)
