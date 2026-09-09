#!/usr/bin/env python3
"""labkit project scaffolder and layer health check.

    lab_init.py init <dir> [--name "Human readable name"] [--force]
    lab_init.py status [<dir>] [--json]

init lays out a research project with four layers that do not overlap:

    raw/            source material - PDFs, saved pages, transcripts
    graphify-out/   generated knowledge graph over raw/ (built by /labkit:lab-graphify)
    notes/          what we concluded, in plain Markdown, under git
    handoffs/       cross-model exchanges with GPT/Codex, self-contained

and writes shared project rules in AGENTS.md with a CLAUDE.md compatibility
entrypoint, so both local hosts use the same records and routing rules.

status is the deterministic half of the health check. It reports what each layer
holds and, crucially, where the layers have drifted out of sync:

  * a finding file with no mem0_rows has never been indexed and will not surface
    in a future recall;
  * a finding file modified after its newest indexed row means mem0 is still
    serving the superseded claim while citing a file that now says otherwise -
    the failure mode that makes an index worse than no index;
  * mem0_rows naming ids that no longer exist are dangling.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover - non-reconfigurable stream
        pass

AGENTS_MD = """# {name}

labkit research project. Slug `{slug}`.
These are the canonical project rules for Claude Code, Codex, and local ChatGPT
runtimes with filesystem and terminal access. `CLAUDE.md` imports this file.

## Layer routing - read before answering anything about this project

Four layers. Each answers a different question shape. Sending a question to the
wrong layer produces a confident answer built on the wrong evidence, so route first.

| The question sounds like | Layer | How to answer it |
|---|---|---|
| "what does paper X say", "where did this number come from", "how do these sources connect" | corpus | `graphify query "..."` against `graphify-out/`, built from `raw/` by `/labkit:lab-graphify` |
| "what did we conclude", "what did we try", "why did we drop that" | notes | read/grep `notes/log.md` and `notes/findings/` |
| "what are this project's constraints", "what did we decide", "what does the user prefer" | memory | `lab_mem.py recall --project {slug} --kind constraint "..."` |
| "what did the other model think", "what happened in the run" | collab | read `handoffs/`, including `handoffs/runs/` |

If a question spans layers, query them in that order and say which layer each part
of the answer came from. Never answer a corpus question from memory rows - memory
holds paraphrased conclusions, not source text.

## Writing rules

- A durable fact goes in TWO places: a file under `notes/findings/` with the exact
  quote and citation, and a one-line summary in mem0 via `lab_mem.py capture` with
  `--source` pointing back at that file and `--finding-file` pointing at it too, so
  the produced row ids land in the file's `mem0_rows:` frontmatter.
- **Amending a finding means re-capturing it.** Editing the file alone leaves mem0
  serving the old claim under a citation that now contradicts it. Re-capture with
  `--supersede <old id>` for each id in `mem0_rows:`. `lab_init.py status` flags
  files that have drifted.
- Never put an exact number, quote, or citation only in mem0. mem0 paraphrases.
- `raw/` is append-only. Do not edit source material.
- `graphify-out/` is generated. Never hand-edit it; rebuild with `/labkit:lab-graphify --update`.

## Cross-model collaboration

Use the installed `lab-orchestrate` skill, or `/labkit:lab-run` in Claude Code, for
the shared goal controller. At each new goal or user-requested resume, ask which
models should be brains and which should execute; use an explicit answer already
given in that request without asking again. The first brain is the lead orchestrator,
other brains advise, and every brain independently reviews the entire goal.
All selected roles work in this project and follow these rules. Resolve labkit scripts
relative to the installed skill; the plugin directory and project directory are
separate paths.

The controller records each run under `handoffs/runs/` with its frozen goal,
acceptance criteria, selected team, prompts, results and `state.json`. Codex models
use Codex CLI; Claude models use Claude CLI from Codex and native Workflow agents
inside Claude Code. Follow the shared skill for run, resume, and submission.
Match each native report to the exact pending artifact and runtime transcript.
Inspect partial changes after interruption before any further execution.
When switching between Claude Code and Codex, use the installed lab-handoff skill.
It saves visible history, explicit decisions and memory sources in .labkit-local/,
then verifies the same checkout before the receiving host accepts. Read the handoff
before continuing a goal; do not resume while transfer is ready or another host owns
the project. This shared-files workflow does not automatically transfer native chat
context, running processes, permissions or global account memories.
Read the recorded state before reporting completion; exhausted rounds or a failed
stage are unfinished work. Keep exact prompts and model attribution for separate
handoffs too.

Use the tools actually available in the local runtime. A web-only ChatGPT session
needs a connector to reach these local files and CLIs. Check current capabilities
instead of treating a historical host failure as a permanent project constraint.

## Memory namespace

mem0 `user_id` for this project is `proj-{slug}`. Keep it out of other projects.
"""

CLAUDE_MD = """# Claude Code entrypoint

@AGENTS.md

Project rules are maintained in AGENTS.md. Follow that file.
"""

GITIGNORE = """# generated knowledge graph - rebuild with: /labkit:lab-graphify --update
graphify-out/*
!graphify-out/GRAPH_REPORT.md

# raw/ is TRACKED on purpose. graphify honours .gitignore, so ignoring raw/ makes
# the whole corpus invisible to the corpus layer - detect reports 0 files and the
# graph comes out empty. If a specific source is too large or too restricted to
# commit, add a targeted rule for that one file, e.g.:
#   raw/huge-dataset.zip

.venv/
__pycache__/
*.pyc
.DS_Store
"""

LOG_MD = """# Research log - {name}

Append-only. Newest entry at the bottom. One entry per working session.

Each entry answers three things: what I tried, what came out, what it changes.
Numbers and quotes belong here verbatim; the paraphrase goes to mem0, not the
other way round.

---

## {today} - project created

Layers initialised. Nothing established yet.
"""

SOURCES_MD = """# Sources

One row per item in `raw/`. Fill this in as you add material, so a citation can be
resolved without opening the PDF.

| File | Title | Authors | Year | Identifier | Added |
|---|---|---|---|---|---|
"""

FINDING_TMPL = """---
finding: <one-line claim, stated so it can be true or false>
source: <arXiv id, DOI, URL, or raw/ filename>
page: <page or section>
established: <YYYY-MM-DD>
by: <claude | codex | chatgpt | human>
confidence: <high | medium | low>
mem0_rows: []
---

<!--
mem0_rows is written by `lab_mem.py capture --finding-file <this file>`. Leave it
alone by hand. If you amend the body of this finding, re-capture with
`--supersede <id>` for every id listed there, or mem0 keeps serving the old claim
while citing this file. `lab_init.py status` flags the drift.
-->

## Claim

<the claim, restated precisely>

## Evidence

> <exact quote, verbatim, with no paraphrase>

## Why it matters here

<what this changes for the project>

## Caveats

<sample size, scope limits, anything that would make this not generalise>
"""

README_HANDOFFS = """# Handoffs

Cross-model exchanges. Two files per round trip:

- `YYYY-MM-DD-<slug>.prompt.md` - the exact prompt that was sent, byte for byte
- `YYYY-MM-DD-<slug>.md` - the record: what was inlined, the answer, the verdict

Keep each exact prompt so a disagreement can be re-examined against the context
that was supplied. Record tool results and model attribution alongside the answer.

The `lab-orchestrate` skill (Claude command `/labkit:lab-run`) stores controller records in
`runs/<run-id>/`: the task, numbered
planner/worker prompts and results, events, and `state.json`. Both local hosts use
the same run directory for status and resume.
"""


def write(path: Path, text: str, force: bool) -> str:
    if path.exists() and not force:
        return "skip   " + str(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return "write  " + str(path)


def cmd_init(args) -> int:
    root = Path(args.directory).expanduser().resolve()
    config_path = root / ".labkit.json"
    if config_path.exists() and not args.force:
        try:
            existing = json.loads(config_path.read_text(encoding="utf-8"))
            name, slug = existing["name"], existing["slug"]
            if not all(isinstance(value, str) and value.strip() for value in (name, slug)):
                raise ValueError("name and slug must be non-empty strings")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print("cannot read existing project identity: {0}".format(exc))
            return 1
    else:
        name = args.name or root.name
        slug = "".join(c if (c.isalnum() or c in "-_") else "-" for c in name.strip().lower())
        slug = "-".join(p for p in slug.split("-") if p) or "project"

    root.mkdir(parents=True, exist_ok=True)
    today = datetime.now().date().isoformat()
    actions = []

    for d in ("raw", "notes/findings", "handoffs", "graphify-out"):
        (root / d).mkdir(parents=True, exist_ok=True)
    for keep in ("raw/.gitkeep", "notes/findings/.gitkeep", "handoffs/.gitkeep"):
        p = root / keep
        if not p.exists():
            p.write_text("", encoding="utf-8")

    actions.append(write(root / "AGENTS.md", AGENTS_MD.format(name=name, slug=slug), args.force))
    actions.append(write(root / "CLAUDE.md", CLAUDE_MD, args.force))
    actions.append(write(root / ".gitignore", GITIGNORE, args.force))
    actions.append(write(root / "notes" / "log.md",
                         LOG_MD.format(name=name, today=today), args.force))
    actions.append(write(root / "raw" / "SOURCES.md", SOURCES_MD, args.force))
    actions.append(write(root / "notes" / "findings" / "_TEMPLATE.md", FINDING_TMPL, args.force))
    actions.append(write(root / "handoffs" / "README.md", README_HANDOFFS, args.force))
    actions.append(write(config_path, json.dumps({
        "name": name,
        "slug": slug,
        "created": today,
        "mem0_namespace": "proj-" + slug,
        "layers": {
            "corpus": "graphify-out/",
            "notes": "notes/",
            "memory": "mem0:proj-" + slug,
            "collab": "handoffs/",
        },
    }, indent=2, ensure_ascii=False) + "\n", args.force))

    if not (root / ".git").exists():
        if shutil.which("git"):
            subprocess.run(["git", "init", "-q"], cwd=str(root), check=False)
            actions.append("write  " + str(root / ".git") + "  (git init)")
        else:
            actions.append("skip   git init - git not on PATH")

    print("\n".join(actions))
    print("\nproject : {0}\nslug    : {1}\nmem0    : proj-{1}".format(root, slug))
    print("\nnext:")
    print("  1. drop sources into raw/, then run:  /labkit:lab-graphify")
    print("  2. record a finding:                  /labkit:lab-capture")
    print("  3. ask across all layers:             /labkit:lab-recall <question>")
    print("  4. plan, execute and review a task:    lab-orchestrate skill (Claude: /labkit:lab-run)")
    return 0


def _find_root(start: Path):
    for candidate in [start] + list(start.parents):
        if (candidate / ".labkit.json").exists():
            return candidate
    return None


def _mtime(path: Path):
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def _corpus_state(root: Path):
    graph = root / "graphify-out" / "graph.json"
    skip = {".gitkeep", "SOURCES.md"}
    sources = [p for p in (root / "raw").rglob("*")
               if p.is_file() and p.name not in skip]
    if not graph.exists():
        return {"built": False, "sources": len(sources),
                "note": "no graph yet ({0} source file(s)) - run /labkit:lab-graphify".format(len(sources))}
    try:
        g = json.loads(graph.read_text(encoding="utf-8"))
        nodes, edges = len(g.get("nodes", [])), len(g.get("edges", []) or g.get("links", []))
    except (ValueError, OSError):
        return {"built": True, "sources": len(sources), "error": True,
                "note": "graph.json unreadable"}
    newest = max((_mtime(p) for p in sources), default=None)
    stale = bool(newest and newest > _mtime(graph))
    note = "graph built, {0} nodes / {1} edges".format(nodes, edges)
    if stale:
        note += "  [STALE: raw/ is newer - run /labkit:lab-graphify --update]"
    return {"built": True, "nodes": nodes, "edges": edges, "sources": len(sources),
            "stale": stale, "note": note}


def _notes_state(root: Path, rows_by_id, memory_available):
    """Cross-check every finding file against the mem0 rows it claims to own."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import lab_mem

    # rglob, not glob: a finding filed into a subdirectory was previously invisible to
    # every check here, so its rows could go stale with nothing reporting it.
    findings = sorted(p for p in (root / "notes" / "findings").rglob("*.md")
                      if not p.name.startswith("_"))
    unindexed, drifted, dangling = [], [], []
    claimed = {}

    for f in findings:
        ids = lab_mem.read_mem0_rows(f)
        rel = f.relative_to(root).as_posix()
        if not ids:
            unindexed.append(rel)
            continue
        for rid in ids:
            claimed.setdefault(rid, []).append(f)

        # Drift is decided on the body hash, not on mtime: capture writes mem0_rows
        # into the frontmatter of the file it just indexed, so mtime is always newer
        # than the rows and every fresh capture would read as stale. Files predating
        # the hash fall back to the mtime comparison.
        recorded = lab_mem.read_frontmatter_value(f, "mem0_indexed_hash")
        if recorded:
            if lab_mem.content_hash(f) != recorded:
                drifted.append((rel, ids))
        elif memory_available:
            live = [rows_by_id[i] for i in ids if i in rows_by_id]
            stamps = [t for t in (lab_mem.parse_ts(r.get("updated_at") or r.get("created_at"))
                                  for r in live) if t]
            if stamps and _mtime(f) > max(stamps):
                drifted.append((rel, ids))

        if memory_available:
            missing = [i for i in ids if i not in rows_by_id]
            if missing:
                dangling.append((rel, missing))

    # The reverse direction. Scanning only files misses rows that no file claims -
    # left behind by a deleted finding, a failed supersede, or a capture that ran
    # without --finding-file. They keep answering recalls under a citation nobody owns.
    unclaimed, mismatched = [], []
    if memory_available:
        # Match on the basename too, not only on a literal "notes/findings/" prefix: a
        # source recorded with backslashes, an absolute path, or a bare filename would
        # otherwise slip past and the orphan would stay invisible.
        by_name = {p.name for p in findings}
        for row in rows_by_id.values():
            rid = row.get("id")
            if not rid:
                continue
            metadata = row.get("metadata") or {}
            src = (metadata.get("source") or "").replace("\\", "/")
            # The explicit link survives deletion even when source is a DOI or URL.
            # Legacy bare citations remain ambiguous; only full finding paths can
            # establish whether a claimed row still points at its owning file.
            pointer = metadata.get("finding_file")
            if not pointer and "notes/findings/" in src and "://" not in src:
                pointer = src
            if rid in claimed:
                if pointer:
                    target = Path(pointer.replace("\\", "/")).expanduser()
                    target = (root / target).resolve()
                    for f in claimed[rid]:
                        if f.resolve() != target:
                            mismatched.append((f.relative_to(root).as_posix(), rid, pointer))
                continue
            if pointer or "notes/findings/" in src or src.rsplit("/", 1)[-1] in by_name:
                unclaimed.append((rid, pointer or src))

    return {"findings": findings, "unindexed": unindexed, "drifted": drifted,
            "dangling": dangling, "unclaimed": unclaimed, "mismatched": mismatched}


def cmd_status(args) -> int:
    root = _find_root(Path(args.directory or ".").expanduser().resolve())
    if root is None:
        print("no .labkit.json found here or in any parent. Run: /labkit:lab-init")
        return 1
    cfg = json.loads((root / ".labkit.json").read_text(encoding="utf-8"))
    slug = cfg["slug"]

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import lab_mem

    rows, memory_available, memory_note = [], False, ""
    if not lab_mem.resolve_api_key():
        memory_note = "MEM0_API_KEY not resolvable - run lab_mem.py doctor"
    else:
        try:
            rows = lab_mem._rows(lab_mem.client().get_all(
                filters={"user_id": "proj-" + slug}))
            memory_available = True
        except Exception as exc:  # noqa: BLE001 - status must never hard-fail
            memory_note = "unavailable ({0})".format(type(exc).__name__)

    rows_by_id = {r.get("id"): r for r in rows}
    corpus = _corpus_state(root)
    notes = _notes_state(root, rows_by_id, memory_available)
    handoffs = sorted(p for p in (root / "handoffs").glob("*.md")
                      if p.name != "README.md" and not p.name.endswith(".prompt.md"))
    prompts = sorted((root / "handoffs").glob("*.prompt.md"))
    prompt_names = {p.name for p in prompts}
    missing_prompts = [p.name for p in handoffs if p.stem + ".prompt.md" not in prompt_names]
    log = root / "notes" / "log.md"
    log_lines = len(log.read_text(encoding="utf-8").splitlines()) if log.exists() else 0

    # One definition of "there is a problem", shared by both output modes. The JSON
    # branch previously checked a narrower set, so `status --json` could exit 0 on a
    # project that plain `status` had just exited 2 on.
    corpus_problem = (corpus.get("stale") or corpus.get("error")
                      or (not corpus["built"] and corpus["sources"] > 0))
    problem_list = (corpus_problem or notes["drifted"] or notes["dangling"] or notes["unindexed"]
                    or notes["unclaimed"] or notes["mismatched"] or missing_prompts
                    or not memory_available)

    if args.json:
        print(json.dumps({
            "project": cfg["name"], "root": str(root), "slug": slug,
            "corpus": corpus,
            "notes": {"findings": len(notes["findings"]), "log_lines": log_lines,
                      "unindexed": notes["unindexed"],
                      "drifted": [d[0] for d in notes["drifted"]],
                      "dangling": [d[0] for d in notes["dangling"]],
                      "unclaimed": [u[0] for u in notes["unclaimed"]],
                      "mismatched": [{"file": f, "id": rid, "finding_file": pointer}
                                     for f, rid, pointer in notes["mismatched"]]},
            "collab": {"records": len(handoffs), "prompts_saved": len(prompts),
                       "missing_prompts": missing_prompts},
            "memory": {"rows": len(rows), "available": memory_available, "note": memory_note},
            "ok": not problem_list,
        }, indent=2, ensure_ascii=False))
        return 2 if problem_list else 0

    print("project : {0}  ({1})".format(cfg["name"], root))
    print("corpus  : {0}".format(corpus["note"]))
    print("notes   : {0} finding(s), log.md {1} lines".format(len(notes["findings"]), log_lines))
    print("collab  : {0} record(s), {1} prompt(s) saved".format(len(handoffs), len(prompts)))
    print("memory  : {0}".format(
        "{0} row(s) in proj-{1}".format(len(rows), slug) if memory_available else memory_note))

    if corpus_problem:
        print("\n! corpus requires attention: {0}".format(corpus["note"]))
    if missing_prompts:
        print("\n! {0} handoff record(s) have no saved .prompt.md - the exchange is not "
              "reproducible".format(len(missing_prompts)))
        for name in missing_prompts:
            print("    {0}".format(name))
    if notes["unindexed"]:
        print("\n! never indexed - these will not surface in a future recall:")
        for f in notes["unindexed"]:
            print("    {0}".format(f))
        print("    fix: lab_mem.py capture --project {0} --finding-file <file> ...".format(slug))
    if notes["drifted"]:
        print("\n! STALE INDEX - file edited after its mem0 rows were written, so mem0 is")
        print("  still serving the superseded claim while citing the amended file:")
        for f, ids in notes["drifted"]:
            print("    {0}".format(f))
            for i in ids:
                print("        --supersede {0}".format(i))
        print("    fix: re-capture with one --supersede per id above")
    if notes["dangling"]:
        print("\n! dangling mem0_rows - ids recorded in the file no longer exist:")
        for f, ids in notes["dangling"]:
            print("    {0}: {1}".format(f, ", ".join(ids)))
    if notes["unclaimed"]:
        print("\n! orphan rows - they link to a finding file, but no file lists their id.")
        print("  They still answer recalls under a citation nobody owns:")
        for rid, src in notes["unclaimed"]:
            print("    {0}  cites {1}".format(rid, src))
        print("    fix: lab_mem.py forget --project {0} --id <id>".format(slug))
    if notes["mismatched"]:
        print("\n! mismatched finding links - listed rows point at a different finding file:")
        for f, rid, pointer in notes["mismatched"]:
            print("    {0}: {1} points at {2}".format(f, rid, pointer))
        print("    fix: restore the file path, or re-capture with the correct finding and source")
    if not memory_available:
        print("\n! the memory layer could not be reached, so nothing about mem0 was verified.")

    if not problem_list:
        print("\nall four layers in sync.")
    return 2 if problem_list else 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="lab_init.py", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    ini = sub.add_parser("init", help="scaffold a labkit research project")
    ini.add_argument("directory")
    ini.add_argument("--name")
    ini.add_argument("--force", action="store_true", help="overwrite existing scaffold files")
    ini.set_defaults(func=cmd_init)

    st = sub.add_parser("status", help="report what each layer holds and where they have drifted")
    st.add_argument("directory", nargs="?")
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=cmd_status)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
