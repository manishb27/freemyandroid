#!/usr/bin/env python3
"""Loads the web interface the way a browser does, against a fake phone.

This test exists because of a bug that shipped in 1.0.0 and survived to
1.0.2: the page loaded with no stylesheet and no script in every real
browser, because the server wanted the token on /static/ requests and a
browser does not put one there. It had been "verified" with curl, with the
token appended to those URLs by hand — which tested an assumption, not the
page.

So the rule here: fetch index.html, read which assets *it* asks for, and
request each one exactly as written.

Also covers the backup end to end over HTTP: the one-click button, and the
quieter path beside it — look at the phone first, tick what to keep, back up
only that.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def get(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def post(url: str, body: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return {"__status": e.code}


def wait_idle(base: str, tok: str, seconds: float = 40.0) -> dict:
    deadline = time.time() + seconds
    while time.time() < deadline:
        _c, body = get(f"{base}/api/state?t={tok}")
        state = json.loads(body)
        if not state["busy"]:
            return state
        time.sleep(0.25)
    raise AssertionError("server stayed busy")


def main() -> int:
    phone = tempfile.mkdtemp(prefix="fakephone-")
    backups = tempfile.mkdtemp(prefix="fakebackup-")
    os.makedirs(os.path.join(phone, "sdcard/DCIM/Camera"))
    os.makedirs(os.path.join(phone, "content"))
    for i in range(3):
        with open(os.path.join(phone, f"sdcard/DCIM/Camera/IMG_{i}.jpg"), "wb") as f:
            f.write(bytes([65 + i]) * (4000 + i))
    with open(os.path.join(phone, "content/contacts_ids.txt"), "w") as f:
        f.write("Row: 0 _id=1\n")
    with open(os.path.join(phone, "content/contacts_data.txt"), "w") as f:
        f.write("Row: 0 contact_id=1, mimetype=vnd.android.cursor.item/name, "
                "data2=Asha, data3=Rao, data4=NULL, data5=NULL, data1=Asha Rao\n")

    env = dict(os.environ, FMA_ADB=os.path.join(HERE, "fake_adb.py"),
               FAKE_PHONE=phone, FMA_BACKUP_DIR=backups, PYTHONUNBUFFERED="1")
    env.pop("FAKE_DENY", None)
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "freemyandroid"), "web",
         "--no-browser", "--port", "0"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    try:
        url = ""
        deadline = time.time() + 15
        while time.time() < deadline and not url:
            line = proc.stdout.readline()
            m = re.search(r"(http://127\.0\.0\.1:\d+)/\?t=([A-Za-z0-9_-]+)", line or "")
            if m:
                base, tok = m.group(1), m.group(2)
                url = m.group(0)
        assert url, "the server never printed its link"

        # ---- the page, and the token on it
        assert get(base + "/")[0] == 403, "page served without the token"
        assert get(base + "/?t=wrong")[0] == 403
        code, html = get(url)
        assert code == 200
        page = html.decode()

        # ---- every asset, requested exactly as the page names it
        assets = re.findall(r'(?:href|src)="(/static/[^"]+)"', page)
        assert assets, "index.html references no assets — test is out of date"
        for a in assets:
            code, body = get(base + a)
            assert code == 200 and len(body) > 200, \
                f"{a} -> HTTP {code}: a browser would load this page broken"
        print(f"   page loads as a browser loads it: {', '.join(assets)}")

        # ---- nothing else leaks out of web/ without the token
        assert get(base + "/static/index.html")[0] == 404
        assert get(base + "/static/../fma/web.py")[0] in (403, 404)
        assert get(base + "/api/state")[0] == 403, "API readable without the token"
        assert post(base + "/api/backup", {}).get("__status") == 403
        assert post(base + "/api/backup/look", {}).get("__status") == 403
        print("   API and everything else still need the token")

        # ---- ids the script reaches for must exist in the page
        with open(os.path.join(ROOT, "web/app.js"), encoding="utf-8") as f:
            js = f.read()
        wanted = set(re.findall(r'\$\("([a-z0-9-]+)"\)', js))
        missing = sorted(i for i in wanted if f'id="{i}"' not in page)
        assert not missing, f"app.js uses ids the page does not have: {missing}"
        print(f"   all {len(wanted)} element ids the script uses exist in the page")

        # ---- one-click backup, over HTTP
        assert post(f"{base}/api/pick?t={tok}", {"serial": "FAKE0001"}).get("queued")
        state = wait_idle(base, tok)
        assert state["device"] and state["backup"]["last"] == ""
        assert post(f"{base}/api/backup?t={tok}", {}).get("queued")
        state = wait_idle(base, tok)
        b = state["backup"]
        assert b["last"] == "today" and b["last_complete"] is True, b
        assert b["result"]["ok"] and b["result"]["copied_files"] == 3, b["result"]
        assert b["progress"] is None
        labels = {i["label"]: i["status"] for i in b["result"]["items"]}
        assert labels["Photos and videos"] == "ok" and labels["Contacts"] == "ok"
        assert len(b["result"]["not_saved"]) >= 4
        saved = os.path.join(b["dest"], "files/DCIM/Camera/IMG_2.jpg")
        assert os.path.getsize(saved) == 4002
        print("   one-click backup: 3 files and contacts saved, verified, summarised")

        # ---- pressing it again copies nothing
        assert post(f"{base}/api/backup?t={tok}", {}).get("queued")
        state = wait_idle(base, tok)
        assert state["backup"]["result"]["copied_files"] == 0
        assert state["backup"]["result"]["everything"] is True
        assert state["backup"]["last_everything"] is True
        print("   second press copies nothing new")

        # ---- look first: more has appeared on the phone since
        def put(rel: str, data: bytes) -> None:
            p = os.path.join(phone, "sdcard", rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(data)

        put("DCIM/Camera/IMG_9.jpg", b"Z" * 1111)
        put("Telegram/Telegram Images/t.jpg", b"T" * 2222)
        put("My 'stuff'; ls/x.txt", b"S" * 333)
        put("notes.txt", b"N" * 44)
        dest = state["backup"]["dest"]
        assert state["backup"]["survey"] is None
        assert post(f"{base}/api/backup/look?t={tok}", {}).get("queued")
        state = wait_idle(base, tok)
        sv = state["backup"]["survey"]
        assert sv and state["backup"]["progress"] is None and not state["error"]
        items = {i["key"]: i for i in sv["items"]}
        assert items["photos"]["files"] == 4 and items["photos"]["new_files"] == 1
        assert items["folder:Telegram"]["label"] == "Folder: Telegram"
        assert items["folder:Telegram"]["summary"].startswith("1 file, ")
        assert items["loose"]["new_bytes"] == 44
        assert any(i["label"] == "Folder: My 'stuff'; ls" for i in sv["items"])
        assert all(re.fullmatch(r"[A-Za-z0-9._:-]+", k) for k in items), list(items)
        assert sv["new_bytes"] == 1111 + 2222 + 333 + 44 and sv["new_files"] == 4
        assert sv["free_bytes"] > 0
        assert not os.path.exists(os.path.join(dest, "files/Telegram")), \
            "looking copied something"
        print(f"   look first: {len(items)} things listed with sizes, nothing copied")

        # ---- a key the page was never shown is refused, and nothing starts
        r = post(f"{base}/api/backup?t={tok}", {"only": ["photos", "nope"]})
        assert "nope" in r.get("error", "") and not r.get("queued"), r
        assert "folder:Telegram" in r["error"], "the error does not list the choices"
        r = post(f"{base}/api/backup?t={tok}", {"only": []})
        assert r.get("error") and not r.get("queued"), r
        r = post(f"{base}/api/backup?t={tok}", {"only": "photos"})
        assert r.get("error") and not r.get("queued"), r
        _c, body = get(f"{base}/api/state?t={tok}")
        assert not json.loads(body)["busy"]
        print("   unknown or empty selections are refused before anything runs")

        # ---- back up what is ticked: that, and only that
        assert post(f"{base}/api/backup?t={tok}",
                    {"only": ["folder:Telegram", "contacts"]}).get("queued")
        state = wait_idle(base, tok)
        b = state["backup"]
        res = b["result"]
        assert res and res["ok"] and res["everything"] is False, res
        assert res["copied_files"] == 1
        got = {i["key"]: i for i in res["items"]}
        assert got["folder:Telegram"]["status"] == "ok"
        assert got["photos"]["status"] == "skipped"
        assert got["photos"]["summary"] == "not chosen"
        assert got["loose"]["summary"] == "not chosen"
        assert got["messages"]["summary"] == "not chosen"
        assert os.path.getsize(os.path.join(dest, "files/Telegram/Telegram Images/t.jpg")) == 2222
        assert not os.path.exists(os.path.join(dest, "files/DCIM/Camera/IMG_9.jpg"))
        assert not os.path.exists(os.path.join(dest, "files/notes.txt"))
        assert b["last"] == "today" and b["last_everything"] is False, \
            "a backup of a selection is being reported as a backup of everything"
        assert b["survey"] is None, "a spent list is still on offer"
        print("   back up what is ticked: only that copied, the rest shown as "
              "'not chosen', and the page knows it was a selection")

        # ---- a list belongs to one phone: picking again throws it away
        assert post(f"{base}/api/backup/look?t={tok}", {}).get("queued")
        assert wait_idle(base, tok)["backup"]["survey"]
        assert post(f"{base}/api/pick?t={tok}", {"serial": "FAKE0001"}).get("queued")
        assert wait_idle(base, tok)["backup"]["survey"] is None
        # With no list to check against, the server looks again for itself.
        assert post(f"{base}/api/backup?t={tok}", {"only": ["nope"]}).get("queued")
        state = wait_idle(base, tok)
        assert state["backup"]["result"] is None
        assert any("nope" in s["text"] and not s["ok"] for s in state["steps"])
        assert post(f"{base}/api/backup?t={tok}", {"only": ["loose"]}).get("queued")
        state = wait_idle(base, tok)
        assert state["backup"]["result"]["copied_files"] == 1
        assert os.path.getsize(os.path.join(dest, "files/notes.txt")) == 44
        print("   picking a phone clears the list; without one the server "
              "looks again before copying")

        # ---- and the one button still means everything
        assert post(f"{base}/api/backup?t={tok}", {}).get("queued")
        state = wait_idle(base, tok)
        b = state["backup"]
        assert b["result"]["ok"] and b["result"]["everything"] is True
        assert b["result"]["copied_files"] == 2 and b["last_everything"] is True
        assert os.path.getsize(os.path.join(dest, "files/DCIM/Camera/IMG_9.jpg")) == 1111
        assert os.path.getsize(os.path.join(dest, "files/My 'stuff'; ls/x.txt")) == 333
        print("   'Back up everything' picks up all that was left out")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(phone, ignore_errors=True)
        shutil.rmtree(backups, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
