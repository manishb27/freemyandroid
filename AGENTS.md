# Free My Android: guide for AI assistants

The full guide is [CLAUDE.md](CLAUDE.md). Read it first and follow it — it is
written for any assistant, not only Claude Code.

The short version, if you read nothing else:

- This tool disables packages on a phone someone depends on. The guards in
  `fma/profiles.py` and `data/protected.json` are the product. Do not weaken
  them to make a profile look more impressive.
- Never add `pm uninstall` anywhere. `pm disable-user` is reversible; uninstall
  is not.
- Every action must record its exact inverse at apply time. If you add an
  action kind to `fma/plan.py`, add its inverse in the same commit.
- A package entry without a `note` saying what breaks is not finished.
- Most people asking for help here do not know what a terminal is. Explain
  each step plainly, and ask before anything that changes their phone.
