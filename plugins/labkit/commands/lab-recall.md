---
description: Answer a question by querying all four labkit layers and citing which one answered
---

Answer the user's question about this project by going to the layers that can actually answer it,
then saying where each part of the answer came from.

Resolve `<PLUGIN_ROOT>` by walking up from this command/skill file to the nearest ancestor containing `.claude-plugin/plugin.json` or `.codex-plugin/plugin.json`. Replace the placeholder below with that absolute path; it is not an environment variable. Do not assume a fixed directory depth: Codex can load a migrated command from a deeper directory.

Locate the project root by walking up for `.labkit.json`; read `slug`. If there is none, stop and
offer `/labkit:lab-init`.

**Route before querying.** Read the question and decide which layers apply. Querying all four every
time is slow and dilutes the answer with weak matches.

| Question shape | Layer | Command |
|---|---|---|
| what a source says, how sources connect, where a number came from | corpus | `graphify query "<question>"` |
| what we concluded, tried, or dropped | notes | grep `notes/log.md` and `notes/findings/` |
| project constraints, decisions, user preferences | memory | `lab_mem.py recall --project <slug> --kind <kind> "<question>"` |
| what the other model thought | collab | read `handoffs/` |

## Use --kind

```
python "<PLUGIN_ROOT>/scripts/lab_mem.py" recall --project <slug> \
  --kind constraint "<question>" --top-k 5
```

Raw semantic score over a mixed namespace is unreliable: in testing, a question about whether a
paper was in the corpus returned three loosely-related findings and *not* the constraint that
answered it directly, with every score between 0.11 and 0.33. Filtering by kind fixed it. Map the
question shape to the kind:

- "can we / are we allowed to / what is missing" → `--kind constraint`
- "why did we choose / why did we drop" → `--kind decision`
- "what did we establish / what does the evidence say" → `--kind finding`
- "how does the user want this done" → `--kind preference`

Run it twice with different kinds rather than once unfiltered when the question spans both.

Skip the corpus layer when `graphify-out/graph.json` does not exist - say the graph has not been
built rather than silently returning less.

## Writing the answer

- Lead with the answer, then attribute. Naming the finding file beats an unattributed assertion.
- When two layers disagree, say so and name both. The notes file wins over a mem0 row: mem0 holds a
  paraphrase, the file holds the quote.
- `recall` marks any hit below 0.15 as a weak match. Treat those as "worth checking", never as
  established.
- Before trusting a mem0 row, consider running `lab_init.py status`. If it reports the row's finding
  file as a STALE INDEX, the row is describing a superseded version of that finding and the file is
  authoritative.
- If nothing in any layer answers the question, say that plainly. Do not fill the gap from general
  knowledge without labelling it as such - the whole point of the layers is that project claims are
  traceable.
