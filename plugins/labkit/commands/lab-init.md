---
description: Scaffold a labkit research project - four layers, routing rules, git repo
---

Create a new labkit research project, or add the labkit layout to an existing folder.

Resolve `<PLUGIN_ROOT>` by walking up from this command/skill file to the nearest ancestor containing `.claude-plugin/plugin.json` or `.codex-plugin/plugin.json`. Replace the placeholder below with that absolute path; it is not an environment variable. Do not assume a fixed directory depth: Codex can load a migrated command from a deeper directory.

Run:

```
python "<PLUGIN_ROOT>/scripts/lab_init.py" init <directory> --name "<Human readable name>"
```

Argument handling:

- The user's argument is the target directory. If they gave a bare name rather than a path, create it under the current working directory.
- If they gave no argument at all, use the current directory and take `--name` from its folder name.
- Pass `--force` only if the user explicitly asks to overwrite an existing scaffold. Without it, existing files are left alone and reported as `skip`.

After it runs:

1. Read the generated `CLAUDE.md` aloud in summary - the routing table is the whole point, and the user should know it exists.
2. Tell them the mem0 namespace (`proj-<slug>`), because that is what isolates this project's memory from every other one.
3. Suggest the next concrete step based on what is already in the folder: if `raw/` has files, suggest `/labkit:lab-graphify`; if it is empty, suggest dropping sources in first.

Do not run `/graphify` automatically. Building a graph costs real time and tokens, and the user may want to curate `raw/` first.
