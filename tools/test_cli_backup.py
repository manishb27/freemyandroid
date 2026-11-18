#!/usr/bin/env python3
"""Runs `./freemyandroid backup` the way an assistant or a script does.

No hardware needed: tools/fake_adb.py stands in for adb. Run by
tools/check.sh.

What this proves: `backup --check --json` prints one JSON object and nothing
else, with everything an assistant needs to tell a person what was found and
ask what to keep; `--only` and `--skip` copy what they say and no more; a
mistyped key or option stops with the choices listed instead of backing up
something else; and none of it writes to the phone.

What it cannot prove: anything about a real phone. See docs/BACKUP.md.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

ITEM_FIELDS = {"key", "label", "kind", "files", "bytes", "new_files",
               "new_bytes", "count", "status", "summary"}


def main() -> int:
    phone = tempfile.mkdtemp(prefix="fakephone-")
    backups = tempfile.mkdtemp(prefix="fakebackup-")

    def put(rel: str, data: bytes) -> None:
        p = os.path.join(phone, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)

    def phone_now() -> list[tuple[str, int, int]]:
        out = []
        for base, _dirs, names in os.walk(phone):
            for n in names:
                p = os.path.join(base, n)
                st = os.stat(p)
                out.append((os.path.relpath(p, phone), st.st_size, st.st_mtime_ns))
        return sorted(out)

    def saved() -> list[str]:
        out = []
        for base, _dirs, names in os.walk(backups):
            out += [os.path.relpath(os.path.join(base, n), backups) for n in names]
        return sorted(out)

    env = dict(os.environ, FMA_ADB=os.path.join(HERE, "fake_adb.py"),
               FAKE_PHONE=phone, FMA_BACKUP_DIR=backups)
    env.pop("FAKE_DENY", None)

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, os.path.join(ROOT, "freemyandroid"), "backup", *args],
            capture_output=True, text=True, env=env, timeout=120)

    try:
        put("sdcard/DCIM/Camera/IMG_1.jpg", b"A" * 4000)
        put("sdcard/DCIM/Camera/IMG_2.jpg", b"B" * 5000)
        put("sdcard/Download/bill.pdf", b"C" * 700)
        put("sdcard/Telegram/Telegram Images/t.jpg", b"D" * 900)
        put("sdcard/My 'stuff'; ls/x.txt", b"E" * 60)
        put("sdcard/notes.txt", b"F" * 30)
        put("content/contacts_ids.txt", b"Row: 0 _id=1\n")
        put("content/contacts_data.txt",
            b"Row: 0 contact_id=1, mimetype=vnd.android.cursor.item/name, "
            b"data2=Asha, data3=Rao, data4=NULL, data5=NULL, data1=Asha Rao\n")
        before = phone_now()

        # ---- --check --json: one JSON object on stdout, nothing else
        p = run("--check", "--json", "FAKE0001")
        assert p.returncode == 0, p.stderr
        blob = json.loads(p.stdout)             # raises if anything else was printed
        assert isinstance(blob, dict)
        for field in ("device", "dest", "free_bytes", "last_backup", "items", "not_saved"):
            assert field in blob, f"--json is missing {field!r}"
        assert blob["device"]["serial"] == "FAKE0001"
        assert blob["dest"].startswith(backups) and blob["free_bytes"] > 0
        assert blob["last_backup"] == "" and blob["last_backup_everything"] is None
        for item in blob["items"]:
            assert ITEM_FIELDS <= set(item), f"item is missing {ITEM_FIELDS - set(item)}"
            assert re.fullmatch(r"[A-Za-z0-9._:-]+", item["key"]), item["key"]
        by_key = {i["key"]: i for i in blob["items"]}
        assert by_key["photos"]["files"] == 2 and by_key["photos"]["new_bytes"] == 9000
        assert by_key["photos"]["summary"] == "2 files, 9 KB"
        assert by_key["contacts"]["count"] == 1 and by_key["contacts"]["kind"] == "contacts"
        assert by_key["folder:Telegram"]["label"] == "Folder: Telegram"
        assert by_key["folder:Telegram"]["discovered"] is True
        assert by_key["loose"]["files"] == 1
        odd = [i for i in blob["items"] if i["label"] == "Folder: My 'stuff'; ls"]
        assert len(odd) == 1 and odd[0]["files"] == 1
        assert blob["not_saved"][0]["what"].startswith("Authenticator"), \
            "the one that locks people out is not first"
        assert all(n["what"] and n["why"] for n in blob["not_saved"])
        assert saved() == [], "--check copied something"
        print(f"   backup --check --json: one JSON object, {len(blob['items'])} "
              "items with keys, sizes and what cannot be saved")

        # ---- --check, for a person: each line ends with the key to type
        p = run("--check", "FAKE0001")
        assert p.returncode == 0
        assert re.search(r"Photos and videos\s+2 files, 9 KB\s+\[photos\]", p.stdout), p.stdout
        assert "[folder:Telegram]" in p.stdout and "[loose]" in p.stdout
        assert "Nothing was copied" in p.stdout and saved() == []
        print("   backup --check: every line shows the key to use with --only")

        # ---- mistakes stop, and say what the choices are
        p = run("--json", "FAKE0001")
        assert p.returncode != 0 and "--check" in p.stderr and p.stdout == ""
        p = run("--only", "photos,nope", "FAKE0001")
        assert p.returncode != 0 and "'nope'" in p.stderr, (p.returncode, p.stderr)
        assert "photos" in p.stderr and "folder:Telegram" in p.stderr
        p = run("--skip", "nope", "FAKE0001")
        assert p.returncode != 0 and "'nope'" in p.stderr
        p = run("--onyl", "photos", "FAKE0001")
        assert p.returncode != 0 and "unknown option" in p.stderr
        p = run("--only", "photos", "--skip", "photos", "FAKE0001")
        assert p.returncode != 0 and "nothing to back up" in p.stderr
        p = run("--check", "--json", "--only", "nope", "FAKE0001")
        assert p.returncode != 0 and p.stdout == ""
        assert saved() == [], "a refused command still copied something"
        print("   unknown keys and options are refused with the valid choices, "
              "and nothing is copied")

        # ---- --only photos: photos land, and nothing else does
        p = run("--only", "photos", "FAKE0001")
        assert p.returncode == 0, p.stdout + p.stderr
        dest = blob["dest"]
        got = saved()
        files = sorted(x for x in got if "/files/" in x)
        assert [os.path.relpath(x, os.path.relpath(dest, backups)) for x in files] == [
            "files/DCIM/Camera/IMG_1.jpg", "files/DCIM/Camera/IMG_2.jpg"], got
        assert not any(x.endswith(("contacts.vcf", "apps.txt")) for x in got), got
        manifests = [x for x in got if x.endswith("manifest.json")]
        assert len(manifests) == 1
        with open(os.path.join(backups, manifests[0])) as f:
            man = json.load(f)
        assert man["everything"] is False and man["chosen"] == ["photos"]
        assert {i["key"]: i["status"] for i in man["items"]}["downloads"] == "skipped"
        assert "not a backup of everything" in p.stdout
        assert "Not chosen" in p.stdout and "Downloads" in p.stdout
        blob2 = json.loads(run("--check", "--json", "FAKE0001").stdout)
        assert blob2["last_backup"] == "today" and blob2["last_backup_everything"] is False
        assert {i["key"]: i for i in blob2["items"]}["photos"]["new_files"] == 0
        print("   backup --only photos: only the photos landed, and the "
              "manifest says it was a selection")

        # ---- --skip: everything but. Then a plain run is everything.
        p = run("--skip", "downloads,loose", "FAKE0001")
        assert p.returncode == 0, p.stdout + p.stderr
        got = saved()
        assert any(x.endswith("files/Telegram/Telegram Images/t.jpg") for x in got)
        assert any(x.endswith("files/My 'stuff'; ls/x.txt") for x in got)
        assert any(x.endswith("contacts.vcf") for x in got)
        assert not any(x.endswith(("bill.pdf", "files/notes.txt")) for x in got), got
        p = run("FAKE0001")
        assert p.returncode == 0 and "Backup complete." in p.stdout, p.stdout
        got = saved()
        assert any(x.endswith("files/Download/bill.pdf") for x in got)
        assert any(x.endswith("files/notes.txt") for x in got)
        blob3 = json.loads(run("--check", "--json", "FAKE0001").stdout)
        assert blob3["last_backup_everything"] is True and blob3["new_files"] == 0
        print("   backup --skip leaves out what it names; a plain backup is "
              "everything and says so")

        # ---- and through all of that, the phone was only ever read
        assert phone_now() == before, "something on the phone was changed"
        print("   the phone is byte-for-byte as it was")
        return 0
    finally:
        shutil.rmtree(phone, ignore_errors=True)
        shutil.rmtree(backups, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
