"""The record of everything we changed, so everything can be put back.

One file per phone, under ~/.local/share/freemyandroid/journal/. Written after
*every* run, including runs that failed halfway: a half-applied run is exactly
when you most need the undo list.

The journal is the product, not a log. If this file is lost, the phone can
still be fixed by hand, but nothing here can do it for you.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .plan import Action, invert

SCHEMA = 1


def data_dir() -> str:
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "freemyandroid")


def journal_dir() -> str:
    d = os.path.join(data_dir(), "journal")
    os.makedirs(d, exist_ok=True)
    return d


def _slug(serial: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", serial) or "unknown"


@dataclass
class Run:
    id: str
    when: float
    profile: str
    device: dict[str, Any]
    actions: list[Action] = field(default_factory=list)

    @property
    def when_text(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.when))

    @property
    def applied(self) -> list[Action]:
        return [a for a in self.actions if a.ok]

    @property
    def failed(self) -> list[Action]:
        return [a for a in self.actions if not a.ok]

    @property
    def reversible(self) -> list[Action]:
        return [a for a in self.applied if a.undo]

    @property
    def summary(self) -> str:
        n = len(self.reversible)
        bad = len(self.failed)
        s = f"{self.when_text}  {self.profile}  {n} change{'s' if n != 1 else ''}"
        if bad:
            s += f", {bad} failed"
        return s

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "when": self.when,
            "profile": self.profile,
            "device": self.device,
            "actions": [a.to_json() for a in self.actions],
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Run":
        return cls(
            id=d.get("id", ""),
            when=d.get("when", 0.0),
            profile=d.get("profile", "?"),
            device=d.get("device", {}),
            actions=[Action.from_json(a) for a in d.get("actions", [])],
        )

    def undo_plan(self) -> list[Action]:
        """Reversed order: the last thing done is the first thing undone."""
        out = []
        for a in reversed(self.applied):
            inv = invert(a)
            if inv:
                out.append(inv)
        return out


def path_for(serial: str) -> str:
    return os.path.join(journal_dir(), f"{_slug(serial)}.json")


def load(serial: str) -> list[Run]:
    p = path_for(serial)
    if not os.path.isfile(p):
        return []
    try:
        with open(p, encoding="utf-8") as f:
            blob = json.load(f)
    except (OSError, json.JSONDecodeError):
        # A corrupt journal is moved aside rather than silently replaced, so
        # the changes it recorded are not lost for good.
        try:
            os.replace(p, p + f".broken-{int(time.time())}")
        except OSError:
            pass
        return []
    return [Run.from_json(r) for r in blob.get("runs", [])]


def save(serial: str, runs: list[Run], device: dict[str, Any] | None = None) -> str:
    p = path_for(serial)
    from . import __version__
    blob = {
        "schema": SCHEMA,
        "written_by": f"free-my-android {__version__}",
        "serial": serial,
        "device": device or (runs[-1].device if runs else {}),
        "runs": [r.to_json() for r in runs],
    }
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(blob, f, indent=1, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    return p


def record(serial: str, profile: str, device: dict[str, Any],
           actions: list[Action]) -> Run:
    runs = load(serial)
    run = Run(
        id=f"{int(time.time())}-{len(runs) + 1}",
        when=time.time(),
        profile=profile,
        device=device,
        actions=actions,
    )
    runs.append(run)
    save(serial, runs, device)
    return run


def all_disabled_by_us(serial: str) -> set[str]:
    """Packages this tool turned off and has not turned back on."""
    state: dict[str, bool] = {}
    for run in load(serial):
        for a in run.applied:
            if a.kind == "disable":
                state[a.target] = True
            elif a.kind == "enable":
                state[a.target] = False
    return {p for p, off in state.items() if off}
