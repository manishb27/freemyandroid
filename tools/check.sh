#!/usr/bin/env bash
# Everything CI runs, locally, in one command.
#
# This script exists because the 1.0.0 release shipped with red CI: the data
# and profile checks had been run by hand, the undefined-name check had not,
# and it was the one that caught a real bug. Running a subset by hand does not
# work. Run this.
#
#   ./tools/check.sh
#
# Nothing here touches a phone.

set -uo pipefail
cd "$(dirname "$0")/.."

fail=0
step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
ok()   { printf '   \033[32mok\033[0m %s\n' "$1"; }
bad()  { printf '   \033[31mFAIL\033[0m %s\n' "$1"; fail=1; }

PY=${PYTHON:-python3}

step "python version"
$PY - <<'EOF' && ok "3.10+" || bad "needs python 3.10+"
import sys
sys.exit(0 if sys.version_info >= (3, 10) else 1)
EOF

step "no third-party dependencies"
if [ -f requirements.txt ] || [ -f pyproject.toml ] || [ -f setup.py ]; then
  bad "a packaging/dependency file appeared; this project is stdlib-only"
else
  ok "none declared"
fi

step "compile"
if $PY -m compileall -q fma freemyandroid; then ok "all modules compile"; else bad "compile errors"; fi

step "import every module"
$PY - <<'EOF' && ok "all modules import" || bad "an import failed"
import importlib, pathlib, sys
sys.path.insert(0, ".")
for p in sorted(pathlib.Path("fma").glob("*.py")):
    if p.stem != "__init__":
        importlib.import_module(f"fma.{p.stem}")
EOF

step "python 3.10 syntax compatibility"
$PY - <<'EOF' && ok "no 3.12-only syntax" || bad "syntax that needs python 3.12"
# A backslash inside an f-string *expression* is legal from 3.12 (PEP 701)
# and a SyntaxError before it. A 3.12 interpreter compiles it happily, so
# running the checks on a new Python cannot see the problem — which is how
# 1.0.1 shipped a file that would not even parse on 3.10.
#
# ast.parse(feature_version=(3, 10)) does not help: the change is in the
# tokeniser, not the grammar. tokenize does help, because from 3.12 the
# pieces of an f-string arrive as separate tokens, so a string literal
# holding a backslash *inside* an f-string is visible.
import io, pathlib, sys, tokenize

if sys.version_info < (3, 12):
    print("   running on <3.12; the compile step already catches this")
    sys.exit(0)

problems = 0
for p in sorted(pathlib.Path("fma").glob("*.py")) + [pathlib.Path("freemyandroid")]:
    src = p.read_text()
    depth = 0
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            name = tokenize.tok_name.get(tok.type, "")
            if name == "FSTRING_START":
                depth += 1
            elif name == "FSTRING_END":
                depth = max(0, depth - 1)
            elif depth and name == "STRING" and "\\" in tok.string:
                print(f"   {p}:{tok.start[0]}: backslash in an f-string "
                      f"expression ({tok.string}) — SyntaxError before 3.12. "
                      f"Move it to a constant or a local.")
                problems += 1
    except (tokenize.TokenError, IndentationError) as e:
        print(f"   {p}: could not tokenise: {e}")
        problems += 1
sys.exit(1 if problems else 0)
EOF

step "undefined names"
$PY - <<'EOF' && ok "no undefined names" || bad "undefined names"
import ast, builtins, pathlib, sys
problems = 0
for p in sorted(pathlib.Path("fma").glob("*.py")):
    tree = ast.parse(p.read_text(), str(p))
    defined = set(dir(builtins)) | {"__file__", "__name__", "__doc__"}
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(n.name)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            defined.add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                defined.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.arg):
            defined.add(n.arg)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            defined.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            defined.update(n.names)
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) \
                and n.id not in defined:
            print(f"   {p}:{n.lineno}: undefined name {n.id!r}")
            problems += 1
sys.exit(1 if problems else 0)
EOF

step "data files"
$PY - <<'EOF' && ok "data valid" || bad "data problems"
import json, pathlib, sys
sys.path.insert(0, ".")
from fma import adb

RISKS = {"safe", "caution", "risky"}
problems, seen = [], {}
for p in sorted(pathlib.Path("data/packages").glob("*.json")):
    blob = json.loads(p.read_text())
    for key in ("id", "name", "applies_to", "packages"):
        if key not in blob:
            problems.append(f"{p.name}: missing {key!r}")
    for e in blob.get("packages", []):
        pid = e.get("id", "")
        if not adb.PKG_RE.match(pid):
            problems.append(f"{p.name}: {pid!r} is not a package name")
        if e.get("risk") not in RISKS:
            problems.append(f"{p.name}: {pid} has risk {e.get('risk')!r}")
        if not e.get("tags"):
            problems.append(f"{p.name}: {pid} has no tags")
        if not e.get("label"):
            problems.append(f"{p.name}: {pid} has no label")
        if e.get("risk") in ("caution", "risky") and not e.get("note"):
            problems.append(f"{p.name}: {pid} is {e['risk']} with no note")
        for v in blob["applies_to"]:
            if (v, pid) in seen:
                problems.append(f"{pid} duplicated for {v} in {p.name} and {seen[(v,pid)]}")
            seen[(v, pid)] = p.name

prot = json.loads(pathlib.Path("data/protected.json").read_text())
for pid in prot["never_disable"]:
    if pid != "android" and not adb.PKG_RE.match(pid):
        problems.append(f"protected.json: {pid!r} is not a package name")

roles = json.loads(pathlib.Path("data/roles.json").read_text())
for r in prot["roles_that_need_a_replacement_first"]:
    if r not in roles["roles"]:
        problems.append(f"protected.json guards unknown role {r!r}")
for r in roles["elder_keep_roles"]:
    if r not in roles["roles"]:
        problems.append(f"elder_keep_roles has unknown role {r!r}")

apps = json.loads(pathlib.Path("data/apps.json").read_text())
ids = set()
for a in apps["apps"]:
    if a["id"] in ids:
        problems.append(f"apps.json: duplicate id {a['id']!r}")
    ids.add(a["id"])
    if a["source"] not in ("fdroid", "github"):
        problems.append(f"apps.json: {a['id']} source {a['source']!r} not allowed")
    if a["source"] == "github" and not a.get("repo"):
        problems.append(f"apps.json: {a['id']} is github with no repo")
    if a.get("fills_role") and a["fills_role"] not in roles["roles"]:
        problems.append(f"apps.json: {a['id']} fills unknown role {a['fills_role']!r}")

for p in sorted(pathlib.Path("data/profiles").glob("*.json")):
    blob = json.loads(p.read_text())
    for t in ("standard", "extreme", "ultra"):
        if t not in blob["intensity"]:
            problems.append(f"{p.name}: no {t} intensity")
    if blob.get("strategy") == "denylist":
        tiers = [set(blob["intensity"][t].get("tags", []))
                 for t in ("standard", "extreme", "ultra")]
        if not (tiers[0] <= tiers[1] <= tiers[2]):
            problems.append(f"{p.name}: intensity tags are not nested")

for line in problems:
    print("  ", line)
print(f"   {len(seen)} package entries, {len(ids)} apps")
sys.exit(1 if problems else 0)
EOF

step "guards and profiles (synthetic device, no phone needed)"
$PY - <<'EOF' && ok "guards hold, intensities nested" || bad "guard or profile problem"
import sys
sys.path.insert(0, ".")
from fma import device, profiles

prof = device.Profile(serial="ci", manufacturer="samsung", brand="samsung",
                      model="SM-CI", android="12", sdk=31, vendor_key="samsung")
pkgs = profiles.load_packages()
prof.installed = {e.id for e in pkgs["*"] + pkgs["samsung"]}
prof.installed |= {"com.sec.android.app.launcher", "com.android.systemui"}

ctx = profiles.build_context(prof, quick=True)
ctx.current_launcher = "com.sec.android.app.launcher"
ctx.launchers = ["com.sec.android.app.launcher"]
ctx.role_holders = {"launcher": "com.sec.android.app.launcher",
                    "dialer": "com.samsung.android.dialer",
                    "sms": "com.samsung.android.messaging"}
ctx.launchable = set(prof.installed)

must_refuse = ["android", "com.android.systemui", "com.google.android.gms",
               "com.qualcomm.qti.whatever", "com.sec.android.app.launcher",
               "com.samsung.android.dialer", "com.samsung.android.messaging",
               "com.samsung.android.networkstack",
               "com.google.android.marvin.talkback"]
leaked = [p for p in must_refuse if not ctx.guard(p)]
if leaked:
    print("   guard allowed:", leaked)
    sys.exit(1)
print(f"   guards refuse all {len(must_refuse)} protected packages")

for spec in profiles.load_profiles():
    prev = None
    for tier in ("standard", "extreme", "ultra"):
        pl = profiles.build(ctx, spec["id"], tier)
        targets = {a.target for a in pl.actions if a.kind == "disable"}
        bad = {t for t in targets if ctx.guard(t)}
        if bad:
            print(f"   {spec['id']}/{tier} plans guarded packages: {bad}")
            sys.exit(1)
        if spec.get("strategy") == "denylist" and prev is not None and not prev <= targets:
            print(f"   {spec['id']}/{tier} dropped {prev - targets} from the previous tier")
            sys.exit(1)
        prev = targets
        print(f"   {spec['id']}/{tier}: {len(pl.actions)} actions")
EOF

step "journal round-trip for every action kind"
$PY - <<'EOF' && ok "all kinds round-trip and invert" || bad "journal round-trip broken"
import sys
sys.path.insert(0, ".")
from fma.plan import Action, invert
cases = [("disable", "com.a.b", ""), ("enable", "com.a.b", ""),
         ("setting", "global/x", "1"), ("unset", "global/x", ""),
         ("doze_allow", "com.a.b", ""), ("doze_deny", "com.a.b", ""),
         ("home", "com.a/.B", ""), ("role", "sms/com.a", ""),
         ("ime", "com.a/.Ime", ""), ("install", "com.a.b", "/tmp/x.apk")]
for kind, target, value in cases:
    a = Action(kind=kind, target=target, value=value, label="x")
    a.undo = {"kind": kind, "target": target, "value": value}
    back = Action.from_json(a.to_json())
    assert back.kind == kind and back.target == target, kind
    assert invert(back) is not None, kind
    back.describe()
print(f"   {len(cases)} action kinds")
EOF

step "entry point"
if ./freemyandroid --version >/dev/null 2>&1; then ok "$(./freemyandroid --version | head -1)"; else bad "--version failed"; fi
if ./freemyandroid --help >/dev/null 2>&1; then ok "--help"; else bad "--help failed"; fi

echo
if [ "$fail" -eq 0 ]; then
  printf '\033[32mAll checks passed.\033[0m\n'
else
  printf '\033[31mSomething failed. Do not push.\033[0m\n'
fi
exit "$fail"
