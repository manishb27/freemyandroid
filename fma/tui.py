"""The terminal interface.

Written for someone who has never opened a terminal before today. That means:
arrow keys and Enter do everything, the key you need is always written at the
bottom of the screen, nothing destructive happens without a confirmation that
says what will change, and every screen can be left with Esc.

Long operations run on a worker thread with a spinner, because `dumpsys
batterystats` can take the better part of a minute and a frozen screen looks
like a crash.
"""

from __future__ import annotations

import curses
import queue
import textwrap
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from . import adb, battery, catalog, device, discover, journal, plan, profiles

C_NORMAL = 0
C_TITLE = 1
C_DIM = 2
C_SAFE = 3
C_CAUTION = 4
C_RISKY = 5
C_SEL = 6
C_OK = 7
C_BAD = 8
C_KEY = 9

RISK_COLOR = {"safe": C_SAFE, "caution": C_CAUTION, "risky": C_RISKY}


class Quit(Exception):
    pass


class Back(Exception):
    pass


def _init_colors() -> None:
    if not curses.has_colors():
        return
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    curses.init_pair(C_TITLE, curses.COLOR_CYAN, bg)
    curses.init_pair(C_DIM, curses.COLOR_WHITE, bg)
    curses.init_pair(C_SAFE, curses.COLOR_GREEN, bg)
    curses.init_pair(C_CAUTION, curses.COLOR_YELLOW, bg)
    curses.init_pair(C_RISKY, curses.COLOR_RED, bg)
    curses.init_pair(C_SEL, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(C_OK, curses.COLOR_GREEN, bg)
    curses.init_pair(C_BAD, curses.COLOR_RED, bg)
    curses.init_pair(C_KEY, curses.COLOR_MAGENTA, bg)


def attr(pair: int, bold: bool = False) -> int:
    a = curses.color_pair(pair) if curses.has_colors() else 0
    if pair == C_DIM and not curses.has_colors():
        a = curses.A_DIM
    elif pair == C_DIM:
        a |= curses.A_DIM
    if bold:
        a |= curses.A_BOLD
    return a


# --- drawing helpers --------------------------------------------------------


class Screen:
    def __init__(self, stdscr: "curses._CursesWindow") -> None:
        self.s = stdscr

    @property
    def size(self) -> tuple[int, int]:
        h, w = self.s.getmaxyx()
        return h, w

    def put(self, y: int, x: int, text: str, a: int = 0) -> None:
        h, w = self.size
        if y < 0 or y >= h or x >= w:
            return
        text = text.replace("\t", "    ")
        try:
            self.s.addnstr(y, x, text, max(0, w - x - 1), a)
        except curses.error:
            pass

    def hline(self, y: int, ch: str = "─", a: int = 0) -> None:
        _h, w = self.size
        self.put(y, 0, ch * max(0, w - 1), a)

    def header(self, title: str, subtitle: str = "") -> None:
        self.put(0, 1, "Free My Android", attr(C_TITLE, True))
        _h, w = self.size
        if title:
            self.put(0, max(18, w - len(title) - 2), title, attr(C_DIM))
        if subtitle:
            self.put(1, 1, subtitle, attr(C_DIM))
        self.hline(2 if subtitle else 1, "─", attr(C_DIM))

    def footer(self, keys: list[tuple[str, str]]) -> None:
        h, w = self.size
        self.hline(h - 2, "─", attr(C_DIM))
        x = 1
        for key, what in keys:
            if x + len(key) + len(what) + 3 >= w:
                break
            self.put(h - 1, x, key, attr(C_KEY, True))
            x += len(key) + 1
            self.put(h - 1, x, what, attr(C_DIM))
            x += len(what) + 3

    def wrap(self, y: int, x: int, text: str, a: int = 0, lines: int = 3) -> int:
        _h, w = self.size
        width = max(20, w - x - 2)
        out = textwrap.wrap(text, width)[:lines]
        for i, line in enumerate(out):
            self.put(y + i, x, line, a)
        return len(out)

    # -- input

    def key(self) -> int:
        try:
            return self.s.getch()
        except KeyboardInterrupt:
            raise Quit from None

    def busy(self, message: str, fn: Callable[[], Any]) -> Any:
        """Run `fn` on a thread while showing a spinner. Re-raises its error."""
        box: queue.Queue = queue.Queue(maxsize=1)

        def work() -> None:
            try:
                box.put(("ok", fn()))
            except BaseException as e:            # noqa: BLE001 - reported below
                box.put(("err", e))

        t = threading.Thread(target=work, daemon=True)
        t.start()
        frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        i = 0
        h, _w = self.size
        self.s.nodelay(True)
        try:
            while True:
                try:
                    kind, payload = box.get_nowait()
                except queue.Empty:
                    pass
                else:
                    if kind == "err":
                        raise payload
                    return payload
                self.put(h - 4, 1, f"{frames[i % len(frames)]} {message}",
                         attr(C_TITLE))
                self.s.refresh()
                i += 1
                time.sleep(0.08)
                if self.s.getch() == 27:          # Esc does not cancel adb, but
                    pass                          # it should not queue up either
        finally:
            self.s.nodelay(False)
            self.put(h - 4, 1, " " * 60)

    def confirm(self, title: str, lines: list[str], danger: bool = False,
                yes_word: str = "yes") -> bool:
        h, w = self.size
        box_h = min(h - 4, len(lines) + 7)
        box_w = min(w - 4, 76)
        top = (h - box_h) // 2
        left = (w - box_w) // 2
        win = curses.newwin(box_h, box_w, top, left)
        win.box()
        a = attr(C_BAD if danger else C_TITLE, True)
        try:
            win.addnstr(0, 2, f" {title} ", box_w - 4, a)
            y = 2
            for line in lines:
                for part in textwrap.wrap(line, box_w - 4) or [""]:
                    if y >= box_h - 3:
                        break
                    win.addnstr(y, 2, part, box_w - 4)
                    y += 1
            prompt = (f"Type {yes_word!r} and Enter to go ahead, Esc to cancel"
                      if danger else "Enter to go ahead, Esc to cancel")
            win.addnstr(box_h - 2, 2, prompt, box_w - 4, attr(C_DIM))
        except curses.error:
            pass
        win.refresh()

        if not danger:
            while True:
                k = win.getch()
                if k in (27, ord("q")):
                    return False
                if k in (10, 13, curses.KEY_ENTER):
                    return True
        typed = ""
        curses.echo()
        try:
            while True:
                k = win.getch()
                if k == 27:
                    return False
                if k in (10, 13, curses.KEY_ENTER):
                    return typed.strip().lower() == yes_word.lower()
                if k in (curses.KEY_BACKSPACE, 127, 8):
                    typed = typed[:-1]
                elif 32 <= k < 127:
                    typed += chr(k)
                try:
                    win.addnstr(box_h - 3, 2, (typed + " " * 20)[:box_w - 4],
                                box_w - 4)
                except curses.error:
                    pass
                win.refresh()
        finally:
            curses.noecho()

    def message(self, title: str, lines: list[str], bad: bool = False) -> None:
        h, w = self.size
        body = []
        for line in lines:
            body.extend(textwrap.wrap(line, min(w - 8, 72)) or [""])
        box_h = min(h - 2, len(body) + 5)
        box_w = min(w - 4, 76)
        win = curses.newwin(box_h, box_w, (h - box_h) // 2, (w - box_w) // 2)
        win.box()
        try:
            win.addnstr(0, 2, f" {title} ", box_w - 4,
                        attr(C_BAD if bad else C_TITLE, True))
            for i, line in enumerate(body[:box_h - 4]):
                win.addnstr(2 + i, 2, line, box_w - 4)
            win.addnstr(box_h - 2, 2, "Any key to continue", box_w - 4,
                        attr(C_DIM))
        except curses.error:
            pass
        win.refresh()
        win.getch()


# --- a reusable scrolling list ---------------------------------------------


@dataclass
class Row:
    text: str
    note: str = ""
    risk: str = ""
    selectable: bool = True
    checked: bool | None = None       # None = not a checkbox
    key: Any = None
    disabled_reason: str = ""


def _step(rows: list[Row], idx: int, delta: int) -> int:
    """Move the cursor, skipping separators and group headings.

    Without this, arrow keys stop on the '── Home screens' divider and the
    list feels broken. Falls back to plain movement if every row is
    unselectable, so a list of nothing but information still scrolls.
    """
    if not rows:
        return 0
    n = len(rows)
    if not any(r.selectable for r in rows):
        return (idx + delta) % n
    j = idx
    for _ in range(n):
        j = (j + delta) % n
        if rows[j].selectable:
            return j
    return idx


def pick(sc: Screen, title: str, subtitle: str,
         rows: list[Row] | Callable[[], list[Row]],
         keys: list[tuple[str, str]],
         checkboxes: bool = False,
         on_key: Callable[[int, int], Any] | None = None) -> Any:
    """Draw a scrolling list. Returns the chosen row's `key`, or raises Back.

    `rows` may be a list or a callable returning one. Pass a callable whenever
    `on_key` changes what the rows should say — a checkbox that only redraws
    after you leave the screen is worse than no checkbox.

    `on_key` gets (key, index) for anything this function does not handle and
    may return a sentinel to end the loop.
    """
    idx = 0
    top = 0
    while True:
        rows_now = rows() if callable(rows) else rows
        sc.s.erase()
        sc.header(title, subtitle)
        h, w = sc.size
        head = 3 if subtitle else 2
        note_space = 4
        view = max(1, h - head - note_space - 2)

        if idx < top:
            top = idx
        if idx >= top + view:
            top = idx - view + 1

        idx = max(0, min(idx, len(rows_now) - 1)) if rows_now else 0
        if not rows_now:
            sc.put(head + 1, 2, "Nothing to show.", attr(C_DIM))
        for i in range(top, min(len(rows_now), top + view)):
            r = rows_now[i]
            y = head + (i - top)
            selected = i == idx
            a = attr(C_SEL) if selected else attr(RISK_COLOR.get(r.risk, C_NORMAL))
            if not r.selectable:
                a = attr(C_DIM)
            prefix = "  "
            if checkboxes and r.checked is not None:
                prefix = "[x] " if r.checked else "[ ] "
            elif checkboxes:
                prefix = "    "
            line = f"{prefix}{r.text}"
            if selected:
                line = line.ljust(max(0, w - 2))
            sc.put(y, 1, line, a)

        cur = rows_now[idx] if rows_now else None
        ny = h - note_space - 1
        sc.hline(ny, "─", attr(C_DIM))
        if cur:
            if cur.disabled_reason:
                sc.wrap(ny + 1, 2, f"Kept: {cur.disabled_reason}",
                        attr(C_CAUTION), lines=2)
            elif cur.note:
                sc.wrap(ny + 1, 2, cur.note, attr(C_DIM), lines=2)
        sc.footer(keys)
        sc.s.refresh()

        k = sc.key()
        if k in (ord("q"),):
            raise Quit
        if k == 27:
            raise Back
        if k in (curses.KEY_UP, ord("k")):
            idx = _step(rows_now, idx, -1)
        elif k in (curses.KEY_DOWN, ord("j")):
            idx = _step(rows_now, idx, +1)
        elif k == curses.KEY_NPAGE:
            idx = min(len(rows_now) - 1, idx + view)
        elif k == curses.KEY_PPAGE:
            idx = max(0, idx - view)
        elif k == curses.KEY_HOME:
            idx = 0
        elif k == curses.KEY_END:
            idx = max(0, len(rows_now) - 1)
        elif (k in (10, 13, curses.KEY_ENTER) and rows_now
              and rows_now[idx].selectable):
            return rows_now[idx].key
        elif on_key is not None:
            out = on_key(k, idx)
            if out is not None:
                return out
