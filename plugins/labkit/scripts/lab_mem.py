#!/usr/bin/env python3
"""labkit memory layer - a thin, opinionated wrapper over mem0 cloud.

One project maps to one mem0 namespace (user_id "proj-<slug>"). Every row carries
metadata saying where the fact came from, so a recall can cite a source instead of
just asserting one.

Why a wrapper at all, rather than calling mem0 directly. Several of mem0's argument
shapes fail silently, and this file is the single place each one is pinned down:

  * The entity id goes flat on add() and delete_all() (user_id="...") but nested on
    search() and get_all() (filters={"user_id": "..."}). delete_all raises a 400 on
    the wrong shape; search swallows the stray kwarg and returns the whole namespace
    as if it had matched.
  * Metadata filtering only works inside the filters AND-form:
    filters={"AND": [{"user_id": u}, {"metadata": {"kind": "constraint"}}]}.
    Passing metadata={"kind": ...} as its own kwarg is accepted and ignored, so an
    unfiltered result comes back looking filtered.
  * add() is asynchronous, returns {"status": "PENDING"}, and splits one sentence
    into several rows. The rows do not all become visible at once, so "did my write
    land" needs a stabilised id diff, not a first-hit poll.
  * delete() and delete_all() are asynchronous too. A 2xx means accepted, not gone.
  * The extraction pass returns real Unicode punctuation, which a cp1252 Windows
    console cannot print. That raises AFTER the row is already written, which looks
    like a failed capture and is not.

The invariant this file exists to hold: a finding file and the mem0 rows indexing it
must never disagree without something reporting it. Every guard below traces to a way
that invariant was found to break under review.

Usage:
    lab_mem.py capture  --project <slug> --text "..." [--source S] [--page N]
                        [--kind decision|constraint|finding|preference|note]
                        [--agent claude|codex|human] [--tag T]...
                        [--supersede ID]... [--finding-file PATH]
                        [--allow-orphans] [--no-wait]
    lab_mem.py recall   --project <slug> "question" [--kind K] [--top-k N] [--json]
    lab_mem.py list     --project <slug> [--kind K] [--json]
    lab_mem.py forget   --project <slug> --id <memory_id>
    lab_mem.py forget   --project <slug> --all --yes
    lab_mem.py doctor
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path

# Written into mem0_indexed_hash when the rows are known NOT to describe the file as it
# now stands. It can never equal a real digest, so status reports drift deterministically
# instead of falling back to an mtime comparison that a later row update could mask.
UNSTAMPED = "unstamped-rows-describe-a-previous-version"

KINDS = ("decision", "constraint", "finding", "preference", "note")

# add() splits a sentence into rows that do not all appear at once. Accept the set
# only after it has held steady across two consecutive polls.
POLL_INTERVAL = 2.0
POLL_ROUNDS = 12
DELETE_CONFIRM_ROUNDS = 10

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover - non-reconfigurable stream
        pass


# --------------------------------------------------------------------------- keys


def resolve_api_key():
    key = os.getenv("MEM0_API_KEY")
    if key:
        return key.strip()
    if sys.platform != "win32":
        return None
    # setx and the Environment Variables dialog write the registry, but a process
    # that was already running inherited the old environment block. Read it directly.
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as hkey:
            value, _ = winreg.QueryValueEx(hkey, "MEM0_API_KEY")
            return str(value).strip() or None
    except (OSError, FileNotFoundError):
        return None


def namespace(project):
    slug = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in project.strip().lower())
    slug = slug.strip("-") or "default"
    return "proj-" + slug


def client():
    key = resolve_api_key()
    if not key:
        sys.exit(
            "MEM0_API_KEY not found in the environment or in HKCU\\Environment.\n"
            "Set it in the Windows Environment Variables dialog "
            "(Win+R -> rundll32 sysdm.cpl,EditEnvironmentVariables), then retry."
        )
    try:
        from mem0 import MemoryClient
    except ImportError:
        sys.exit("mem0ai is not installed. Run: pip install mem0ai")
    return MemoryClient(api_key=key)


# ----------------------------------------------------------------------- helpers


def _rows(response):
    if isinstance(response, dict):
        return response.get("results", [])
    return response or []


def _entity_filter(uid, kind=None):
    """Build the only filter shape mem0 actually honours for metadata."""
    if not kind:
        return {"user_id": uid}
    return {"AND": [{"user_id": uid}, {"metadata": {"kind": kind}}]}


def parse_ts(value):
    """mem0 timestamps are ISO 8601 with an offset. Return an aware datetime or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _retry(fn, what, attempts=3, delay=2.0):
    """Retry a mutating mem0 call through transient transport failures.

    A dropped connection mid-run used to surface as a raw httpx traceback, which is
    both unreadable and, on the supersede path, ambiguous: the caller cannot tell
    whether the row was deleted. Retrying and then reporting cleanly makes the outcome
    something callers can act on. Raises the last exception if every attempt fails.
    """
    last = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - transport layer raises many types
            last = exc
            if attempt + 1 < attempts:
                print("   retrying {0} after {1}: {2}".format(
                    what, type(exc).__name__, str(exc).splitlines()[0][:120]))
                time.sleep(delay)
    raise last


def _matches_meta(row, wanted):
    """True when every key/value we sent is present on the row.

    Used to tell OUR new rows apart from rows another capture happened to land in the
    same window. An id diff alone cannot do that: a concurrent or late-arriving write
    would be adopted as this call's result, and its id written into the wrong finding.
    """
    meta = row.get("metadata") or {}
    return all(meta.get(k) == v for k, v in wanted.items())


def _atomic_write(path: Path, text: str):
    """Write via a temp file in the same directory, then replace.

    Path.write_text truncates the target first, so a crash or a full disk mid-write
    leaves an empty or half-written file. The finding file is the layer of record;
    it must never be the thing that gets corrupted. mkstemp creates the temp file with
    0600, so the original mode is copied over before the replace.
    """
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if path.exists():
            shutil.copymode(str(path), tmp)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ------------------------------------------------------- finding-file frontmatter

_FM = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.S)
BOOKKEEPING_KEYS = ("mem0_rows", "mem0_indexed_hash")

# A YAML block-list item at any indentation. The earlier version only matched zero or
# two spaces, so a four-space list was read as empty and then orphaned on write-back.
_LIST_ITEM = re.compile(r"^\s*-\s+(.*)$")


def _scalar(raw):
    """Strip an inline comment and surrounding quotes from a YAML scalar."""
    raw = raw.strip()
    if raw.startswith(("'", '"')):
        quote = raw[0]
        end = raw.find(quote, 1)
        if end != -1:
            return raw[1:end]
    return raw.split("#", 1)[0].strip()


def _split_frontmatter(text):
    m = _FM.match(text)
    if not m:
        return None, text
    return m.group(1), text[m.end():]


def _strip_bookkeeping(fm_lines):
    """Drop mem0_rows (and its list items) and mem0_indexed_hash from frontmatter lines."""
    kept, in_rows = [], False
    for line in fm_lines:
        if re.match(r"^mem0_rows\s*:", line):
            in_rows = not _scalar(line.split(":", 1)[1])  # inline form has no block below
            continue
        if re.match(r"^mem0_indexed_hash\s*:", line):
            in_rows = False
            continue
        if in_rows:
            if _LIST_ITEM.match(line) or not line.strip() or line.lstrip().startswith("#"):
                continue
            in_rows = False
        kept.append(line)
    return kept


def content_hash(path):
    """SHA-256 of the file minus its own bookkeeping frontmatter.

    Drift cannot be judged on mtime: capture writes mem0_rows into the frontmatter of
    the very file it just indexed, so mtime is always newer than the rows and every
    fresh capture would read as stale. Hashing instead, and excluding only the two
    keys capture itself writes, makes the check exact - a changed `finding:` line or a
    changed evidence block both count, our own id-write does not.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    fm, body = _split_frontmatter(text)
    if fm is None:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    payload = "\n".join(_strip_bookkeeping(fm.splitlines())) + "\n---\n" + body
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def read_frontmatter_value(path, key):
    """Read one scalar key out of a finding file's YAML frontmatter."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    fm, _ = _split_frontmatter(text)
    if fm is None:
        return None
    for line in fm.splitlines():
        if re.match(r"^{0}\s*:".format(re.escape(key)), line):
            return _scalar(line.split(":", 1)[1]) or None
    return None


def read_mem0_rows(path):
    """Read the mem0_rows: list out of a finding file's YAML frontmatter."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    fm, _ = _split_frontmatter(text)
    if fm is None:
        return []
    ids, in_block = [], False
    for line in fm.splitlines():
        if re.match(r"^mem0_rows\s*:", line):
            inline = _scalar(line.split(":", 1)[1])
            if inline and inline != "[]":
                ids += [x.strip().strip("'\"") for x in inline.strip("[]").split(",") if x.strip()]
                in_block = False
            else:
                in_block = not inline
            continue
        if in_block:
            # A blank line or a full-line comment inside a block list is legal YAML and
            # must not end the list - doing so silently truncated the id set, and the
            # dropped ids then survived a supersede that looked complete.
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            m = _LIST_ITEM.match(line)
            if m:
                ids.append(_scalar(m.group(1)).strip("'\""))
                continue
            in_block = False
    return [i for i in ids if i]


def write_mem0_rows(path, ids, stamp_hash=True, expect_hash=None):
    """Rewrite mem0_rows and mem0_indexed_hash in the frontmatter.

    stamp_hash=False writes the UNSTAMPED sentinel rather than omitting the key. Omitting
    it would drop status back to an mtime comparison, and a later row update could then
    make a known-unsynced finding look fine again. The sentinel can never match a digest,
    so the drift stays reported until someone re-captures.

    expect_hash closes the check-then-write race: the caller compared the file before the
    asynchronous write, but the file is only read HERE. If it no longer matches, the rows
    describe a version that is already gone, so record them without a stamp instead of
    blessing content nobody indexed.
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    fm, body = _split_frontmatter(text)
    if fm is None:
        raise SystemExit("{0} has no YAML frontmatter; cannot record mem0_rows".format(path))

    kept = _strip_bookkeeping(fm.splitlines())
    payload = "\n".join(kept) + "\n---\n" + body
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    if expect_hash is not None and digest != expect_hash:
        stamp_hash = False

    block = (["mem0_rows:"] + ["  - " + i for i in ids]) if ids else ["mem0_rows: []"]
    block.append("mem0_indexed_hash: " + (digest if stamp_hash else UNSTAMPED))
    _atomic_write(p, "---\n" + "\n".join(kept + block) + "\n---\n" + body)
    return stamp_hash


# ---------------------------------------------------------------------- commands


def _await_new_rows(c, uid, before, wanted_meta):
    """Wait for this add()'s rows, and only this add()'s rows.

    Two guards, both from observed behaviour: the set must hold steady across two
    polls (a split write reveals its rows one at a time, and stopping at the first
    would silently orphan the rest), and every row must carry the metadata we sent
    (an id diff alone would adopt a concurrent capture's row).
    """
    previous = None
    for _ in range(POLL_ROUNDS):
        time.sleep(POLL_INTERVAL)
        fresh = [r for r in _rows(c.get_all(filters={"user_id": uid}))
                 if r.get("id") not in before and _matches_meta(r, wanted_meta)]
        ids = sorted(r.get("id") for r in fresh)
        if ids and ids == previous:
            return fresh
        previous = ids
    return []


def _confirm_deleted(c, uid, ids):
    """Return the subset genuinely gone. A 2xx from delete() only means accepted."""
    remaining = set(ids)
    for _ in range(DELETE_CONFIRM_ROUNDS):
        live = {r.get("id") for r in _rows(c.get_all(filters={"user_id": uid}))}
        remaining &= live
        if not remaining:
            return set(ids)
        time.sleep(POLL_INTERVAL)
    return set(ids) - remaining


def cmd_capture(args):
    finding = Path(args.finding_file) if args.finding_file else None
    if finding:
        try:
            fm, _ = _split_frontmatter(finding.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            sys.exit("cannot read finding file {0} ({1}); nothing was indexed or deleted".format(
                finding, type(exc).__name__))
        if fm is None:
            sys.exit("{0} has no YAML frontmatter; nothing was indexed or deleted".format(finding))

    c = client()
    uid = namespace(args.project)

    # A per-call token in the metadata. Without it, two captures carrying the same
    # project/kind/agent are indistinguishable, and a concurrent or late-arriving row
    # could be adopted as this call's result and written into the wrong finding.
    capture_id = uuid.uuid4().hex
    meta = {"project": args.project, "kind": args.kind, "agent": args.agent,
            "capture_id": capture_id}
    if args.source:
        meta["source"] = args.source
    elif finding:
        meta["source"] = finding.as_posix()
    if finding:
        meta["finding_file"] = finding.resolve().as_posix()
    if args.page is not None:
        meta["page"] = args.page
    if args.tag:
        meta["tags"] = args.tag

    namespace_ids = {r.get("id") for r in _rows(c.get_all(filters={"user_id": uid}))}

    # Snapshot the file BEFORE the write. Everything downstream compares against this,
    # not against whatever the file says once the asynchronous add has finished.
    pre_hash = content_hash(finding) if finding else None
    prior_ids = read_mem0_rows(finding) if finding else []
    requested = list(args.supersede or [])

    # A memory id is deletable with these credentials whether or not it belongs to this
    # project, so a mistyped id would silently destroy another project's fact. An id the
    # finding file already lists may be retired locally when absent from this namespace;
    # that is NOT permission to delete it remotely, where it may belong to another project.
    for old in requested:
        if old not in namespace_ids and old not in prior_ids:
            sys.exit("--supersede {0}: not a row in {1} and not listed in the finding file. "
                     "Refusing to delete a row that does not belong to this project.".format(
                         old, uid))

    # The laundering case: the finding was amended, its old rows still assert the old
    # claim, and re-capturing would keep them AND stamp a fresh hash - leaving status
    # reporting "in sync" over an index that contradicts the file. Superseding SOME of
    # the old ids is the same hole, one row narrower, so the check demands all of them.
    recorded = read_frontmatter_value(finding, "mem0_indexed_hash") if finding else None
    # Legacy rows without a hash have no verified content version. They must be
    # superseded too, rather than silently blessing them on the first hashed capture.
    amended = bool(prior_ids) and recorded != pre_hash
    if finding and prior_ids and not args.allow_orphans:
        uncovered = [i for i in prior_ids if i not in requested]
        if amended and uncovered:
            sys.exit(
                "{0} has changed since it was indexed or has no verified hash, and these rows it still lists were "
                "not superseded:\n".format(finding)
                + "".join("    --supersede {0}\n".format(i) for i in uncovered)
                + "They are not verified against this version. Capturing now would keep them and "
                  "stamp a fresh hash, so status would report this finding as in sync while "
                  "mem0 contradicts it.\nPass every line above, or --allow-orphans if the old "
                  "rows are deliberately kept."
            )

    res = c.add([{"role": "user", "content": args.text}], user_id=uid, metadata=meta)
    print("queued    -> {0}  event={1} status={2}".format(
        uid, res.get("event_id", "?"), res.get("status", "?")))

    if args.no_wait:
        print("not waiting for indexing (--no-wait); the row is searchable in ~10s")
        if args.supersede or finding:
            print("WARNING: --supersede and --finding-file are skipped under --no-wait")
        return 0

    fresh = _await_new_rows(c, uid, namespace_ids, meta)
    if not fresh:
        # Measured: mem0's extraction pass can decide a sentence carries no durable
        # fact and store nothing at all, with no error - "A fact belonging to another
        # project entirely." produced zero rows. That is far more common than a slow
        # index, so name it first. Nothing is superseded and no file is touched.
        print("no row was created within {0}s.".format(int(POLL_ROUNDS * POLL_INTERVAL)))
        print("mem0's extraction most likely found no durable fact in --text. Vague or "
              "self-referential sentences are dropped silently; state a concrete fact with "
              "its subject named. Nothing was superseded and no file was updated.")
        return 1

    new_ids = []
    for r in fresh:
        print("indexed   -> {0!r}".format(r.get("memory")))
        print("id        -> {0}".format(r.get("id")))
        new_ids.append(r.get("id"))
    if len(fresh) > 1:
        print("note      -> mem0 split this capture into {0} rows".format(len(fresh)))

    # Supersede only after the replacement is confirmed live, and de-register only the
    # ids confirmed gone. A row whose delete failed must stay listed, or it becomes an
    # orphan that no later status run can see.
    deleted = set()
    if requested:
        for old in requested:
            if old not in namespace_ids:
                continue  # A dangling local reference never authorizes a remote delete.
            try:
                _retry(lambda o=old: c.delete(memory_id=o), "delete " + old)
            except Exception as exc:  # noqa: BLE001 - report and keep going
                print("supersede -> {0} request FAILED ({1}: {2})".format(
                    old, type(exc).__name__, str(exc).splitlines()[0][:120]))
        # Ask the namespace, never the response. A retry that ends in an exception may
        # still have deleted the row, and a 2xx only means the request was accepted.
        deleted = _confirm_deleted(c, uid, requested)
        for old in requested:
            print("supersede -> {0} {1}".format(
                old, ("deleted" if old in namespace_ids else "absent from namespace; reference retired")
                if old in deleted else "STILL PRESENT - left in mem0_rows"))

    if finding:
        survived = [i for i in prior_ids if i not in deleted]
        # Any old row still standing means the index disagrees with the file, so the
        # hash must not bless this version - status has to keep reporting it.
        unconfirmed = [i for i in requested if i not in deleted]
        stale_survivors = amended and bool(survived)
        stamped = write_mem0_rows(finding, survived + new_ids,
                                  stamp_hash=not (unconfirmed or stale_survivors), expect_hash=pre_hash)
        if stamped:
            print("recorded  -> mem0_rows in {0}".format(finding))
        elif unconfirmed:
            print("recorded  -> mem0_rows in {0}, hash NOT stamped: {1} row(s) could not be "
                  "confirmed deleted and still assert the old claim. status will report drift "
                  "until they are gone.".format(finding, len(unconfirmed)))
        elif stale_survivors:
            print("recorded  -> mem0_rows in {0}, hash NOT stamped: old rows were deliberately "
                  "kept and are not verified against this version. status will report drift "
                  "until you re-capture with every listed id superseded.".format(finding))
        else:
            print("recorded  -> mem0_rows in {0}, hash NOT stamped: the file changed while the "
                  "capture was in flight, so these rows describe the previous version. status "
                  "will report drift until you re-capture.".format(finding))
    return 0


def cmd_recall(args):
    c = client()
    uid = namespace(args.project)
    hits = _rows(c.search(args.question,
                          filters=_entity_filter(uid, args.kind),
                          top_k=args.top_k))

    if args.json:
        print(json.dumps(hits, ensure_ascii=False, indent=2))
        return 0

    scope = uid + (" [kind={0}]".format(args.kind) if args.kind else "")
    if not hits:
        print("no memories in {0} matched that question.".format(scope))
        return 0

    print("{0} hit(s) in {1}:\n".format(len(hits), scope))
    for h in hits:
        meta = h.get("metadata") or {}
        cite = meta.get("source") or "no source recorded"
        if meta.get("page") is not None:
            cite += ", p.{0}".format(meta["page"])
        score = h.get("score")
        score_s = "{0:.3f}".format(score) if isinstance(score, (int, float)) else "?"
        weak = "  <- weak match" if isinstance(score, (int, float)) and score < 0.15 else ""
        print("  [{0}] {1}".format(meta.get("kind", "note"), h.get("memory")))
        print("      source: {0}   by: {1}   score: {2}{3}".format(
            cite, meta.get("agent", "?"), score_s, weak))
        print("      id: {0}\n".format(h.get("id")))
    return 0


def cmd_list(args):
    c = client()
    uid = namespace(args.project)
    rows = _rows(c.get_all(filters=_entity_filter(uid, args.kind)))
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    scope = uid + (" [kind={0}]".format(args.kind) if args.kind else "")
    print("{0} memory row(s) in {1}:\n".format(len(rows), scope))
    for r in rows:
        meta = r.get("metadata") or {}
        print("  [{0}] {1}".format(meta.get("kind", "note"), r.get("memory")))
        print("      source: {0}   id: {1}".format(meta.get("source", "-"), r.get("id")))
        print("      updated: {0}\n".format(r.get("updated_at") or r.get("created_at") or "-"))
    return 0


def cmd_forget(args):
    c = client()
    uid = namespace(args.project)
    if args.all:
        if not args.yes:
            sys.exit("refusing to wipe a namespace without --yes")
        # delete_all forwards kwargs straight into query params, so the entity id
        # goes flat here - a filters={...} dict is rejected with a 400.
        print(c.delete_all(user_id=uid))
        return 0
    if not args.id:
        sys.exit("pass --id <memory_id>, or --all --yes to wipe the namespace")
    try:
        live = {r.get("id") for r in
                _rows(_retry(lambda: c.get_all(filters={"user_id": uid}), "namespace read"))}
    except Exception as exc:  # noqa: BLE001
        sys.exit("could not read {0} to verify ownership, so nothing was deleted "
                 "({1}: {2})".format(uid, type(exc).__name__, str(exc).splitlines()[0][:150]))
    if args.id not in live:
        sys.exit("{0} is not a row in {1}. Refusing to delete a row that does not belong "
                 "to this project.".format(args.id, uid))
    try:
        print(_retry(lambda: c.delete(memory_id=args.id), "delete " + args.id))
    except Exception as exc:  # noqa: BLE001
        # A failed retry does not mean nothing happened: the delete may have landed and
        # only the response was lost. Ask the namespace before reporting an outcome.
        gone = _confirm_deleted(c, uid, [args.id])
        state = ("the row is gone despite the error" if args.id in gone
                 else "the row is still present")
        sys.exit("delete reported an error after retries ({0}: {1}); {2}.".format(
            type(exc).__name__, str(exc).splitlines()[0][:150], state))
    return 0


def cmd_doctor(_args):
    key = resolve_api_key()
    src = "env" if os.getenv("MEM0_API_KEY") else ("registry" if key else "missing")
    line = "MEM0_API_KEY : " + src
    if key:
        line += "  ({0}...{1}, len {2})".format(key[:5], key[-3:], len(key))
    print(line)
    try:
        import mem0

        print("mem0ai       : {0}".format(getattr(mem0, "__version__", "installed")))
    except ImportError:
        print("mem0ai       : NOT INSTALLED  (pip install mem0ai)")
        return 1
    if not key:
        return 1
    try:
        client().users()
        print("auth         : OK")
    except Exception as exc:  # noqa: BLE001 - report whatever the API said
        print("auth         : FAILED  {0}: {1}".format(type(exc).__name__, str(exc)[:200]))
        return 1
    return 0


def main():
    ap = argparse.ArgumentParser(prog="lab_mem.py", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    cap = sub.add_parser("capture", help="write one durable fact into project memory")
    cap.add_argument("--project", required=True)
    cap.add_argument("--text", required=True)
    cap.add_argument("--source", help="citation pointer, e.g. notes/findings/x.md")
    cap.add_argument("--page", type=int)
    cap.add_argument("--kind", choices=KINDS, default="finding")
    cap.add_argument("--agent", default="claude", help="who established this: claude, codex, human")
    cap.add_argument("--tag", action="append", help="repeatable")
    cap.add_argument("--supersede", action="append", metavar="ID",
                     help="retire this row once the replacement is confirmed live; repeatable")
    cap.add_argument("--finding-file", metavar="PATH",
                     help="record the produced row ids in this file's mem0_rows frontmatter")
    cap.add_argument("--allow-orphans", action="store_true",
                     help="capture against an amended finding without superseding its old rows")
    cap.add_argument("--no-wait", action="store_true")
    cap.set_defaults(func=cmd_capture)

    rec = sub.add_parser("recall", help="semantic search over project memory")
    rec.add_argument("--project", required=True)
    rec.add_argument("question")
    rec.add_argument("--kind", choices=KINDS,
                     help="restrict to one kind; far more reliable than raw semantic score")
    rec.add_argument("--top-k", type=int, default=5)
    rec.add_argument("--json", action="store_true")
    rec.set_defaults(func=cmd_recall)

    lst = sub.add_parser("list", help="dump rows in the project namespace")
    lst.add_argument("--project", required=True)
    lst.add_argument("--kind", choices=KINDS)
    lst.add_argument("--json", action="store_true")
    lst.set_defaults(func=cmd_list)

    fgt = sub.add_parser("forget", help="delete one row, or the whole namespace")
    fgt.add_argument("--project", required=True)
    fgt.add_argument("--id")
    fgt.add_argument("--all", action="store_true")
    fgt.add_argument("--yes", action="store_true")
    fgt.set_defaults(func=cmd_forget)

    doc = sub.add_parser("doctor", help="check key resolution, install, and auth")
    doc.set_defaults(func=cmd_doctor)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
