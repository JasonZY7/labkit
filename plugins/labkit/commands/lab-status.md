---
description: Report what each labkit layer holds, and where the layers have drifted apart
---

Show the state of all four layers, and any drift between them.

Resolve `<PLUGIN_ROOT>` by walking up from this command/skill file to the nearest ancestor containing `.claude-plugin/plugin.json` or `.codex-plugin/plugin.json`. Replace the placeholder below with that absolute path; it is not an environment variable. Do not assume a fixed directory depth: Codex can load a migrated command from a deeper directory.

```
python "<PLUGIN_ROOT>/scripts/lab_init.py" status
```

It walks up for `.labkit.json`, so it works from any subdirectory. Pass a directory argument to check
a different project, or `--json` for a machine-readable form.

**Exit codes are the contract:** `0` = all four layers in sync, `2` = drift found, `1` = not a labkit
project. The checks are in the script, not in this file, so they run the same way every time.

What it detects beyond the counts:

- **STALE INDEX** - a finding whose content changed after it was indexed. mem0 is still serving the
  superseded claim under a `source:` pointer to the amended file. The output prints the exact
  `--supersede <id>` lines needed to fix it; pass them to `/labkit:lab-capture`.
- **never indexed** - a finding file with no `mem0_rows`, which will not surface in a future recall.
- **dangling mem0_rows** - ids recorded in a file that no longer exist in the namespace.
- **mismatched finding pointers** - a row claims a different finding path from the file listing its
  id, including findings renamed without updating their memory index.
- **corpus staleness** - `raw/` newer than `graphify-out/graph.json`, so the graph predates the
  sources. Missing graphs with source files and unreadable graphs also exit `2`. Suggest
  `/labkit:lab-graphify --update`; an empty project does not need a graph yet.
- **unsaved handoff prompts** - a handoff record with no matching `.prompt.md`, meaning that exchange
  cannot be reproduced.

Relay what it found. Do not re-derive the checks yourself or add your own; if something is missing
from the report, the fix belongs in `lab_init.py`, not in a paragraph here that a future session may
or may not follow.

Keep the summary to a few lines when everything is in sync. This is a glance, not a report.
