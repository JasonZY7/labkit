#!/usr/bin/env python3
"""Prepare and accept a local checkout handoff between Claude Code and Codex.

prepare --directory PROJECT --from claude --to codex --context-file CONTEXT.json --source-quiescent
accept PACKET --directory PROJECT --host codex --receipt-file RECEIPT.json
status --directory PROJECT | cancel --directory PROJECT --id ID --reason TEXT

Native sessions are read, never rewritten. Private packets stay in this checkout.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import lab_history
import lab_run_legacy as legacy
from lab_mem import _atomic_write

PRIVATE = ".labkit-local"
HOSTS = ("claude", "codex")
CONTEXT_LISTS = ("constraints", "decisions", "rejected_approaches", "ideas",
                 "open_questions", "next_steps", "verification", "important_files")
PROJECT_RECORDS = ("AGENTS.md", "CLAUDE.md", "HANDOFF.md", "HANDOVER.md", "README.md", "notes/log.md", ".labkit.json")


def dump(path, value):
    _atomic_write(Path(path), json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label + " must be nonempty text")
    return value


def git(project, *args, allow_failure=False):
    result = subprocess.run(["git", "--no-optional-locks", "-C", str(project), *args], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    if result.returncode and not allow_failure:
        raise ValueError("Git inspection failed: " + result.stderr.decode("utf-8", "replace").strip())
    return result.stdout if result.returncode == 0 else b""


def canonical_project(project, *, required=True):
    value = git(Path(project).resolve(), "rev-parse", "--show-toplevel", allow_failure=not required).decode("utf-8").strip()
    if not value:
        return Path(project).resolve()
    return Path(value).resolve()


def is_link(path):
    return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x0400)


def contained(project, relative):
    value = Path(text(relative, "project file"))
    if value.is_absolute() or ".." in value.parts or not value.parts:
        raise ValueError("project file must be a relative path inside this checkout")
    path = project / value
    if not path.resolve().is_relative_to(project) or value.parts[0] in (".git", PRIVATE):
        raise ValueError("project file cannot point outside the checkout or into internal records")
    return path


def digest_file(path):
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError("file changed during inspection: " + str(path))
    return digest.hexdigest()


def repo_snapshot(project):
    """Fingerprint the actual checkout, index, tracked and nonignored untracked files."""
    project = canonical_project(project)
    common = Path(git(project, "rev-parse", "--git-common-dir").decode("utf-8").strip())
    if not common.is_absolute():
        common = project / common
    index = git(project, "ls-files", "--stage", "-z")
    names = git(project, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    files = {}
    for name in sorted(set(names.decode("utf-8").split("\0")) - {""}):
        if name == ".labkit-run.lock" or Path(name).parts[0] == PRIVATE:
            continue
        path = project / name
        if path.is_symlink():
            files[name] = {"symlink": os.readlink(path)}
        elif not path.exists():
            files[name] = {"missing": True}
        elif is_link(path):
            raise ValueError("cannot fingerprint a reparse-point checkout entry: " + name)
        elif path.is_file():
            files[name] = {"sha256": digest_file(path)}
        elif path.is_dir():
            # git ls-files lists a directory for a submodule, not ordinary folders.
            subroot = canonical_project(path)
            if subroot != path.resolve() or not subroot.is_relative_to(project):
                raise ValueError("unavailable or invalid submodule: " + name)
            files[name] = {"submodule": repo_snapshot(path)}
    result = {"project": str(project),
              "git_dir": str(Path(git(project, "rev-parse", "--absolute-git-dir").decode("utf-8").strip()).resolve()),
              "common_dir": str(common.resolve()),
              "head": git(project, "rev-parse", "--verify", "HEAD", allow_failure=True).decode().strip() or None,
              "branch": git(project, "symbolic-ref", "--quiet", "--short", "HEAD", allow_failure=True).decode("utf-8").strip() or None,
              "index_sha256": hashlib.sha256(index).hexdigest(), "files": files}
    result["fingerprint"] = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return result


def private_root(project, create=False):
    directory = project / PRIVATE
    if directory.exists() and (not directory.is_dir() or is_link(directory)):
        raise ValueError("handoff directory must be an ordinary directory in this checkout")
    if git(project, "ls-files", "-z", "--", PRIVATE):
        raise ValueError("private handoff directory is tracked by Git; choose a private checkout before exporting history")
    marker = directory / ".gitignore"
    for child in (marker, directory / "transfers", directory / "transfer-state.json"):
        if child.exists() and is_link(child):
            raise ValueError("private handoff paths cannot be links or reparse points")
    if create:
        directory.mkdir(exist_ok=True)
        if marker.exists() and marker.read_text(encoding="utf-8").strip() != "*":
            raise ValueError("existing handoff directory has different ignore rules; it was not replaced")
        if not marker.exists():
            _atomic_write(marker, "*\n")
    return directory


def detect_host():
    override = os.getenv("LABKIT_HOST")
    if override:
        if override not in HOSTS:
            raise ValueError("LABKIT_HOST must be claude or codex")
        return override
    if os.getenv("CLAUDECODE"):
        return "claude"
    if os.getenv("CODEX_THREAD_ID"):
        return "codex"
    return None


def status(project):
    project = canonical_project(project)
    path = private_root(project) / "transfer-state.json"
    if not path.exists():
        return None
    state = read_json(path)
    if state.get("format") != 1 or Path(state["project"]).resolve() != project:
        raise ValueError("handoff state belongs to a different checkout")
    return state


def check_owner(project, host=None):
    # A subdirectory is part of the same checkout and cannot bypass its owner.
    project = canonical_project(project, required=False)
    if not (project / PRIVATE / "transfer-state.json").exists():
        return
    state = status(project)
    if state["status"] == "ready":
        raise RuntimeError("project handoff awaits acceptance or cancellation: " + state["handoff_id"])
    actual = host or detect_host()
    if actual != state["owner"]:
        raise RuntimeError("this checkout is owned by " + state["owner"] +
                           "; hand it back before execution. A plain shell must set LABKIT_HOST to its actual host.")


def validate_context(context, project):
    if not isinstance(context, dict) or set(context) != {"objective", "current_state", *CONTEXT_LISTS}:
        raise ValueError("context needs objective/current_state and " + ", ".join(CONTEXT_LISTS))
    for key in ("objective", "current_state"):
        text(context[key], key)
    for key in CONTEXT_LISTS:
        if not isinstance(context[key], list):
            raise ValueError(key + " must be a list")
        for value in context[key]:
            text(value, key)
    if not context["next_steps"]:
        raise ValueError("record at least one concrete next step")
    for value in context["important_files"]:
        if not contained(project, value).is_file():
            raise ValueError("important file is missing: " + value)


def reference_snapshot(project, context):
    """Explicit context references matter even when Git ignores them."""
    names = set(context["important_files"])
    names.update(name for name in PROJECT_RECORDS if (project / name).is_file())
    return {name: digest_file(contained(project, name)) for name in sorted(names)}


def quiescent_runs(project):
    runs = []
    for path in sorted((project / "handoffs" / "runs").glob("*/state.json")):
        state = legacy.load(path.parent)
        pending = state.get("pending") or {}
        if state["status"] in ("running", "awaiting_host", "awaiting_worker", "review_ready") or (
                pending and state["status"] != "cancelled" and not
                (state["status"] == "interrupted" and pending.get("mode") == "cli")):
            raise RuntimeError("resolve or stop the existing run before handoff: " + str(path.parent))
        runs.append({"path": str(path.parent), "state_sha256": digest_file(path),
                     "status": state["status"], "format": state["format"],
                     "goal": state.get("goal", state.get("task")),
                     "pending": pending or None})
    return runs


def memory_snapshot(project, packet, discovery, include_memory):
    memory_dir = packet / "memory"
    memory_dir.mkdir()
    result = {"project_files": [], "claude_auto_memory": [], "mem0": {"status": "not_configured"}}
    for relative in PROJECT_RECORDS:
        path = contained(project, relative)
        if path.is_file() and not is_link(path):
            result["project_files"].append({"path": relative, "sha256": digest_file(path)})
    for number, item in enumerate(discovery.get("memory_files", []), 1):
        source = Path(item["path"] if isinstance(item, dict) else item)
        if not source.is_file() or is_link(source):
            raise ValueError("auto-memory source is missing or is a link")
        before = digest_file(source)
        content = source.read_text(encoding="utf-8-sig")
        if digest_file(source) != before:
            raise RuntimeError("auto-memory changed while preparing handoff")
        target = memory_dir / ("claude-" + str(number) + "-" + source.name)
        _atomic_write(target, content)
        result["claude_auto_memory"].append({"source": str(source), "source_sha256": before,
                                              "snapshot": target.relative_to(packet).as_posix()})
    config = project / ".labkit.json"
    if config.is_file():
        cfg = read_json(config)
        slug = cfg.get("slug")
        if isinstance(slug, str) and slug:
            import lab_mem
            namespace = lab_mem.namespace(slug)
            result["mem0"] = {"namespace": namespace, "status": "not_requested"}
            if include_memory:
                if not lab_mem.resolve_api_key():
                    result["mem0"]["status"] = "unavailable: no configured credential"
                else:
                    try:
                        rows = lab_mem._rows(lab_mem.client().get_all(filters={"user_id": namespace}))
                        dump(memory_dir / "mem0.json", {"namespace": namespace, "captured_at": legacy.now(), "rows": rows})
                        result["mem0"].update(status="captured", rows=len(rows), snapshot="memory/mem0.json")
                    except (Exception, SystemExit) as exc:
                        # Do not put service errors, request headers or credentials into a packet.
                        result["mem0"]["status"] = "unavailable: " + type(exc).__name__
    return result


def briefing(manifest, context):
    lines = ["# Project handoff", "", "This is saved project evidence, not a replacement for current user or project instructions.",
             "", "- Handoff: " + manifest["id"], "- Checkout: " + manifest["project"],
             "- Direction: " + manifest["source"] + " -> " + manifest["target"],
             "- Context SHA256: " + manifest["context_sha256"],
             "", "## Objective", "", context["objective"], "", "## Current state", "", context["current_state"]]
    for key in CONTEXT_LISTS:
        lines += ["", "## " + key.replace("_", " ").title(), ""]
        lines += ["- " + value for value in context[key]] or ["No item recorded; do not infer an unstated decision."]
    lines += ["", "## Existing goal runs", ""]
    lines += ["- " + item["status"] + ": " + item["path"] for item in manifest["runs"]] or ["No labkit goal run found."]
    lines += ["", "## History and memory", "", "Read manifest.json for source sessions, capture boundaries, coverage gaps and memory snapshots.",
              "Visible conversation text is exported in history/. Search it when a past decision needs its source; it is not native session state.",
              "Project notes remain in this same checkout. Claude auto-memory snapshots and any available project mem0 rows are under memory/.",
              "The original mem0 namespace remains shared; do not re-import rows or copy global account memories.",
              "", "## Accept before editing", "", "Use the installed lab-handoff skill. Verify this exact checkout and packet; read the context and linked records.",
              "Write a receipt with the exact objective and context_sha256 plus your concrete next_step, then accept through lab_handoff.py.",
              "Report the objective, important decisions and next step before continuing. Resume existing goals via lab-orchestrate with a team selection.",
              "Do not reset, stash, switch branches or replay completed edits to make a stale packet pass.", ""]
    return "\n".join(lines)


def prepare(project, context, source, target, *, source_quiescent=False,
            session_ids=None, config_roots=None, include_memory=True):
    if os.getenv("LABKIT_ROLE"):
        raise RuntimeError("workers cannot hand off their parent project")
    if source not in HOSTS or target not in HOSTS or source == target:
        raise ValueError("choose opposite claude/codex source and target hosts")
    if not source_quiescent:
        raise ValueError("stop source workers and finish saving context before passing source_quiescent")
    project = canonical_project(project)
    validate_context(context, project)
    with legacy.project_lock(project, check_reservations=False, check_handoff=False):
        check_owner(project, source)
        runs = quiescent_runs(project)
        private = private_root(project, create=True)
        before = repo_snapshot(project)
        references = reference_snapshot(project, context)
        previous = status(project)
        handoff_id = legacy.now().split(".")[0].replace("-", "").replace(":", "") + "-" + uuid.uuid4().hex[:8]
        packet = private / "transfers" / handoff_id
        packet.mkdir(parents=True)
        dump(packet / "context.json", context)
        config_roots = config_roots or {}
        discovery = lab_history.discover(source, project, config_root=config_roots.get(source))
        sessions = discovery["sessions"]
        if session_ids is not None:
            wanted = set(session_ids)
            sessions = [s for s in sessions if s["id"] in wanted]
            if wanted != {s["id"] for s in sessions}:
                raise ValueError("requested session was not found in this exact project's history")
        history_dir = packet / "history"
        history_dir.mkdir()
        exports = [lab_history.export_session(s, project, history_dir / (str(i) + ".jsonl"))
                   for i, s in enumerate(sessions, 1)]
        memory_discovery = discovery if source == "claude" else lab_history.discover("claude", project, config_root=config_roots.get("claude"))
        memory = memory_snapshot(project, packet, memory_discovery, include_memory)
        gaps = list(dict.fromkeys([*discovery.get("warnings", []), *memory_discovery.get("warnings", [])]))
        if not sessions:
            gaps.append("No source-host main session found for this checkout; context and project files are available, but conversation coverage is empty.")
        for exported in exports:
            gaps.extend(exported.get("warnings", []))
        if memory["mem0"]["status"].startswith("unavailable"):
            gaps.append("Project mem0 could not be captured; reconnect to the same namespace before relying on cloud-only decisions.")
        after = repo_snapshot(project)
        if before != after or references != reference_snapshot(project, context) or runs != quiescent_runs(project):
            raise RuntimeError("checkout changed during preparation; the incomplete packet was retained, no ownership was transferred")
        manifest = {"format": 1, "id": handoff_id, "created_at": legacy.now(), "project": str(project),
                    "source": source, "target": target, "source_quiescent": "source host reported its workers stopped; not an OS-wide process attestation",
                    "previous_handoff_id": previous.get("handoff_id") if previous else None,
                    "context_sha256": digest_file(packet / "context.json"), "repository": after, "referenced_files": references,
                    "history": exports, "memory": memory, "coverage_gaps": gaps, "runs": runs}
        _atomic_write(packet / "BRIEFING.md", briefing(manifest, context))
        entry = "$lab-handoff" if target == "codex" else "/labkit:lab-handoff"
        _atomic_write(packet / "OPEN_TARGET.txt", "请使用 " + entry + " 接收交接：" + str(packet) +
                      "。在同一本地 checkout 继续，先读取交接记录并校验代码状态，复述目标、关键决定和下一步，再接收执行权。\n")
        manifest["packet_files"] = {p.relative_to(packet).as_posix(): digest_file(p)
                                     for p in sorted(packet.rglob("*")) if p.is_file()}
        dump(packet / "manifest.json", manifest)
        state = {"format": 1, "project": str(project), "status": "ready", "owner": source,
                 "source": source, "target": target, "handoff_id": handoff_id, "packet": str(packet),
                 "manifest_sha256": digest_file(packet / "manifest.json"), "updated_at": legacy.now()}
        dump(private / "transfer-state.json", state)
    return packet


def load_packet(packet, project):
    project = canonical_project(project)
    expected_parent = private_root(project) / "transfers"
    packet = Path(packet).resolve()
    if packet.parent != expected_parent.resolve() or is_link(packet):
        raise ValueError("packet must be in this exact checkout's private transfer directory")
    manifest = read_json(packet / "manifest.json")
    if manifest.get("format") != 1 or manifest["id"] != packet.name or Path(manifest["project"]).resolve() != project:
        raise ValueError("packet identity or checkout does not match")
    for relative, digest in manifest["packet_files"].items():
        path = packet / relative
        if Path(relative).is_absolute() or ".." in Path(relative).parts or not path.resolve().is_relative_to(packet) or is_link(path):
            raise ValueError("invalid packet file path")
        if digest_file(path) != digest:
            raise ValueError("packet file changed after preparation: " + relative)
    return manifest


def accept(packet, project, target, receipt):
    if os.getenv("LABKIT_ROLE"):
        raise RuntimeError("workers cannot accept their parent project's handoff")
    project = canonical_project(project)
    with legacy.project_lock(project, check_reservations=False, check_handoff=False):
        manifest = load_packet(packet, project)
        packet = Path(packet).resolve()
        state = status(project)
        if not state or state["handoff_id"] != manifest["id"] or state["manifest_sha256"] != digest_file(packet / "manifest.json"):
            raise ValueError("this is not the current handoff; a stale packet cannot acquire the checkout")
        if target != manifest["target"]:
            raise ValueError("handoff is addressed to a different target host")
        if not isinstance(receipt, dict) or set(receipt) != {"objective", "next_step", "context_sha256"}:
            raise ValueError("receipt needs objective, next_step and context_sha256")
        context = read_json(packet / "context.json")
        if receipt["objective"] != context["objective"] or receipt["context_sha256"] != manifest["context_sha256"]:
            raise ValueError("receipt must acknowledge this exact context and original objective")
        text(receipt["next_step"], "next_step")
        if state["status"] == "active" and state["owner"] == target:
            saved = read_json(packet / "receipt.json")
            if saved["acknowledgement"] != receipt:
                raise ValueError("this handoff was already accepted with a different receipt")
            return state
        if state["status"] != "ready":
            raise ValueError("this handoff is no longer awaiting acceptance")
        if (quiescent_runs(project) != manifest["runs"] or repo_snapshot(project) != manifest["repository"]
                or reference_snapshot(project, context) != manifest["referenced_files"]):
            raise ValueError("checkout changed since preparation; refresh the handoff instead of resetting or replaying edits")
        dump(packet / "receipt.json", {"accepted_at": legacy.now(), "target": target,
                                       "acknowledgement": receipt, "evidence": "target agent acknowledgement; not proof of model comprehension"})
        state.update(status="active", owner=target, updated_at=legacy.now())
        dump(private_root(project) / "transfer-state.json", state)
        return state


def cancel(project, handoff_id, reason, host=None):
    text(reason, "cancellation reason")
    project = canonical_project(project)
    with legacy.project_lock(project, check_reservations=False, check_handoff=False):
        state = status(project)
        if not state or state["handoff_id"] != handoff_id or state["status"] != "ready":
            raise ValueError("only the current unaccepted handoff can be cancelled")
        actual = host or detect_host()
        if actual not in (state["source"], state["target"]):
            raise ValueError("cancellation must come from one of the named hosts")
        dump(Path(state["packet"]) / "cancellation.json", {"reason": reason, "host": actual, "at": legacy.now()})
        state.update(status="active", owner=state["source"], updated_at=legacy.now())
        dump(private_root(project) / "transfer-state.json", state)
        return state


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("prepare")
    start.add_argument("--directory", required=True)
    start.add_argument("--from", dest="source", choices=HOSTS, required=True)
    start.add_argument("--to", dest="target", choices=HOSTS, required=True)
    start.add_argument("--context-file", required=True)
    start.add_argument("--source-quiescent", action="store_true", required=True)
    start.add_argument("--session", action="append", dest="session_ids")
    start.add_argument("--without-cloud-memory", action="store_true")
    receive = commands.add_parser("accept")
    receive.add_argument("packet")
    receive.add_argument("--directory", required=True)
    receive.add_argument("--host", choices=HOSTS, required=True)
    receive.add_argument("--receipt-file", required=True)
    show = commands.add_parser("status")
    show.add_argument("--directory", required=True)
    guard = commands.add_parser("guard")
    guard.add_argument("--directory", required=True)
    guard.add_argument("--host", choices=HOSTS)
    stop = commands.add_parser("cancel")
    stop.add_argument("--directory", required=True)
    stop.add_argument("--id", required=True)
    stop.add_argument("--reason", required=True)
    stop.add_argument("--host", choices=HOSTS)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("packet")
    inspect.add_argument("--directory", required=True)
    workspace = commands.add_parser("workspace")
    workspace.add_argument("--directory", required=True)
    history = commands.add_parser("sessions")
    history.add_argument("--directory", required=True)
    history.add_argument("--host", choices=HOSTS, required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            packet = prepare(args.directory, read_json(args.context_file), args.source, args.target,
                             source_quiescent=args.source_quiescent, session_ids=args.session_ids,
                             include_memory=not args.without_cloud_memory)
            print("Handoff prepared: " + str(packet))
            print((packet / "OPEN_TARGET.txt").read_text(encoding="utf-8"))
            return 0
        if args.command == "accept":
            value = accept(args.packet, args.directory, args.host, read_json(args.receipt_file))
        elif args.command == "cancel":
            value = cancel(args.directory, args.id, args.reason, args.host)
        elif args.command == "inspect":
            value = load_packet(args.packet, args.directory)
        elif args.command == "workspace":
            value = {"private_directory": str(private_root(canonical_project(args.directory), create=True))}
        elif args.command == "guard":
            check_owner(canonical_project(args.directory), args.host)
            value = {"execution_allowed": True, "host": args.host or detect_host()}
        elif args.command == "sessions":
            value = lab_history.discover(args.host, canonical_project(args.directory))
        else:
            value = status(args.directory)
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
