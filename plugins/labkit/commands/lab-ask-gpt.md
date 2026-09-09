---
description: Send a self-contained question to the local GPT/Codex, file the exchange, index the verdict
---

Get an independent read from the other model and keep a record that can be re-examined later.

Resolve `<PLUGIN_ROOT>` by walking up from this command/skill file to the nearest ancestor containing `.claude-plugin/plugin.json` or `.codex-plugin/plugin.json`. Replace the placeholder below with that absolute path; it is not an environment variable. Do not assume a fixed directory depth: Codex can load a migrated command from a deeper directory.

Save the exact inlined context so the exchange can be reviewed later. This also supports hosts
where the other model cannot read local files. A Windows setup previously failed with
`CreateProcessWithLogonW failed: 1385`; treat that as a host-specific failure, not a universal
restriction. The continuous user-selected model workflow is provided separately by `lab-orchestrate`.

Steps:

1. Locate the project root by walking up for `.labkit.json`; read `slug`. `cd` there.

2. Gather the context the question actually needs. Be selective - inlined context is the whole
   prompt, and a bloated prompt buys a vaguer answer:
   - relevant `notes/findings/*.md` (the evidence blocks, not just the claims)
   - `lab_mem.py recall --project <slug> --kind constraint "<question>"` for prior decisions and
     limits; add a second call without `--kind` for findings
   - a `graphify query` result if the question turns on what a source says
   - the specific code or data under discussion, pasted in full

3. **Write the prompt directly into `handoffs/<YYYY-MM-DD>-<slug>.prompt.md`** with the Write tool -
   not to a scratch directory, and never onto the command line, where quotes, backticks, `$` and
   newlines do not survive PowerShell argument parsing. Saving it in the project is the point: a
   record that only *describes* what was inlined cannot be re-examined, and the earlier version of
   this command failed exactly that way.

   Structure: the question, then `## Context` with each inlined piece under its own heading naming
   where it came from. Ask for a direct answer plus where it disagrees with your framing, and state
   that this request should be answered from the supplied context only.

4. Locate the installed `codex-bridge` skill and read its instructions. Resolve its script path
   from that installation rather than assuming another host has the same home directory.
   Call its ask mode with the host's shell tool, pointing at the saved prompt:

```
& "<installed-codex-bridge>/scripts/codex-bridge.ps1" `
  -Mode ask -PromptFile "<abs path to the .prompt.md>" -Effort high -KeepArtifacts
```

Use `-Mode ask` for this context-only second opinion. If the optional bridge is missing, report
the missing dependency instead of inventing its path or claiming the handoff succeeded.

5. File `handoffs/<YYYY-MM-DD>-<slug>.md` next to the prompt: a link to the `.prompt.md`, a table of
   what was inlined and from where, the full answer, the model name and session id from the bridge's
   meta block, and your own verdict on where the two models agree or diverge.

6. Index the outcome, attributed to the other model:

```
python "<PLUGIN_ROOT>/scripts/lab_mem.py" capture --project <slug> \
  --text "<what GPT concluded, self-contained>" \
  --source "handoffs/<file>.md" --kind finding --agent codex
```

`--agent codex` is what later lets a recall separate "we established this" from "the other model
thought this".

7. **If the answer invalidates an existing finding, amend it properly.** Edit the finding file, then
   re-capture it with `--supersede <id>` for every id in its `mem0_rows:` frontmatter and
   `--finding-file` pointing at it. Editing the file alone leaves mem0 serving the refuted claim
   under a citation that now contradicts it. `lab_init.py status` exits 2 and names the file until
   this is done.

Reporting back: lead with where the two models **disagree**. Independent agreement is worth one
line; a disagreement is the reason the call was made. Do not smooth over a divergence to sound
consistent.
