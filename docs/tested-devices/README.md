# Tested devices

One file per device that has been run end to end, with what was done, what it
measurably changed, and what broke. These are reports, not marketing: a device
page that only lists wins is not worth reading.

| Device | Android | Profile applied | Outcome | Report |
|---|---|---|---|---|
| Samsung Galaxy M21 (SM-M215F) | 12 (API 31) | `fast/extreme` + launcher swap | 71 packages off, ~110 MB freed, 0 failures | [samsung-galaxy-m21.md](samsung-galaxy-m21.md) |

## Adding your own

A useful report has four things:

1. **Identity** — `./freemyandroid info` output verbatim.
2. **Before and after**, measured the same way. For memory, the only reading
   worth quoting is a paired before/after taken minutes apart around a single
   change; Android's `MemFree` drifts by hundreds of megabytes on its own.
3. **What the guards refused**, and whether they were right.
4. **What broke.** This is the part people actually need. "Nothing broke" is a
   valid and useful answer, but say what you checked.

Attach the relevant journal file
(`~/.local/share/freemyandroid/journal/<serial>.json`) if you are filing an
issue — it records every change with its exact inverse, so a problem can be
traced to one line.
