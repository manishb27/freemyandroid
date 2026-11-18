#!/usr/bin/env python3
"""Drives the real pick() in a pseudo-terminal and presses keys at it.

This exists because of a bug that shipped from 1.0.0 to 1.1.0: on the review
screen the footer said "Enter  apply", and Enter ticked the highlighted box
instead. pick() returned the row under the cursor before the screen's own
handler was asked, so the plan could not be applied from the terminal
interface at all. Nothing caught it, because nothing pressed a key.

What this proves is the key handling in fma/tui.py. It does not prove any
screen in fma/app.py end to end; those need a phone, or a fake one with
packages on it, which tools/fake_adb.py does not have yet.
"""

import os
import select
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

try:
    import pty
except ImportError:                      # not a Unix: nothing to drive it with
    print("   skipped: no pseudo-terminals on this platform")
    sys.exit(0)


def press(keys: bytes, *, with_handler: bool) -> str:
    """Run pick() over three ticked rows, send `keys`, return what came back."""
    fd_out, result = tempfile.mkstemp(prefix="fma-tui-")
    os.close(fd_out)
    os.unlink(result)
    pid, fd = pty.fork()
    if pid == 0:
        sys.path.insert(0, ROOT)
        import curses
        from fma import tui

        ticked = [True, True, True]

        def main(stdscr):
            sc = tui.Screen(stdscr)

            def rows():
                return [tui.Row(text=f"row {i}", key=f"ROW{i}", checked=ticked[i])
                        for i in range(3)]

            def on_key(k, i):
                if k == ord(" "):
                    ticked[i] = not ticked[i]
                    return None
                if k in (10, 13, curses.KEY_ENTER):
                    return ("go", None)
                return None

            return tui.pick(sc, "Review", "", rows, [("Enter", "apply")],
                            checkboxes=True,
                            on_key=on_key if with_handler else None)

        try:
            out = curses.wrapper(main)
            text = f"{out!r} ticked={ticked!r}"
        except BaseException as e:       # Back, Quit, or a real failure
            text = f"raised {type(e).__name__}"
        with open(result, "w") as f:
            f.write(text)
        os._exit(0)

    time.sleep(0.8)                      # let curses draw before typing at it
    for b in keys:
        os.write(fd, bytes([b]))
        time.sleep(0.15)
    deadline = time.time() + 6
    done = False
    while time.time() < deadline and not done:
        if select.select([fd], [], [], 0.2)[0]:
            try:
                os.read(fd, 65536)
            except OSError:
                pass
        done = os.waitpid(pid, os.WNOHANG)[0] != 0
    if not done:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
        return "(still waiting for a key)"
    with open(result) as f:
        text = f.read()
    os.unlink(result)
    return text


def main() -> int:
    os.environ.setdefault("TERM", "xterm")

    got = press(b"\r", with_handler=True)
    assert got == "('go', None) ticked=[True, True, True]", \
        f"Enter on the review screen did not apply: {got}"
    print("   Enter reaches the screen's own handler: the plan can be applied")

    got = press(b" \r", with_handler=True)
    assert got == "('go', None) ticked=[False, True, True]", got
    print("   Space unticks the row under the cursor, and Enter still applies")

    got = press(b"\r", with_handler=False)
    assert got.startswith("'ROW0'"), got
    print("   a plain list still returns the row Enter was pressed on")

    got = press(b"\x1b", with_handler=True)
    assert got == "raised Back", got
    print("   Esc goes back")
    return 0


if __name__ == "__main__":
    sys.exit(main())
