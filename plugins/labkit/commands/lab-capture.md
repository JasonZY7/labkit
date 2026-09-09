---
description: Record a durable research finding into notes/findings/ and index it in mem0
---

Write one finding into the project's two write-side layers. The file is the record; mem0 is the
index into it.

Resolve `<PLUGIN_ROOT>` by walking up from this command/skill file to the nearest ancestor containing `.claude-plugin/plugin.json` or `.codex-plugin/plugin.json`. Replace the placeholder below with that absolute path; it is not an environment variable. Do not assume a fixed directory depth: Codex can load a migrated command from a deeper directory.

**Both writes, or neither.** A finding that exists only in mem0 has lost its exact numbers and its
citation, because mem0 stores an LLM paraphrase. A finding that exists only as a file will not be
recalled in a future session.

Steps:

1. Locate the project root by walking up for `.labkit.json`. If there is none, stop and offer
   `/labkit:lab-init`. Read `slug` from it and `cd` there.

2. Establish the finding with the user if any of these is missing: the claim, the exact supporting
   quote, the source identifier, the page or section. Do not invent a citation, and do not soften a
   number. If the user cannot supply a source, write `unsourced` in the `source:` field rather than
   leaving it blank.

3. Write `notes/findings/<YYYY-MM-DD>-<short-slug>.md` using `notes/findings/_TEMPLATE.md` as the
   shape. The `## Evidence` block holds the quote verbatim - no paraphrase, no ellipsis that removes
   a qualifier. Leave `mem0_rows` and `mem0_indexed_hash` alone; step 4 writes them.

4. Index it, pointing `--source` at the file and `--finding-file` at the same file so the produced
   row ids land in its frontmatter:

```
python "<PLUGIN_ROOT>/scripts/lab_mem.py" capture --project <slug> \
  --text "<one sentence, self-contained, no pronouns>" \
  --source "notes/findings/<file>.md" --finding-file "notes/findings/<file>.md" \
  --kind finding --agent claude
```

`--kind` is not decoration - `/labkit:lab-recall` filters on it, and that filter is far more reliable than
raw semantic score. Use `decision` for a choice and its reason, `constraint` for a hard limit on the
project, `preference` for how the user wants work done, `finding` for something established from
evidence.

5. Append a one-line entry to `notes/log.md` under today's date, linking the finding file.

6. Report the row ids. Expect more than one: mem0 splits a sentence carrying several facts into
   separate rows, and `capture` prints how many it produced.

## Amending an existing finding

Editing the file is only half the job. The mem0 rows still carry the old claim, under a `source:`
pointer to a file that now contradicts it - an index that lies while citing something correct.

```
python "<PLUGIN_ROOT>/scripts/lab_mem.py" capture --project <slug> \
  --text "<the corrected claim>" \
  --source "notes/findings/<file>.md" --finding-file "notes/findings/<file>.md" \
  --supersede <old-id> --supersede <another-old-id> --kind finding --agent claude
```

One `--supersede` per id in the file's `mem0_rows:` frontmatter. `lab_init.py status` exits 2 and
prints the exact lines for any finding whose content changed since it was indexed.

Guards that make this safe, each from a reviewed failure mode:

- **Re-capturing an amended finding without `--supersede` is refused.** Allowing it would keep the
  old rows *and* stamp a fresh hash, leaving status reporting "in sync" over an index that
  contradicts the file. Existing rows without a recorded hash also need full supersede. Pass
  `--allow-orphans` only when the old rows are deliberately kept; status continues to report drift.
- **The finding must be readable and have frontmatter before any remote write or delete.** Capture
  saves an explicit `finding_file` metadata pointer so status can detect a missing or renamed file,
  even when `--source` is an external citation. If omitted, `--source` defaults to the finding file.
- **Old rows are deleted only after the replacement is confirmed live**, and only ids confirmed
  *gone* are removed from `mem0_rows`. A failed delete stays listed, so it cannot become an orphan
  no later status run can see.
- **A row is only adopted as this capture's own** if it carries the metadata this call sent and the
  id set has held steady across two polls. Without both, a concurrent capture's row could be written
  into the wrong finding, and a split write's later rows would be silently dropped.
- **If the file changes while the capture is in flight**, the ids are recorded but the hash is not
  stamped: the rows describe the version that was sent, so status keeps reporting drift.
- **`--supersede` refuses an id from another project.** A memory id is deletable with these
  credentials regardless of namespace, so a mistyped id would otherwise destroy another project's
  fact. A listed id absent from this namespace can be retired from the local file, but never
  authorizes a remote delete.

If mem0's extraction finds no durable fact in `--text`, capture exits non-zero, supersedes nothing
and touches no file. Vague or self-referential sentences get dropped silently by mem0; state a
concrete fact with its subject named.

Keep `--text` self-contained. mem0 rows are retrieved out of context; "it improved by 12%" is
useless six weeks later, "swapping to flash attention cut step time 12% on the 7B run" is not.
