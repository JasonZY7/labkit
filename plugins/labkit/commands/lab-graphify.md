---
description: Build or refresh the corpus layer - a graphify knowledge graph over raw/
---

Build the corpus layer for this labkit project.

This command does not reimplement graphify. It **invokes the `graphify` skill** and binds it to
labkit's paths, then records the result in the notes layer. Read the graphify SKILL.md and follow
its pipeline; everything below is the labkit-specific binding plus the traps that have actually
bitten this setup.

## Bindings

| graphify expects | labkit value |
|---|---|
| `INPUT_PATH` | `raw` - the corpus is source material only, never `notes/` or `handoffs/` |
| working directory | the project root (the dir holding `.labkit.json`) - `graphify-out/` is resolved relative to cwd, so getting this wrong scatters output |
| `SPEC_PATH` | `references/extraction-spec.md` beside the graphify SKILL.md you loaded |

Locate the project root by walking up for `.labkit.json`. If there is none, stop and offer
`/labkit:lab-init`. `cd` there before the first graphify command and stay there.

Pass `--update` through when the user asks for a refresh rather than a rebuild.

## Traps this setup has already hit

- **`.gitignore` blinds the corpus.** graphify honours gitignore. An earlier version of the labkit
  scaffold ignored `raw/*`, and `detect` reported 1 file / 39 words for a six-source corpus with no
  error. If detect returns far fewer files than `raw/` contains, check `.gitignore` first.
- **`raw` is the input, not `.`.** Running graphify on `.` pulls `notes/` and `handoffs/` into the
  graph, which collapses the corpus/notes boundary the whole routing table depends on.
- **Semantic extraction depends on the host.** Follow the installed graphify skill's available
  extraction path. If it calls for agent extraction, use this host's write-capable subagents;
  verify that chunk files were actually saved. Do not assume Claude agent type names exist on Codex.
- **Papers need a PDF reader.** Discover this host's PDF tools. Use an installed reader such as
  PyMuPDF (`fitz`) when the native PDF path is unavailable; do not assume `pdftoppm` is installed.

## After the build

1. Report the shape plainly: nodes, edges, communities, and the community labels you assigned.
2. Surface any `GRAPH HEALTH WARNING` from graphify's Step 4.5 integrity check rather than
   swallowing it, and write it into `notes/log.md` so it is not rediscovered next session.
3. Append a dated entry to `notes/log.md`: what went into the corpus, the graph shape, and anything
   the graph revealed that is worth a finding.
4. Run `lab_init.py status` - it compares the newest mtime in `raw/` against `graphify-out/graph.json`
   and will now report the corpus as fresh.

Do not capture graph statistics into mem0. Node counts are not durable facts; they change on every
rebuild and will read as stale claims within a week.
