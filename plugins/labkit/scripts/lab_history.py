"""Read project-scoped native transcripts without changing provider state.

Exports are a visible-text archive, not a reconstruction of hidden model context.
Native files are append-only inputs here; exports bind a complete captured prefix.
"""
from __future__ import annotations

import hashlib
from contextlib import closing
import json
import ntpath
import os
from pathlib import Path
import re
import sqlite3
import stat
import uuid


_UUID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"


def _plain_path(value):
    value = os.fspath(value)
    if value.lower().startswith("\\\\?\\unc\\"):
        return "\\\\" + value[8:]
    if value.startswith(("\\\\?\\", "\\\\.\\")):
        return value[4:]
    if value.lower().startswith("//?/unc/"):
        return "//" + value[8:]
    if value.startswith(("//?/", "//./")):
        return value[4:]
    return value


def _identity(value):
    value = _plain_path(value)
    if re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith("\\\\"):
        return ntpath.normcase(ntpath.normpath(value)).replace("\\", "/")
    return os.path.normcase(os.path.abspath(value)).replace("\\", "/")


def _safe_path(value):
    path = Path(os.path.abspath(_plain_path(value)))
    for entry in (path, *path.parents):
        try:
            info = entry.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x0400:
            raise ValueError(f"native history path redirects through a symlink or reparse point: {entry}")
    return path


def _session_id(value):
    if not isinstance(value, str) or not re.fullmatch(_UUID, value):
        raise ValueError("native session id must be a UUID")
    return str(uuid.UUID(value))


def _config(provider, config_root=None):
    if provider not in {"claude", "codex"}:
        raise ValueError("provider must be claude or codex")
    if config_root is None:
        env = "CLAUDE_CONFIG_DIR" if provider == "claude" else "CODEX_HOME"
        config_root = os.environ.get(env) or Path.home() / ("." + provider)
    return _safe_path(config_root)


def _claude_directory(config, project):
    # Claude's normal project key is the absolute path with punctuation replaced.
    key = re.sub(r"[^A-Za-z0-9]", "-", str(_safe_path(project)))
    if len(key) > 200:
        raise ValueError("Claude project key exceeds 200 characters; hashed project keys are unsupported")
    return _safe_path(config / "projects" / key)


def _subagent(meta):
    source = meta.get("source")
    return bool(meta.get("agent_path") or meta.get("isSidechain") or meta.get("teamName")
                or "subagent" in str(source).lower()
                or "subagent" in str(meta.get("thread_source", "")).lower())


def _read_prefix(path):
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        data = stream.read(before.st_size)
        after = os.fstat(stream.fileno())
    if (len(data) != before.st_size or after.st_size < before.st_size
            or after.st_size == before.st_size and after.st_mtime_ns != before.st_mtime_ns):
        raise ValueError("native transcript changed or shrank during capture")
    size = data.rfind(b"\n") + 1
    warnings = []
    if size != len(data):
        warnings.append("An incomplete trailing native record was omitted from the captured prefix.")
    if after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns:
        warnings.append("The live native transcript changed during capture; only the initial prefix is included.")
    prefix = data[:size]
    rows = []
    for line, raw in enumerate(prefix.split(b"\n")[:-1], 1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw.decode("utf-8-sig" if line == 1 else "utf-8"))
        except (ValueError, UnicodeDecodeError) as error:
            raise ValueError(f"malformed complete native record at line {line}") from error
        if not isinstance(row, dict):
            raise ValueError(f"native record at line {line} is not an object")
        rows.append((line, row))
    return rows, prefix, before.st_size, warnings


def _codex_meta(path):
    # Discovery only reads the metadata record, never unrelated message bodies.
    with path.open("rb") as stream:
        raw = stream.readline()
    row = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(row, dict) or row.get("type") != "session_meta":
        raise ValueError("Codex transcript must begin with session_meta")
    meta = row.get("payload")
    if not isinstance(meta, dict):
        raise ValueError("Codex session_meta payload is invalid")
    return meta


def _native_path(provider, path, config, project, session_id):
    path = _safe_path(path)
    if provider == "claude":
        if path.parent != _claude_directory(config, project) or path.name.lower() != session_id + ".jsonl":
            raise ValueError("Claude source must be the exact project's top-level UUID transcript")
    else:
        roots = [config / "sessions", config / "archived_sessions"]
        if not any(path.is_relative_to(root) and path != root for root in roots):
            raise ValueError("Codex source is outside native session directories")
        if any(part.lower() in {"subagent", "subagents"} for part in path.relative_to(config).parts):
            raise ValueError("Codex subagent transcript paths are excluded")
        if not re.fullmatch(r"rollout-.+-" + re.escape(session_id) + r"\.jsonl", path.name, re.I):
            raise ValueError("Codex rollout filename does not match the session id")
    if not path.is_file():
        raise ValueError("native session transcript does not exist")
    return path


def _descriptor(provider, path, config, project, session_id, **extra):
    return {"id": session_id, "provider": provider, "path": str(path),
            "cwd": str(_safe_path(project)), "config_root": str(config), **extra}


def _codex_candidates(config, project, warnings):
    candidates = []
    databases = ([path for path in config.glob("state_*.sqlite")
                  if re.fullmatch(r"state_\d+\.sqlite", path.name)] if config.is_dir() else [])
    databases.sort(key=lambda path: int(path.stem.split("_")[1]), reverse=True)
    if databases:
        try:
            database = _safe_path(databases[0])
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
                connection.row_factory = sqlite3.Row
                columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
                required = {"id", "rollout_path", "cwd"}
                if not required <= columns:
                    raise ValueError("Codex state database lacks thread metadata")
                selected = sorted(columns & (required | {"source", "thread_source", "agent_path", "archived"}))
                # The DB stores Windows extended paths in some releases.
                key = _identity(project).lower()
                extended = "//?/unc/" + key[2:] if key.startswith("//") else "//?/" + key
                aliases = (key, extended, "//./" + key)
                sql = ("SELECT " + ",".join(selected) + " FROM threads "
                       "WHERE lower(replace(cwd, char(92), '/')) IN (?,?,?)")
                candidates.extend(dict(row) for row in connection.execute(sql, aliases))
        except (OSError, sqlite3.Error, ValueError) as error:
            warnings.append(f"Codex state metadata unavailable ({type(error).__name__}); using native metadata headers.")
    else:
        warnings.append("Codex state database is absent; sessions were discovered from native metadata headers.")
    # Native headers supplement the index: live or older transcripts may not
    # have a DB row. Only headers are read until the project identity matches.
    for name in ("sessions", "archived_sessions"):
        folder = _safe_path(config / name)
        if not folder.is_dir():
            continue
        for path in folder.rglob("rollout-*.jsonl"):
            try:
                path = _safe_path(path)
                meta = _codex_meta(path)
                if isinstance(meta.get("cwd"), str) and _identity(meta["cwd"]) == _identity(project):
                    candidates.append({**meta, "rollout_path": str(path), "archived": name == "archived_sessions"})
            except (OSError, ValueError, UnicodeDecodeError):
                warnings.append("A native Codex metadata header could not be read and was skipped.")
    return candidates


def discover(provider, project, config_root=None):
    """Find main sessions and scoped Markdown memory for one exact project."""
    project, config = _safe_path(project), _config(provider, config_root)
    warnings, sessions, memory_files = [], [], []
    if provider == "codex":
        for candidate in _codex_candidates(config, project, warnings):
            try:
                if _subagent(candidate) or _identity(candidate["cwd"]) != _identity(project):
                    continue
                session_id = _session_id(candidate["id"])
                path = _native_path(provider, candidate["rollout_path"], config, project, session_id)
                meta = _codex_meta(path)
                if _subagent(meta):
                    continue
                if _session_id(meta.get("id")) != session_id or _identity(meta.get("cwd", "")) != _identity(project):
                    raise ValueError("Codex metadata does not match the project/session")
                sessions.append(_descriptor(provider, path, config, project, session_id,
                                            archived=bool(candidate.get("archived"))))
            except (OSError, ValueError, KeyError, TypeError, UnicodeDecodeError) as error:
                warnings.append(f"A Codex session failed source validation and was skipped ({type(error).__name__}).")
        warnings.append("Codex global memories are not copied because they have no verified exact-project boundary.")
    else:
        folder = _claude_directory(config, project)
        foreign_cwd = False
        if folder.is_dir():
            for path in sorted(folder.glob("*.jsonl")):
                try:
                    session_id = _session_id(path.stem)
                    path = _native_path(provider, path, config, project, session_id)
                    rows, _, _, _ = _read_prefix(path)
                    # Claude replaces punctuation in directory keys, so distinct
                    # checkouts can share this folder. Memory has no own cwd.
                    if any(row.get("cwd") and _identity(row["cwd"]) != _identity(project)
                           for _, row in rows):
                        foreign_cwd = True
                    scoped = [row for _, row in rows if row.get("sessionId") and row.get("cwd")]
                    if not scoped or any(_session_id(row["sessionId"]) != session_id
                                         or _identity(row["cwd"]) != _identity(project) for row in scoped):
                        raise ValueError("Claude session has missing or mismatched project metadata")
                    if all(_subagent(row) for row in scoped):
                        continue
                    sessions.append(_descriptor(provider, path, config, project, session_id))
                except (OSError, ValueError, TypeError, UnicodeDecodeError) as error:
                    warnings.append(f"A Claude session failed source validation and was skipped ({type(error).__name__}).")
            memory = _safe_path(folder / "memory")
            if memory.is_dir():
                if foreign_cwd:
                    warnings.append("Claude auto-memory was omitted because this native project directory contains another cwd; its storage key may collide across checkouts.")
                elif not sessions:
                    warnings.append("Claude auto-memory was omitted because no native main session verifies this exact project cwd.")
                else:
                    for path in sorted(memory.rglob("*.md")):
                        path = _safe_path(path)
                        if path.is_file():
                            memory_files.append(str(path))
    sessions = list({session["id"]: session for session in sessions}.values())
    return {"sessions": sorted(sessions, key=lambda item: item["id"]),
            "memory_files": memory_files, "warnings": warnings}


def _text(content, allowed):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(block["text"] for block in content
                     if isinstance(block, dict) and block.get("type") in allowed
                     and isinstance(block.get("text"), str))


def _message(role, text, row, line, **extra):
    return {"role": role, "text": text, "timestamp": row.get("timestamp"),
            "source_line": line, **extra}


def _codex_messages(rows, project, session_id, warnings):
    if not rows or rows[0][1].get("type") != "session_meta":
        raise ValueError("Codex transcript must begin with session_meta")
    meta = rows[0][1].get("payload", {})
    if (_session_id(meta.get("id")) != session_id
            or _identity(meta.get("cwd", "")) != _identity(project) or _subagent(meta)):
        raise ValueError("Codex native metadata does not match the selected main session/project")
    turn_cwds = {}
    for _, row in rows:
        payload = row.get("payload", {})
        if row.get("type") == "turn_context" and isinstance(payload, dict) and payload.get("cwd"):
            turn_cwds[payload.get("turn_id")] = _identity(payload["cwd"])
    visible_kinds = {"UserMessage", "AgentMessage", "userMessage", "agentMessage"}
    native_turns, event_turns, current_turn = set(), set(), None
    for _, row in rows:
        payload = row.get("payload", {})
        if not isinstance(payload, dict):
            continue
        if row.get("type") == "turn_context":
            current_turn = payload.get("turn_id")
        if row.get("type") == "event_msg":
            if (payload.get("type") == "item_completed" and isinstance(payload.get("item"), dict)
                    and payload["item"].get("type") in visible_kinds):
                native_turns.add(payload.get("turn_id"))
            elif payload.get("type") in {"user_message", "agent_message"}:
                event_turns.add(current_turn)
    messages, seen, foreign = [], set(), 0
    current_cwd, current_turn, fallback_used = _identity(project), None, False
    for line, row in rows:
        payload = row.get("payload", {})
        if row.get("type") == "compacted":
            warnings.append(f"Compaction boundary at source line {line}; generated replacement context is excluded.")
        if not isinstance(payload, dict):
            continue
        if row.get("type") == "turn_context":
            current_turn = payload.get("turn_id")
            if payload.get("cwd"):
                current_cwd = _identity(payload["cwd"])
        is_completed = (row.get("type") == "event_msg" and payload.get("type") == "item_completed"
                        and isinstance(payload.get("item"), dict)
                        and payload["item"].get("type") in visible_kinds)
        if is_completed:
            if payload.get("thread_id") and _session_id(payload["thread_id"]) != session_id:
                raise ValueError("Codex completed item belongs to another session")
            item = payload.get("item", {})
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if turn_cwds.get(payload.get("turn_id"), current_cwd) != _identity(project):
                foreign += 1
                continue
            role = "user" if kind in {"UserMessage", "userMessage"} else "assistant"
            if item.get("channel") in {"analysis", "reasoning"}:
                continue
            if role == "assistant" and item.get("phase") not in {None, "commentary", "final_answer", "final"}:
                continue
            text = item.get("text") if isinstance(item.get("text"), str) else _text(item.get("content"), {"text", "Text", "input_text", "output_text"})
            key = (payload.get("turn_id"), item.get("id"))
            if item.get("id") and key in seen:
                continue
            seen.add(key)
            extra = {"message_id": item.get("id"), "turn_id": payload.get("turn_id"), "phase": item.get("phase")}
        elif current_turn in native_turns or native_turns and current_turn is None:
            # Never mix model-input records into a turn with native UI events.
            # Unbound preambles in such logs can contain injected instructions.
            continue
        elif current_turn in event_turns:
            if row.get("type") != "event_msg" or payload.get("type") not in {"user_message", "agent_message"}:
                continue
            role = "user" if payload["type"] == "user_message" else "assistant"
            text = payload.get("message", "")
            extra = {}
            fallback_used = True
        else:
            if row.get("type") != "response_item" or payload.get("type") != "message":
                continue
            role = payload.get("role")
            if role not in {"user", "assistant"} or payload.get("channel") in {"analysis", "reasoning"}:
                continue
            if payload.get("phase") in {"analysis", "reasoning"} or payload.get("recipient") not in {None, "all"}:
                continue
            text = _text(payload.get("content"), {"text", "input_text", "output_text"})
            extra = {"phase": payload.get("phase")}
            fallback_used = True
        if not is_completed and current_cwd != _identity(project):
            foreign += 1
            continue
        if isinstance(text, str) and text:
            messages.append(_message(role, text, row, line, **extra))
    if fallback_used or not native_turns:
        warnings.append("Legacy transcript fallback was used; native visible-message coverage may be incomplete and user-role context may include injected text.")
    if foreign:
        warnings.append(f"Omitted {foreign} visible messages associated with a different project cwd.")
    return messages


def _claude_messages(rows, project, session_id, warnings):
    messages, seen, bound, branches = [], set(), False, {}
    for line, row in rows:
        if row.get("sessionId") and _session_id(row["sessionId"]) != session_id:
            raise ValueError("Claude native record belongs to another session")
        if row.get("cwd") and _identity(row["cwd"]) != _identity(project):
            raise ValueError("Claude native record belongs to another project cwd")
        if row.get("cwd") and row.get("sessionId"):
            bound = True
        if row.get("isCompactSummary") or row.get("type") == "system" and row.get("subtype") == "compact_boundary":
            warnings.append(f"Compaction record at source line {line} was excluded; original visible records are retained.")
            continue
        if row.get("type") not in {"user", "assistant"} or row.get("isMeta") or _subagent(row):
            continue
        message = row.get("message", {})
        if not isinstance(message, dict) or message.get("role", row["type"]) != row["type"]:
            continue
        text = _text(message.get("content"), {"text"})
        message_id = row.get("uuid")
        if message_id and message_id in seen:
            continue
        if message_id:
            seen.add(message_id)
            parent = row.get("parentUuid")
            if parent is not None:
                branches.setdefault(parent, set()).add(message_id)
        if text:
            messages.append(_message(row["type"], text, row, line, message_id=message_id,
                                     parent_id=row.get("parentUuid")))
    if not bound:
        raise ValueError("Claude native transcript lacks exact session/project metadata")
    if any(len(children) > 1 for children in branches.values()):
        warnings.append("Claude transcript contains branches; visible records are archived in source order, not reconstructed as one active context chain.")
    return messages


def export_session(session, project, destination):
    """Export only visible text from a freshly validated native source prefix."""
    provider = session.get("provider")
    config, project = _config(provider, session.get("config_root")), _safe_path(project)
    session_id = _session_id(session.get("id"))
    if _identity(session.get("cwd", "")) != _identity(project):
        raise ValueError("selected session cwd does not match project")
    path = _native_path(provider, session.get("path", ""), config, project, session_id)
    destination = _safe_path(destination)
    if destination == path or destination.is_relative_to(config):
        raise ValueError("export destination must be outside native provider storage")
    rows, prefix, source_bytes, warnings = _read_prefix(path)
    parser = _codex_messages if provider == "codex" else _claude_messages
    messages = parser(rows, project, session_id, warnings)
    # Exclusive creation prevents overwriting either an existing packet or input.
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8", newline="\n") as stream:
        for message in messages:
            stream.write(json.dumps(message, ensure_ascii=False) + "\n")
    return {"session_id": session_id, "provider": provider, "source_path": str(path),
            "export_path": str(destination), "source_bytes": source_bytes,
            "captured_bytes": len(prefix), "source_prefix_sha256": hashlib.sha256(prefix).hexdigest(),
            "message_count": len(messages), "format": "visible-messages-jsonl-v1",
            "warnings": list(dict.fromkeys(warnings))}
