"""Verify native Claude results against a task-bound runtime transcript."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid


def _required_text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")
    return value


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(block["text"] for block in content
                       if isinstance(block, dict) and block.get("type") == "text"
                       and isinstance(block.get("text"), str))
    return ""


def _checked_file(filename, boundary):
    original = Path(os.path.abspath(Path(filename).expanduser()))
    try:
        path = original.resolve(strict=True)
    except FileNotFoundError as error:
        raise ValueError(f"native runtime evidence is missing: {original.name}") from error
    if not path.is_relative_to(boundary) or path == boundary or not path.is_file():
        raise ValueError("native runtime evidence must be a file inside its expected directory")
    if path != original:
        raise ValueError("native runtime evidence must not redirect through a symlink or junction")
    for ancestor in (path, *path.parents):
        info = ancestor.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x0400:
            raise ValueError("native runtime evidence must not contain a reparse point")
        if ancestor == boundary:
            break
    return path


def _transcript_path(transcript_file, config_root):
    root = Path(config_root or os.getenv("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    projects = (root.expanduser() / "projects").resolve()
    path = _checked_file(transcript_file, projects)
    relative = path.relative_to(projects)
    parts = relative.parts
    if len(parts) < 4 or parts[2] != "subagents" or not path.is_file():
        raise ValueError("native transcript must be a session subagent JSONL file")
    try:
        session_id = str(uuid.UUID(parts[1]))
    except ValueError as error:
        raise ValueError("native transcript directory must identify a session UUID") from error
    match = re.fullmatch(r"agent-([A-Za-z0-9_-]+)\.jsonl", parts[-1])
    if match is None:
        raise ValueError("native transcript filename must identify an agent")
    return path, session_id, match.group(1)


def _jsonl(raw, label):
    try:
        # JSON strings may legally contain Unicode line separators; JSONL uses LF.
        rows = [json.loads(line) for line in raw.decode("utf-8-sig").split("\n") if line.strip()]
    except (UnicodeError, ValueError) as error:
        raise ValueError(f"native {label} must contain complete UTF-8 JSONL records") from error
    if not rows or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"native {label} must contain JSON objects")
    return rows


def _completion(path, agent_id, model, role, expected):
    meta_path = _checked_file(path.with_suffix(".meta.json"), path.parent)
    journal_path = _checked_file(path.parent / "journal.jsonl", path.parent)
    meta_raw, journal_raw = meta_path.read_bytes(), journal_path.read_bytes()
    try:
        meta = json.loads(meta_raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as error:
        raise ValueError("native agent metadata must be valid UTF-8 JSON") from error
    agent_type = "general-purpose" if role == "worker" else "Plan"
    if not isinstance(meta, dict) or meta.get("agentType") != agent_type:
        raise ValueError(f"native agent type does not match requested role: expected {agent_type}")
    if meta.get("model") != model:
        raise ValueError("native agent metadata model does not match requested model")
    entries = [row for row in _jsonl(journal_raw, "workflow journal")
               if row.get("agentId") == agent_id]
    if not entries or entries[-1].get("type") != "result":
        raise ValueError("native workflow journal does not prove this agent has completed")
    result = entries[-1]
    key = _required_text(result.get("key"), "native completion key")
    started = next((row for row in reversed(entries[:-1]) if row.get("type") == "started"), None)
    if started is None or started.get("key") != key:
        raise ValueError("native workflow completion does not match the latest agent start")
    if _required_text(result.get("result"), "native completed result").strip() != expected:
        raise ValueError("report does not match the completed native workflow result")
    return {
        "metadata": {"path": str(meta_path), "sha256": hashlib.sha256(meta_raw).hexdigest(),
                     "agent_type": agent_type, "model": meta["model"],
                     "spawn_depth": meta.get("spawnDepth")},
        "completion": {"path": str(journal_path), "sha256": hashlib.sha256(journal_raw).hexdigest(),
                       "agent_id": agent_id, "key": key, "source": "native-workflow-journal"},
    }


def verify(pending, report, transcript_file, *, config_root=None):
    """Bind a native response to its pending token, exact model, and final text.

    ``config_root`` is a Claude configuration directory, with ``projects/``
    beneath it. Evidence comes from the local runtime record, not a model's
    self-description; this does not authenticate against local file tampering.
    """
    if not isinstance(pending, dict) or not isinstance(pending.get("member"), dict):
        raise ValueError("pending native request must contain a member")
    member = pending["member"]
    if member.get("provider") != "claude":
        raise ValueError("native Claude transcript cannot verify another provider")
    model = _required_text(member.get("model"), "requested model")
    token = _required_text(pending.get("token"), "pending token")
    artifact = _required_text(pending.get("artifact"), "pending artifact")
    expected = _required_text(report, "report").strip()
    path, session_id, agent_id = _transcript_path(transcript_file, config_root)
    raw = path.read_bytes()
    rows = _jsonl(raw, "transcript")

    token_seen = False
    actual_models = []
    final_text = None
    for row in rows:
        if row.get("type") not in ("user", "assistant"):
            continue
        if row.get("sessionId") != session_id or row.get("agentId") != agent_id:
            raise ValueError("native transcript conversation identity does not match its path")
        message = row.get("message")
        if not isinstance(message, dict):
            raise ValueError("native conversation record has no message object")
        if row["type"] == "user":
            if token in _text(message.get("content")):
                token_seen = True
            continue
        if not token_seen:
            raise ValueError("native assistant response precedes the pending user prompt")
        actual = message.get("model")
        if actual != model:
            raise ValueError(f"native assistant model does not match requested model {model}")
        if actual not in actual_models:
            actual_models.append(actual)
        final_text = _text(message.get("content"))
    if not token_seen:
        raise ValueError("native transcript user prompt does not contain the pending token")
    if not actual_models or final_text is None:
        raise ValueError("native transcript contains no model response")
    if final_text.strip() != expected:
        raise ValueError("report does not match the final native assistant response")
    runtime = _completion(path, agent_id, model, pending.get("role"), expected)

    return {
        "text": expected, "provider": "claude", "requested_model": model,
        "requested_effort": member.get("effort"), "effort_verified": False,
        "role": pending.get("role"),
        "actual_models": actual_models, "identity_verified": True,
        "identity_source": "native-transcript", "session_id": session_id,
        "transcript": {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
                       "session_id": session_id, "agent_id": agent_id,
                       "artifact": artifact, "token": token,
                       "started_at": pending.get("started_at"), **runtime},
    }
