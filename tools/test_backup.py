#!/usr/bin/env python3
"""End-to-end test of the backup pipeline against a fake phone.

No hardware needed: tools/fake_adb.py stands in for adb and runs the real
shell commands against a fixture directory. Run by tools/check.sh.

What this proves: survey, copy, verify, incremental re-runs, changed files,
SD cards, hostile file names, skipped caches, and a phone that refuses the
contacts and messages providers.

What it cannot prove: that a given real phone lets ADB read its contacts.
Some do not, which is exactly why the refusal path is tested here.
"""

import json
import os
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
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(phone, ignore_errors=True)
        shutil.rmtree(dest_root, ignore_errors=True)
    sys.exit(code)
