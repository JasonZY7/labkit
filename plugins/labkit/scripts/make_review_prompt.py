#!/usr/bin/env python3
"""Build one self-contained review prompt for a model that cannot read files.

For context-only review or a host without filesystem access, the prompt must carry
its source evidence. This assembles the handover, the core
scripts and regression checks, and a review brief into a single file, keeping the answer
anchored to specific lines instead of general advice.

    make_review_prompt.py [--out review.md] [--focus "..."] [--verify]

--verify frames it as a follow-up pass on changes already made, rather than a first
look. Feed the result to codex-bridge with -PromptFile.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = (
    "scripts/lab_mem.py",
    "scripts/lab_init.py",
    "scripts/lab_models.py",
    "scripts/lab_run.py",
    "scripts/lab_run_legacy.py",
    "scripts/lab_native.py",
    "scripts/lab_handoff.py",
    "scripts/lab_history.py",
    "scripts/lab_fs.py",
    "scripts/lab_selftest.py",
    "scripts/test_lab_mem.py",
    "scripts/test_lab_status.py",
    "scripts/test_lab_models.py",
    "scripts/test_lab_run.py",
    "scripts/test_lab_team.py",
    "scripts/test_lab_native.py",
    "scripts/test_lab_native_controller.py",
    "scripts/test_lab_handoff.py",
    "scripts/test_lab_handoff_context.py",
    "scripts/test_lab_history.py",
    "scripts/test_lab_scaffold.py",
    "scripts/test_lab_fs.py",
)

BRIEF = """You are reviewing a shared Claude/Codex plugin called labkit. Be adversarial: I would rather hear
that something is broken than be told it looks fine. You have NO filesystem access in this setup -
everything you need is inlined below, so do not ask to read files or offer to open paths.

The handover document immediately below explains what the plugin is, the invariant it exists to
protect, and the mem0 behaviours that were established by experiment. Treat those measured
behaviours as facts, not as claims to re-derive.

## What I want

Find real defects. For each: what breaks, the concrete input or sequence that triggers it, and how
bad it is. Prioritise in this order:

0. Model selection, durable goal execution, native response attribution, interrupted
   work recovery, independent review coverage, cross-host handoff ownership and
   checkout/history/memory identity, and any path to false completion.

1. Ways the two-place invariant can silently break - a finding whose index disagrees with its file
   and where nothing reports it. This is the whole point of the plugin.
2. Data-loss paths. Anything that deletes a mem0 row without a live replacement, or corrupts a
   finding file.
3. Correctness bugs in the frontmatter parsing and hashing. It is hand-rolled, not a YAML library.
4. Concurrency and partial failure: two captures at once, a crash between steps, a network failure
   mid-sequence, a row that arrives late.
5. Whether the self-test actually pins the behaviour it claims. Would each check FAIL if the guard
   it targets were deleted, or is it vacuous?

Do not suggest rewriting this in another language, adding type hints, adding a YAML dependency for
its own sake, or general style improvements. Read the handover's current limitations before
reporting anything in it - those are trade-offs, and I want argument about the trade-off if you
disagree, not a rediscovery.

Rank by severity. If a category is clean, say so in one line rather than padding.
"""

VERIFY_BRIEF = """You reviewed this plugin before and found defects. Changes have been made in
response. This is a VERIFICATION pass, not a fresh review. Be adversarial: an incomplete fix reported
as complete is the worst outcome. You have NO filesystem access - the current source is inlined below.

For each defect you previously raised, one line: FIXED / PARTIAL / NOT FIXED, plus the specific code
that decides it. Then spend most of your words on:

A. Any NEW defect the changes introduced.
B. Any remaining way the two-place invariant can break with nothing reporting it.
C. Whether the self-test's checks would actually fail if the guard each one targets were removed.

Do not restate what is now correct at length. One line each is enough.
"""


def main():
    ap = argparse.ArgumentParser(prog="make_review_prompt.py",
                                 description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="review.md", help="where to write the prompt")
    ap.add_argument("--focus", help="an extra instruction appended to the brief")
    ap.add_argument("--verify", action="store_true",
                    help="frame it as a follow-up pass on changes already made")
    args = ap.parse_args()

    handover = ROOT / "HANDOVER.md"
    if not handover.exists():
        sys.exit("HANDOVER.md not found next to this script's parent directory: " + str(ROOT))

    parts = [VERIFY_BRIEF if args.verify else BRIEF]
    if args.focus:
        parts.append("## Focus for this pass\n\n" + args.focus.strip() + "\n")
    parts.append("# Handover\n\n" + handover.read_text(encoding="utf-8"))
    for rel in SOURCES:
        src = ROOT / rel
        if not src.exists():
            sys.exit("missing source file: " + str(src))
        parts.append("## Inlined file: {0}\n\n```python\n{1}\n```\n".format(
            rel, src.read_text(encoding="utf-8")))

    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    out.write_text("\n".join(parts), encoding="utf-8")
    print("wrote {0}  ({1:,} chars)".format(out, out.stat().st_size))
    print("\nfeed it to codex-bridge with:")
    print('  & "$HOME\\.claude\\local-marketplace\\codex-bridge\\skills\\codex-bridge\\scripts'
          '\\codex-bridge.ps1" -Mode ask -PromptFile "{0}" -Effort high -KeepArtifacts'.format(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
