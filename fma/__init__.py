"""Free My Android — debloat an Android phone over ADB.

Two interfaces over one engine:

    ./freemyandroid        a terminal interface (curses)
    ./freemyandroid web    a local web page, for when the person deciding is
                           not the person at the keyboard

Python 3.10+ and the standard library. `adb` is the only external binary, and
`fma.platform_tools` will fetch that too.
"""

__version__ = "1.0.2"
__license__ = "MIT"
__url__ = "https://github.com/manishb27/freemyandroid"

# Bumped together with a CHANGELOG.md entry. The version is reported by
# `./freemyandroid --version`, shown in the web page footer, and written into
# every journal entry, so a bug report can be tied to the code that caused it.
