#!/usr/bin/env python3
"""labkit end-to-end self-test.

Runs deterministic local guard regressions, then exercises the whole round trip
against LIVE mem0 in throwaway namespaces and deletes them. The live section
does not mock mem0: its asynchronous writes and filtering need real integration
coverage. The offline checks pin rare failure paths without relying on service timing.

The guard tests below each trace to a specific way the two-place invariant - finding
file as record, mem0 rows as index - was found to break. A test that only proves the
happy path works is not enough here: the failure mode that matters is an index that
disagrees with its file while everything reports success.

    lab_selftest.py [--keep] [--offline]

Costs a few dozen mem0 writes and a couple of minutes, most of it waiting out
asynchronous indexing and deletion. Exit 0 means every check passed.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import uuid
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from lab_fs import trash

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass

PASSED, FAILED = [], []


def check(label, condition, detail=""):
    (PASSED if condition else FAILED).append(label)
    print("  {0} {1}{2}".format("PASS" if condition else "FAIL", label,
                                ("  <- " + str(detail)[:200]) if detail and not condition else ""))
    return bool(condition)


def run(*args, expect=None):
    """Run one labkit script. A wrong exit code is a FAILURE, not a printed note."""
    proc = subprocess.run([sys.executable] + [str(a) for a in args],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (proc.stdout or "") + (proc.stderr or "")
    if expect is not None:
        check("exit {0} from {1}".format(expect, Path(args[0]).stem + " " + str(args[1])),
              proc.returncode == expect, "got {0}: {1}".format(proc.returncode, out[-200:]))
    return proc.returncode, out


def wait_for(predicate, tries=15, delay=2):
    """mem0 writes AND deletes are asynchronous. Poll instead of sleeping blind."""
    for _ in range(tries):
        if predicate():
            return True
        time.sleep(delay)
    return False


def offline_frontmatter_checks(lab_mem, tmp: Path):
    """Parser round-trips, run without touching the network."""
    print("\n0. frontmatter parser (offline)")

    four_space = tmp / "four-space.md"
    four_space.write_text(
        "---\nfinding: a claim\nmem0_rows:\n    - aaa\n    - bbb\nother: keep me\n---\n\nbody\n",
        encoding="utf-8")
    check("reads a 4-space block list", lab_mem.read_mem0_rows(four_space) == ["aaa", "bbb"],
          lab_mem.read_mem0_rows(four_space))

    lab_mem.write_mem0_rows(four_space, ["ccc"])
    text = four_space.read_text(encoding="utf-8")
    check("rewrite drops the old 4-space items", "aaa" not in text and "bbb" not in text, text)
    check("rewrite keeps unrelated frontmatter", "other: keep me" in text, text)
    check("rewrite keeps the body", text.rstrip().endswith("body"), text[-80:])

    commented = tmp / "commented.md"
    commented.write_text("---\nmem0_rows: [abc] # set by capture\nk: v\n---\n\nbody\n",
                         encoding="utf-8")
    check("strips an inline comment from an inline list",
          lab_mem.read_mem0_rows(commented) == ["abc"], lab_mem.read_mem0_rows(commented))

    # A blank line or comment inside a block list is legal YAML. Ending the list there
    # silently truncated the id set, and the dropped ids then survived a supersede that
    # looked complete - so this is a correctness case, not a formatting nicety.
    interrupted = tmp / "interrupted.md"
    interrupted.write_text(
        "---\nk: v\nmem0_rows:\n  - aaa\n\n  # a note\n  - bbb\nlast: z\n---\n\nbody\n",
        encoding="utf-8")
    check("a blank line and comment do not truncate the block list",
          lab_mem.read_mem0_rows(interrupted) == ["aaa", "bbb"],
          lab_mem.read_mem0_rows(interrupted))
    lab_mem.write_mem0_rows(interrupted, ["ccc"])
    txt = interrupted.read_text(encoding="utf-8")
    check("rewriting an interrupted list leaves no stray items",
          "aaa" not in txt and "bbb" not in txt and "last: z" in txt, txt)

    sentinel = tmp / "sentinel.md"
    sentinel.write_text("---\nk: v\nmem0_rows: []\n---\n\nbody\n", encoding="utf-8")
    lab_mem.write_mem0_rows(sentinel, ["z1"], stamp_hash=False)
    recorded = lab_mem.read_frontmatter_value(sentinel, "mem0_indexed_hash")
    check("an unstamped write records the sentinel, not a missing key",
          recorded == lab_mem.UNSTAMPED, recorded)
    check("the sentinel can never equal a real hash",
          lab_mem.content_hash(sentinel) != recorded)

    guarded = tmp / "guarded.md"
    guarded.write_text("---\nk: v\nmem0_rows: []\n---\n\nbody\n", encoding="utf-8")
    stamped = lab_mem.write_mem0_rows(guarded, ["g1"], expect_hash="not-the-real-hash")
    check("expect_hash mismatch refuses to stamp", stamped is False)
    check("expect_hash mismatch still records the ids",
          lab_mem.read_mem0_rows(guarded) == ["g1"], lab_mem.read_mem0_rows(guarded))

    hashed = tmp / "hashed.md"
    hashed.write_text("---\nfinding: v1\nmem0_rows: []\n---\n\nbody\n", encoding="utf-8")
    h_before = lab_mem.content_hash(hashed)
    lab_mem.write_mem0_rows(hashed, ["x1", "x2"])
    check("recording ids does not change the content hash",
          lab_mem.content_hash(hashed) == h_before)
    check("recorded hash matches the content hash",
          lab_mem.read_frontmatter_value(hashed, "mem0_indexed_hash") == h_before)
    hashed.write_text(hashed.read_text(encoding="utf-8").replace("finding: v1", "finding: v2"),
                      encoding="utf-8")
    check("editing frontmatter changes the content hash",
          lab_mem.content_hash(hashed) != h_before)


def main():
    ap = argparse.ArgumentParser(prog="lab_selftest.py", description=__doc__.splitlines()[0])
    ap.add_argument("--keep", action="store_true", help="leave the temp project on disk")
    ap.add_argument("--offline", action="store_true", help="run local regression checks only")
    args = ap.parse_args()

    suite = unittest.defaultTestLoader.discover(str(HERE), pattern="test_lab_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        return 1
    if args.offline:
        return 0

    import lab_mem

    if not lab_mem.resolve_api_key():
        sys.exit("MEM0_API_KEY not resolvable - cannot run the live round trip.")

    project = "labkit-selftest-" + uuid.uuid4().hex[:8]
    other = "labkit-selftest-other-" + uuid.uuid4().hex[:6]
    uid, other_uid = lab_mem.namespace(project), lab_mem.namespace(other)
    root = Path(tempfile.mkdtemp(prefix="labkit-selftest-", dir=Path(tempfile.gettempdir()).resolve()))
    mem, init = HERE / "lab_mem.py", HERE / "lab_init.py"
    client = lab_mem.client()

    def live(ns=None):
        return lab_mem._rows(client.get_all(filters={"user_id": ns or uid}))

    def live_ids(ns=None):
        return {r.get("id") for r in live(ns)}

    print("namespace : {0}\nproject   : {1}\n".format(uid, root))

    try:
        offline_frontmatter_checks(lab_mem, root)

        # ---------------------------------------------------------------- scaffold
        print("\n1. scaffold")
        run(init, "init", root / "proj", "--name", project, expect=0)
        proj = root / "proj"
        for rel in ("AGENTS.md", "CLAUDE.md", ".gitignore", ".labkit.json", "notes/log.md",
                    "raw/SOURCES.md", "notes/findings/_TEMPLATE.md", "handoffs/README.md"):
            check("scaffolds " + rel, (proj / rel).exists())
        gi = (proj / ".gitignore").read_text(encoding="utf-8")
        ignores_raw = any(line.strip() in ("raw/*", "raw/", "raw", "/raw", "/raw/*")
                          for line in gi.splitlines())
        check("does NOT gitignore raw/ in any form (would blind graphify)", not ignores_raw)
        cfg = json.loads((proj / ".labkit.json").read_text(encoding="utf-8"))
        check("records the mem0 namespace", cfg["mem0_namespace"] == uid, cfg.get("mem0_namespace"))

        template = (proj / "notes" / "findings" / "_TEMPLATE.md").read_text(encoding="utf-8")

        # ------------------------------------------------------------ capture+link
        print("\n2. capture, and link the rows back into the finding file")
        finding = proj / "notes" / "findings" / "2026-01-01-selftest.md"
        finding.write_text(template.replace(
            "<one-line claim, stated so it can be true or false>", "selftest claim v1"),
            encoding="utf-8")
        rc, out = run(mem, "capture", "--project", project,
                      "--text", "The labkit selftest fixture asserts that capture writes row ids "
                                "back into the finding file frontmatter.",
                      "--source", "notes/findings/2026-01-01-selftest.md",
                      "--kind", "finding", "--finding-file", finding, expect=0)
        check("capture reports a row id", "id        ->" in out, out[-300:])
        ids_v1 = lab_mem.read_mem0_rows(finding)
        check("mem0_rows written into frontmatter", bool(ids_v1), ids_v1)
        check("ids are live in the namespace", bool(ids_v1) and set(ids_v1) <= live_ids())

        # ------------------------------------------------------------ kind filter
        print("\n3. kind filtering, with a control")
        run(mem, "capture", "--project", project,
            "--text", "Selftest constraint: the corpus for this fixture is deliberately empty.",
            "--kind", "constraint", "--source", "notes/log.md", expect=0)
        q = "selftest fixture corpus frontmatter"
        rc, out = run(mem, "recall", "--project", project, q, "--kind", "constraint",
                      "--top-k", "10", "--json", expect=0)
        filtered = json.loads(out) if out.strip().startswith("[") else []
        rc, out = run(mem, "recall", "--project", project, q, "--top-k", "10", "--json", expect=0)
        unfiltered = json.loads(out) if out.strip().startswith("[") else []
        kinds_unfiltered = {(h.get("metadata") or {}).get("kind") for h in unfiltered}
        # Control: without the control, this test would pass even with filtering removed,
        # because the query might happen to rank only constraints.
        check("control: an unfiltered recall returns more than one kind",
              len(kinds_unfiltered) > 1, kinds_unfiltered)
        check("recall --kind returns only that kind",
              bool(filtered) and all((h.get("metadata") or {}).get("kind") == "constraint"
                                     for h in filtered),
              [(h.get("metadata") or {}).get("kind") for h in filtered])

        # ------------------------------------------------------------------ status
        print("\n4. status: in sync")
        rc, out = run(init, "status", proj, expect=0)
        check("status says all layers in sync", "all four layers in sync" in out, out[-300:])

        # ------------------------------------------------------------ drift detection
        print("\n5. status: detects a finding amended after indexing")
        for old, new in (("selftest claim v1", "selftest claim v2, amended"),
                         ("<exact quote, verbatim, with no paraphrase>", "amended evidence")):
            finding.write_text(finding.read_text(encoding="utf-8").replace(old, new),
                               encoding="utf-8")
            rc, _ = run(init, "status", proj)
            check("drift detected after editing {0!r}".format(old[:24]), rc == 2, rc)
            finding.write_text(finding.read_text(encoding="utf-8").replace(new, old),
                               encoding="utf-8")
            rc, _ = run(init, "status", proj)
            check("restoring the edit clears the drift", rc == 0, rc)
        finding.write_text(finding.read_text(encoding="utf-8")
                           .replace("selftest claim v1", "selftest claim v2, amended"),
                           encoding="utf-8")
        rc, out = run(init, "status", proj, expect=2)
        check("status names the STALE INDEX", "STALE INDEX" in out, out[-400:])
        check("status prints a --supersede line per stale id",
              all("--supersede {0}".format(i) in out for i in ids_v1), out[-400:])

        # -------------------------------------------- the laundering guard (astra #1)
        print("\n6. capture refuses to launder an amended finding")
        rc, out = run(mem, "capture", "--project", project,
                      "--text", "This capture must be refused: the finding was amended and no "
                                "--supersede was supplied.",
                      "--source", "notes/findings/2026-01-01-selftest.md",
                      "--kind", "finding", "--finding-file", finding)
        check("re-capture without --supersede is refused", rc != 0, rc)
        check("refusal names the ids to supersede",
              all("--supersede {0}".format(i) in out for i in ids_v1), out[-400:])
        check("refusal did not stamp a fresh hash - status still reports drift",
              run(init, "status", proj)[0] == 2)

        # --allow-orphans is the documented escape hatch AND the way to guarantee the
        # file lists two ids, so the partial-supersede case below does not depend on
        # whether mem0 happened to split a sentence this run.
        rc, out = run(mem, "capture", "--project", project, "--text",
                      "The allow-orphans escape hatch deliberately keeps the previous rows "
                      "of this finding in place while adding a second one.",
                      "--finding-file", finding, "--allow-orphans", expect=0)
        multi = lab_mem.read_mem0_rows(finding)
        check("--allow-orphans keeps the old ids and adds the new",
              set(ids_v1) <= set(multi) and len(multi) > len(ids_v1), multi)
        check("--allow-orphans does not hide the stale index",
              run(init, "status", proj)[0] == 2)

        # Superseding only SOME of the listed ids is the same laundering hole one row
        # narrower: the uncovered row survives and the file gets a fresh hash anyway.
        print("\n6b. partial --supersede is refused as well")
        finding.write_text(finding.read_text(encoding="utf-8")
                           .replace("selftest claim v2, amended", "selftest claim v3"),
                           encoding="utf-8")
        check("the file is amended again", run(init, "status", proj)[0] == 2)
        rc, out = run(mem, "capture", "--project", project, "--text",
                      "Partial supersede must be refused just as a bare re-capture is.",
                      "--finding-file", finding, "--supersede", multi[0])
        check("partial --supersede is refused", rc != 0, rc)
        check("refusal lists the uncovered ids only",
              all(i in out for i in multi[1:]) and "--supersede {0}".format(multi[0]) not in out,
              out[-400:])

        # ------------------------------------------- ownership guard (astra #7)
        # Measured behaviour worth pinning: mem0's extraction silently stores nothing
        # when the text carries no concrete fact. capture must report that rather than
        # claim success, and must not supersede anything on the way out.
        # Tolerant on purpose. Whether mem0's extraction keeps a vague sentence is not
        # deterministic - the same input produced 0 rows on one run and 1 on the next -
        # so asserting the empty case would make this suite flaky. What must hold either
        # way: a zero-row outcome is reported as such and never as success.
        print("\n7. a zero-extraction capture is reported, not silently treated as success")
        rc, out = run(mem, "capture", "--project", project,
                      "--text", "A fact belonging to a different selftest project entirely.",
                      "--kind", "note")
        if rc == 0:
            check("stored: reported a row id", "id        ->" in out, out[-200:])
        else:
            check("not stored: message names extraction, not a timeout",
                  "extraction" in out, out[-300:])

        print("\n7b. deletes are refused across project boundaries")
        run(mem, "capture", "--project", other,
            "--text", "The other selftest project pins mem0ai to version 2.0.20 and runs on "
                      "Python 3.12 under Windows.",
            "--kind", "note", expect=0)
        foreign = sorted(live_ids(other_uid))
        check("fixture row exists in the other namespace", bool(foreign))
        if foreign:
            rc, out = run(mem, "forget", "--project", project, "--id", foreign[0])
            check("forget --id refuses a foreign row", rc != 0 and "does not belong" in out,
                  out[-200:])
            rc, out = run(mem, "capture", "--project", project, "--text", "x",
                          "--supersede", foreign[0])
            check("--supersede refuses a foreign row", rc != 0 and "does not belong" in out,
                  out[-200:])
            check("the foreign row survived", foreign[0] in live_ids(other_uid))

        # ------------------------------------------------------------- supersede
        print("\n8. re-capture superseding every listed id clears the drift")
        stale = lab_mem.read_mem0_rows(finding)  # includes whatever --allow-orphans left
        supersede = []
        for i in stale:
            supersede += ["--supersede", i]
        rc, out = run(mem, "capture", "--project", project,
                      "--text", "Amended selftest claim v2: capture must also be able to retire "
                                "the rows a previous version of this finding produced.",
                      "--source", "notes/findings/2026-01-01-selftest.md",
                      "--kind", "finding", "--finding-file", finding, *supersede, expect=0)
        check("supersede reports each id as deleted",
              all("supersede -> {0} deleted".format(i) in out for i in stale), out[-500:])
        ids_v2 = lab_mem.read_mem0_rows(finding)
        check("frontmatter no longer lists the retired ids",
              bool(ids_v2) and not set(ids_v2) & set(stale), "stale={0} v2={1}".format(stale, ids_v2))
        check("retired rows are gone from mem0", wait_for(lambda: not set(stale) & live_ids()))
        check("the hash is stamped once every supersede is confirmed",
              lab_mem.read_frontmatter_value(finding, "mem0_indexed_hash") != lab_mem.UNSTAMPED)
        run(init, "status", proj, expect=0)

        # ---------------------------------------------------- unindexed + json parity
        print("\n9. status: unindexed findings, and json/text exit parity")
        orphan_file = proj / "notes" / "findings" / "2026-01-02-orphan.md"
        orphan_file.write_text(template, encoding="utf-8")
        rc_text, out = run(init, "status", proj, expect=2)
        check("status names the unindexed file", "2026-01-02-orphan.md" in out, out[-300:])
        rc_json, out_json = run(init, "status", proj, "--json")
        check("--json exits the same as text output", rc_json == rc_text,
              "json {0} vs text {1}".format(rc_json, rc_text))
        check("--json reports ok:false", '"ok": false' in out_json, out_json[-200:])
        trash(orphan_file, within=proj / "notes" / "findings")

        # ------------------------------------------------ nested findings (astra #8)
        print("\n10. status sees findings in subdirectories")
        nested = proj / "notes" / "findings" / "sub" / "2026-01-03-nested.md"
        nested.parent.mkdir(parents=True, exist_ok=True)
        nested.write_text(template, encoding="utf-8")
        rc, out = run(init, "status", proj, expect=2)
        check("a nested finding is not invisible", "2026-01-03-nested.md" in out, out[-300:])
        trash(nested.parent, within=proj / "notes" / "findings")

        # ------------------------------------------------- orphan rows (astra #8)
        print("\n11. status reports rows no finding file claims")
        rc, out = run(mem, "capture", "--project", project,
                      "--text", "An orphan row: captured with a findings citation but no "
                                "--finding-file, so nothing on disk lists its id.",
                      "--source", "notes/findings/2026-01-01-selftest.md",
                      "--kind", "finding", expect=0)
        rc, out = run(init, "status", proj, expect=2)
        check("orphan row reported", "orphan rows" in out, out[-500:])

        # ------------------------------------------------------------ forget --id
        print("\n12. forget --id")
        target = ids_v2[0]
        run(mem, "forget", "--project", project, "--id", target, expect=0)
        check("row actually disappears", wait_for(lambda: target not in live_ids()))
        rc, out = run(init, "status", proj, expect=2)
        check("the now-dangling id is reported", "dangling" in out, out[-400:])

        print("\n13. forget --all refuses without --yes")
        rc, out = run(mem, "forget", "--project", project, "--all")
        check("forget --all without --yes is refused", rc != 0 and "--yes" in out, out[-200:])

    finally:
        print("\n14. teardown")
        for ns, ns_uid in ((project, uid), (other, other_uid)):
            run(mem, "forget", "--project", ns, "--all", "--yes")
        check("namespaces emptied", wait_for(lambda: not live() and not live(other_uid)))
        if args.keep:
            print("    kept: {0}".format(root))
        else:
            trash(root, within=Path(tempfile.gettempdir()).resolve())
            print("    recycled: {0}".format(root))

    print("\n{0} passed, {1} failed".format(len(PASSED), len(FAILED)))
    for f in FAILED:
        print("  FAILED: {0}".format(f))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
