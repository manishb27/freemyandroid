#!/usr/bin/env python3
"""A stand-in for `adb`, so the backup pipeline can be tested with no phone.

Point FMA_ADB at this file and FAKE_PHONE at a directory laid out like a
phone's storage:

    $FAKE_PHONE/sdcard/DCIM/Camera/a.jpg
    $FAKE_PHONE/storage/1A2B-3C4D/DCIM/b.jpg        (an SD card, optional)
    $FAKE_PHONE/content/contacts_data.txt            (canned `content query`)
    $FAKE_PHONE/content/sms.txt
    $FAKE_PHONE/content/calls.txt
    $FAKE_PHONE/packages.txt

It implements only what fma/backup.py sends, and it does so by running the
real shell command against the fixture directory with the paths rewritten —
so `find … -exec stat -c` is the genuine article, not an imitation of it.

Anything it does not recognise exits non-zero with a message, so a new adb
call in the code shows up as a failing test instead of silently passing.

Set FAKE_DENY=contacts,sms to make those providers answer "Permission
Denial", which is what some real phones do. FAKE_DENY=look makes the loop
that lists the top of storage print nothing, as it would on a phone whose
`stat` is too old to describe what it finds.
"""

import os
import shutil
import subprocess
import sys

ROOT = os.environ.get("FAKE_PHONE", "")
DENY = {x for x in os.environ.get("FAKE_DENY", "").split(",") if x}


def to_local(path: str) -> str:
    if path.startswith("/sdcard"):
        return ROOT + path
    if path.startswith("/storage"):
        return ROOT + path
    return path


def main() -> int:
    args = sys.argv[1:]
    if not ROOT or not os.path.isdir(ROOT):
        print("fake_adb: FAKE_PHONE is not a directory", file=sys.stderr)
        return 2
    if args[:1] == ["start-server"]:
        return 0
    if args[:1] == ["devices"]:
        print("List of devices attached\nFAKE0001               device usb:1-1 "
              "product:fake model:Fake_Phone device:fake transport_id:1")
        return 0
    if len(args) >= 2 and args[0] == "-s":
        args = args[2:]

    if args[:1] == ["pull"]:
        rest = [a for a in args[1:] if a != "-a"]
        if len(rest) != 2:
            print("fake_adb: pull wants <remote> <local>", file=sys.stderr)
            return 2
        src, dst = to_local(rest[0]), rest[1]
        if not os.path.isfile(src):
            print(f"adb: error: failed to stat remote object '{rest[0]}': "
                  "No such file or directory", file=sys.stderr)
            return 1
        shutil.copy2(src, dst)
        print(f"{rest[0]}: 1 file pulled, 0 skipped.")
        return 0

    if args[:1] == ["shell"] and len(args) == 2:
        cmd = args[1]

        if cmd.startswith("content query"):
            uri = cmd.split("--uri", 1)[1].split()[0]
            name = {"content://com.android.contacts/data": ("contacts", "contacts_data.txt"),
                    "content://com.android.contacts/contacts": ("contacts", "contacts_ids.txt"),
                    "content://sms": ("sms", "sms_ids.txt" if cmd.endswith("--projection _id") else "sms.txt"),
                    "content://call_log/calls": ("calls", "calls_ids.txt" if cmd.endswith("--projection _id") else "calls.txt"),
                    }.get(uri)
            if not name:
                print(f"Error while accessing provider: unknown url {uri}", file=sys.stderr)
                return 1
            kind, fname = name
            if kind in DENY:
                print("Error while accessing provider:" + uri.replace("content://", "")
                      + "\njava.lang.SecurityException: Permission Denial: opening provider",
                      file=sys.stderr)
                return 1
            path = os.path.join(ROOT, "content", fname)
            if not os.path.isfile(path):
                print("No result found.")
                return 0
            sys.stdout.write(open(path, encoding="utf-8").read())
            return 0

        if cmd.startswith("pm list packages"):
            path = os.path.join(ROOT, "packages.txt")
            if os.path.isfile(path):
                sys.stdout.write(open(path, encoding="utf-8").read())
            return 0

        # `for f in '/sdcard'/*; do …` is how the backup looks around the
        # top of storage. Like the others it is run for real, so the quoting
        # the code sends is the quoting a shell actually has to survive.
        if "look" in DENY and cmd.startswith("for f in "):
            return 0
        if cmd.startswith(("find ", "[ -d ", "ls ", "for f in ")):
            local = cmd.replace("'/sdcard", f"'{ROOT}/sdcard") \
                       .replace("'/storage", f"'{ROOT}/storage") \
                       .replace("ls /storage", f"ls {ROOT}/storage")
            # Run inside the fixture, so that if a hostile folder name ever
            # did get executed, what it left behind would land where a test
            # can see it and not in whatever directory the test started from.
            p = subprocess.run(["bash", "-c", local], capture_output=True,
                               text=True, cwd=ROOT)
            sys.stdout.write(p.stdout.replace(ROOT + "/sdcard", "/sdcard")
                                     .replace(ROOT + "/storage", "/storage"))
            return 0

    print(f"fake_adb: not implemented: {sys.argv[1:]}", file=sys.stderr)
    return 99


if __name__ == "__main__":
    sys.exit(main())
