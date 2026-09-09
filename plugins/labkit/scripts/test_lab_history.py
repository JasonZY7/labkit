"""Offline fixtures for project-scoped, visible native conversation history."""
import hashlib
import json
import os
from contextlib import closing
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest

import lab_history
from lab_fs import trash


class HistoryTests(unittest.TestCase):
    def setUp(self):
        parent = Path(tempfile.gettempdir()).resolve()
        self.root = Path(tempfile.mkdtemp(prefix="labkit-history-tests-", dir=parent)).resolve()
        self.addCleanup(trash, self.root, within=parent)
        self.project = self.root / "project"
        self.project.mkdir()
        self.claude = self.root / "claude"
        self.codex = self.root / "codex"
        self.session_id = "01234567-89ab-4cde-8f01-23456789abcd"
        self.other_id = "12345678-9abc-4def-8012-3456789abcde"
        self.third_id = "23456789-abcd-4ef0-8123-456789abcdef"
        self.timestamp = "2026-09-09T12:00:00Z"

    def write_jsonl(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        # Deliberately preserve non-ASCII text and an LF byte boundary.
        path.write_bytes(("\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
                          + "\n").encode("utf-8"))
        return path

    def claude_dir(self, project=None):
        encoded = re.sub(r"[^a-zA-Z0-9]", "-", str(project or self.project))
        return self.claude / "projects" / encoded

    def claude_event(self, role, text, *, uuid="message-1", parent=None, **extra):
        return {
            "type": role, "uuid": uuid, "parentUuid": parent,
            "sessionId": self.session_id, "cwd": str(self.project),
            "timestamp": self.timestamp,
            "message": {"role": role, "content": text}, **extra,
        }

    def claude_rows(self):
        return [self.claude_event("user", "原始问题", uuid="user-1"),
                self.claude_event("assistant", [{"type": "text", "text": "可见回答"}],
                                  uuid="answer-1", parent="user-1")]

    def claude_file(self, rows=None, *, session_id=None, directory=None):
        return self.write_jsonl(
            (directory or self.claude_dir()) / f"{session_id or self.session_id}.jsonl",
            self.claude_rows() if rows is None else rows)

    def codex_meta(self, *, session_id=None, cwd=None, source="cli"):
        return {"type": "session_meta", "timestamp": self.timestamp,
                "payload": {"id": session_id or self.session_id,
                            "cwd": str(self.project) if cwd is None else cwd,
                            "source": source}}

    def turn(self, turn_id="turn-1", *, cwd=None):
        return {"type": "turn_context", "timestamp": self.timestamp,
                "payload": {"turn_id": turn_id,
                            "cwd": str(self.project) if cwd is None else cwd}}

    def response(self, role, text, *, item_id=None, phase=None, **extra):
        payload = {"type": "message", "role": role,
                   "content": [{"type": "input_text" if role == "user" else "output_text",
                                "text": text}], **extra}
        if item_id is not None:
            payload["id"] = item_id
        if phase is not None:
            payload["phase"] = phase
        return {"type": "response_item", "timestamp": self.timestamp, "payload": payload}

    def completed(self, item_type, text, *, item_id="item-1", phase=None,
                  turn_id="turn-1", block_type="Text"):
        item = {"type": item_type, "id": item_id,
                "content": [{"type": block_type, "text": text}]}
        if phase is not None:
            item["phase"] = phase
        return {"type": "event_msg", "timestamp": self.timestamp,
                "payload": {"type": "item_completed", "thread_id": self.session_id,
                            "turn_id": turn_id, "item": item}}

    def codex_rows(self, *, session_id=None, cwd=None, source="cli"):
        return [self.codex_meta(session_id=session_id, cwd=cwd, source=source),
                self.turn(cwd=cwd), self.response("user", "原始问题"),
                self.response("assistant", "可见回答", phase="final_answer")]

    def codex_file(self, rows=None, *, session_id=None, directory=None):
        return self.write_jsonl(
            (directory or self.codex / "sessions" / "2026" / "09" / "09")
            / f"rollout-2026-09-09T12-00-00-{session_id or self.session_id}.jsonl",
            self.codex_rows(session_id=session_id) if rows is None else rows)

    def discover(self, provider):
        return lab_history.discover(provider, self.project,
                                    config_root=getattr(self, provider))

    def session(self, provider):
        result = self.discover(provider)
        matching = [session for session in result["sessions"]
                    if session["id"] == self.session_id]
        self.assertEqual(len(matching), 1, result)
        return matching[0]

    def export(self, provider, *, session=None, suffix=""):
        destination = self.root / "exports" / f"{provider}{suffix}.jsonl"
        destination.parent.mkdir(parents=True, exist_ok=True)
        metadata = lab_history.export_session(
            self.session(provider) if session is None else session, self.project, destination)
        messages = [json.loads(line) for line in destination.read_text(encoding="utf-8").splitlines()
                    if line.strip()]
        return metadata, messages

    def assert_visible(self, messages, expected):
        self.assertEqual([(message["role"], message["text"]) for message in messages], expected)
        for message in messages:
            self.assertEqual(message["timestamp"], self.timestamp)
            self.assertIsInstance(message["source_line"], int)
            self.assertGreater(message["source_line"], 0)

    def test_claude_discovers_only_exact_project_top_level_sessions_and_memory(self):
        path = self.claude_file()
        self.claude_file(session_id=self.other_id,
                         directory=self.claude_dir() / self.session_id / "subagents")
        self.claude_file(session_id=self.third_id,
                         directory=self.claude_dir(self.root / "another-project"))
        self.write_jsonl(self.claude_dir() / "not-a-session.jsonl", self.claude_rows())
        memory = self.claude_dir() / "memory" / "MEMORY.md"
        memory.parent.mkdir(parents=True)
        memory.write_text("Fixture project memory only.\n", encoding="utf-8")
        result = self.discover("claude")
        self.assertEqual([session["id"] for session in result["sessions"]], [self.session_id])
        self.assertEqual(Path(result["sessions"][0]["path"]).resolve(), path.resolve())
        self.assertEqual({Path(item).resolve() for item in result["memory_files"]},
                         {memory.resolve()})

    def test_claude_omits_memory_when_colliding_directory_contains_another_cwd(self):
        self.project = self.root / "repo.one"
        self.project.mkdir()
        foreign = self.root / "repo-one"
        foreign.mkdir()
        self.assertNotEqual(self.project, foreign)
        self.assertEqual(self.claude_dir(), self.claude_dir(foreign))
        self.claude_file()
        rows = self.claude_rows()
        for row in rows:
            row.update(sessionId=self.other_id, cwd=str(foreign))
        self.claude_file(rows, session_id=self.other_id, directory=self.claude_dir(foreign))
        memory = self.claude_dir() / "memory" / "MEMORY.md"
        memory.parent.mkdir(parents=True)
        memory.write_text("Memory cannot be attributed to either checkout.\n", encoding="utf-8")
        before = memory.read_bytes()
        result = self.discover("claude")
        self.assertEqual([session["id"] for session in result["sessions"]], [self.session_id])
        self.assertEqual(result["memory_files"], [])
        self.assertTrue(any("auto-memory" in warning and "another cwd" in warning
                            for warning in result["warnings"]))
        self.assertEqual(memory.read_bytes(), before)

    def test_claude_omits_memory_without_a_verifiable_exact_main_session(self):
        self.claude_file([{"type": "progress", "data": {"text": "No project metadata"}}])
        memory = self.claude_dir() / "memory" / "MEMORY.md"
        memory.parent.mkdir(parents=True)
        memory.write_text("The directory name alone does not establish ownership.\n", encoding="utf-8")
        result = self.discover("claude")
        self.assertEqual(result["sessions"], [])
        self.assertEqual(result["memory_files"], [])
        self.assertTrue(any("auto-memory" in warning and "no native main session" in warning
                            for warning in result["warnings"]))

    def test_claude_exports_visible_text_without_internal_or_tool_content(self):
        rows = [self.claude_event("user", "可见问题", uuid="user-1"),
                self.claude_event("assistant", [
                    {"type": "thinking", "thinking": "PRIVATE REASONING"},
                    {"type": "text", "text": "可见回答"},
                    {"type": "tool_use", "id": "tool-1", "name": "Bash",
                     "input": {"command": "PRIVATE TOOL INPUT"}},
                ], uuid="answer-1", parent="user-1"),
                self.claude_event("user", [{"type": "tool_result", "tool_use_id": "tool-1",
                                            "content": "PRIVATE TOOL OUTPUT"}], uuid="tool-result"),
                self.claude_event("assistant", "PRIVATE SIDECHAIN", uuid="side", isSidechain=True),
                self.claude_event("user", "PRIVATE META", uuid="meta", isMeta=True),
                self.claude_event("assistant", "PRIVATE TEAM", uuid="team", teamName="workers"),
                {"type": "progress", "data": {"text": "PRIVATE PROGRESS"}},
                {"type": "attachment", "attachment": {"text": "PRIVATE ATTACHMENT"}},
                {"type": "system", "content": "PRIVATE SYSTEM"}]
        self.claude_file(rows)
        metadata, messages = self.export("claude")
        self.assert_visible(messages, [("user", "可见问题"), ("assistant", "可见回答")])
        self.assertEqual(metadata["message_count"], 2)

    def test_claude_preserves_physical_history_across_compaction_and_divergent_leaves(self):
        rows = self.claude_rows() + [
            {"type": "system", "subtype": "compact_boundary", "uuid": "boundary",
             "parentUuid": None, "logicalParentUuid": "answer-1", "sessionId": self.session_id},
            self.claude_event("user", "DERIVED COMPACTION SUMMARY", uuid="summary",
                              parent="boundary", isCompactSummary=True),
            self.claude_event("user", "压缩后的问题", uuid="user-2", parent="summary"),
            self.claude_event("assistant", "先前分支回答", uuid="answer-2", parent="user-2"),
            self.claude_event("assistant", "最后分支回答", uuid="answer-3", parent="user-2"),
        ]
        self.claude_file(rows)
        metadata, messages = self.export("claude")
        self.assert_visible(messages, [("user", "原始问题"), ("assistant", "可见回答"),
                                       ("user", "压缩后的问题"), ("assistant", "先前分支回答"),
                                       ("assistant", "最后分支回答")])
        self.assertTrue(metadata["warnings"], "omitting a derived compaction summary must be explicit")

    def test_claude_rejects_changed_session_or_project_inside_source(self):
        for field, foreign in (("sessionId", self.other_id),
                               ("cwd", str(self.root / "foreign-project"))):
            with self.subTest(field=field):
                path = self.claude_file()
                session = self.session("claude")
                rows = self.claude_rows()
                rows[-1][field] = foreign
                self.write_jsonl(path, rows)
                with self.assertRaises(ValueError):
                    self.export("claude", session=session, suffix=field)

    def test_codex_fallback_discovers_active_and_archived_main_sessions(self):
        self.codex_file()
        self.codex_file(session_id=self.other_id, directory=self.codex / "archived_sessions")
        self.codex_file(self.codex_rows(session_id=self.third_id, source="subagent"),
                        session_id=self.third_id)
        nested_id = "3456789a-bcde-4f01-8234-56789abcdef0"
        self.codex_file(self.codex_rows(session_id=nested_id), session_id=nested_id,
                        directory=self.codex / "sessions" / "subagents")
        foreign_id = "456789ab-cdef-4012-8345-6789abcdef01"
        self.codex_file(self.codex_rows(session_id=foreign_id, cwd=str(self.root / "foreign")),
                        session_id=foreign_id)
        result = self.discover("codex")
        self.assertEqual({session["id"] for session in result["sessions"]},
                         {self.session_id, self.other_id})

    def test_codex_sqlite_discovery_keeps_exact_project_and_main_threads(self):
        path = self.codex_file()
        subagent = self.codex_file(self.codex_rows(session_id=self.other_id, source="subagent"),
                                   session_id=self.other_id)
        foreign = self.codex_file(self.codex_rows(session_id=self.third_id,
                                                 cwd=str(self.root / "foreign")),
                                  session_id=self.third_id)
        database = self.codex / "state_5.sqlite"
        with closing(sqlite3.connect(database)) as connection:
            with connection:
                connection.execute("CREATE TABLE threads "
                                   "(id TEXT, rollout_path TEXT, cwd TEXT, source TEXT, archived INTEGER)")
                connection.executemany("INSERT INTO threads VALUES (?, ?, ?, ?, ?)", [
                    (self.session_id, str(path), str(self.project), "cli", 0),
                    (self.other_id, str(subagent), str(self.project), "subagent", 0),
                    (self.third_id, str(foreign), str(self.root / "foreign"), "cli", 1),
                ])
        before = database.read_bytes()
        result = self.discover("codex")
        self.assertEqual([session["id"] for session in result["sessions"]], [self.session_id])
        self.assertEqual(database.read_bytes(), before, "history discovery must not mutate native state")

    def test_codex_merges_indexed_and_unindexed_native_sessions_without_duplicates(self):
        indexed = self.codex_file()
        self.codex_file(session_id=self.other_id)
        self.codex_file(session_id=self.third_id, directory=self.codex / "archived_sessions")
        foreign_id = "3456789a-bcde-4f01-8234-56789abcdef0"
        foreign_project = self.root / "foreign-project"
        foreign = self.codex_file(
            self.codex_rows(session_id=foreign_id, cwd=str(foreign_project)),
            session_id=foreign_id)
        with closing(sqlite3.connect(self.codex / "state_5.sqlite")) as connection:
            with connection:
                connection.execute("CREATE TABLE threads "
                                   "(id TEXT, rollout_path TEXT, cwd TEXT, source TEXT, archived INTEGER)")
                connection.executemany("INSERT INTO threads VALUES (?, ?, ?, ?, ?)", [
                    (self.session_id, str(indexed), str(self.project), "cli", 0),
                    (foreign_id, str(foreign), str(foreign_project), "cli", 0),
                ])
        result = self.discover("codex")
        found = [session["id"] for session in result["sessions"]]
        self.assertCountEqual(found, [self.session_id, self.other_id, self.third_id])

    @unittest.skipUnless(os.name == "nt", "extended Windows path aliases are Windows-specific")
    def test_codex_normalizes_extended_windows_path_and_case_aliases(self):
        self.codex_file(self.codex_rows(cwd="\\\\?\\" + str(self.project)))
        self.codex_file(self.codex_rows(session_id=self.other_id,
                                       cwd=str(self.project).replace("\\", "/").upper() + "/"),
                        session_id=self.other_id)
        result = self.discover("codex")
        self.assertEqual({session["id"] for session in result["sessions"]},
                         {self.session_id, self.other_id})
        _, messages = self.export("codex")
        self.assert_visible(messages, [("user", "原始问题"), ("assistant", "可见回答")])

    def test_codex_exports_native_completed_user_and_agent_items(self):
        self.codex_file([self.codex_meta(), self.turn(),
                         self.completed("UserMessage", "用户问题", item_id="user-1"),
                         self.completed("AgentMessage", "进度说明", item_id="agent-1",
                                        phase="commentary", block_type="text"),
                         self.completed("AgentMessage", "最终回答", item_id="agent-2",
                                        phase="final_answer")])
        _, messages = self.export("codex")
        self.assert_visible(messages, [("user", "用户问题"), ("assistant", "进度说明"),
                                       ("assistant", "最终回答")])

    def test_codex_tool_only_completed_item_preserves_legacy_visible_messages(self):
        self.codex_file(self.codex_rows() + [{
            "type": "event_msg", "timestamp": self.timestamp,
            "payload": {"type": "item_completed", "thread_id": self.session_id,
                        "turn_id": "turn-1", "item": {
                            "type": "CommandExecution", "id": "tool-1",
                            "command": "Write-Output 'PRIVATE TOOL INPUT'",
                            "aggregated_output": "PRIVATE TOOL OUTPUT", "exit_code": 0,
                        }},
        }])
        _, messages = self.export("codex")
        self.assert_visible(messages, [("user", "原始问题"), ("assistant", "可见回答")])

    def test_codex_mixed_legacy_and_native_records_do_not_duplicate_messages(self):
        self.codex_file([
            self.codex_meta(), self.turn("legacy-turn"),
            self.response("user", "旧问题", item_id="legacy-user"),
            self.response("assistant", "旧回答", item_id="legacy-agent", phase="final_answer"),
            self.turn(), self.response("user", "PRIVATE INJECTED CONTEXT", item_id="injected-1"),
            self.response("user", "新问题", item_id="user-1"),
            self.completed("UserMessage", "新问题", item_id="user-1"),
            self.response("assistant", "新回答", item_id="agent-1", phase="final_answer"),
            self.completed("AgentMessage", "新回答", item_id="agent-1", phase="final_answer"),
        ])
        _, messages = self.export("codex")
        self.assert_visible(messages, [("user", "旧问题"), ("assistant", "旧回答"),
                                       ("user", "新问题"), ("assistant", "新回答")])

    def test_codex_legacy_fallback_filters_nonvisible_roles_and_analysis(self):
        self.codex_file([
            self.codex_meta(), self.turn(),
            self.response("system", "PRIVATE SYSTEM"),
            self.response("developer", "PRIVATE DEVELOPER"),
            self.response("user", "可见问题"),
            self.response("assistant", "PRIVATE ANALYSIS", channel="analysis"),
            self.response("assistant", "PRIVATE PHASE", phase="analysis"),
            {"type": "response_item", "payload": {"type": "reasoning", "text": "PRIVATE REASONING"}},
            {"type": "response_item", "payload": {"type": "function_call_output",
                                                       "output": "PRIVATE TOOL OUTPUT"}},
            self.response("tool", "PRIVATE TOOL MESSAGE"),
            self.response("assistant", "可见进度", phase="commentary"),
            self.response("assistant", "可见结果", phase="final_answer"),
        ])
        _, messages = self.export("codex")
        self.assert_visible(messages, [("user", "可见问题"), ("assistant", "可见进度"),
                                       ("assistant", "可见结果")])

    def test_codex_project_change_cannot_export_foreign_turn_text(self):
        self.codex_file(self.codex_rows() + [
            self.turn("foreign-turn", cwd=str(self.root / "foreign-project")),
            self.response("user", "FOREIGN USER TEXT"),
            self.completed("AgentMessage", "FOREIGN ASSISTANT TEXT", item_id="foreign-agent",
                           turn_id="foreign-turn", phase="final_answer"),
        ])
        session = self.session("codex")
        try:
            metadata, messages = self.export("codex", session=session)
        except ValueError:
            return  # Rejecting the entire mixed-project source is also valid.
        self.assert_visible(messages, [("user", "原始问题"), ("assistant", "可见回答")])
        self.assertTrue(metadata["warnings"])

    def test_partial_trailing_record_is_dropped_with_exact_prefix_evidence(self):
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                path = getattr(self, provider + "_file")()
                prefix = path.read_bytes()
                path.write_bytes(prefix + b'{"type":"unfinished')
                metadata, messages = self.export(provider)
                self.assert_visible(messages, [("user", "原始问题"), ("assistant", "可见回答")])
                self.assertTrue(metadata["warnings"])
                self.assertEqual(metadata["source_bytes"], path.stat().st_size)
                self.assertEqual(metadata["captured_bytes"], len(prefix))
                self.assertEqual(metadata["source_prefix_sha256"], hashlib.sha256(prefix).hexdigest())

    def test_malformed_complete_record_rejects_export(self):
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                path = getattr(self, provider + "_file")()
                session = self.session(provider)
                path.write_bytes(path.read_bytes() + b'{"malformed":}\n')
                with self.assertRaises(ValueError):
                    self.export(provider, session=session)

    def test_export_revalidates_untrusted_session_id_and_source_path(self):
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                path = getattr(self, provider + "_file")()
                session = self.session(provider)
                outside = self.root / "outside-native-root" / path.name
                outside.parent.mkdir(exist_ok=True)
                outside.write_bytes(path.read_bytes())
                for mutation in ({"id": self.other_id}, {"path": str(outside)}):
                    with self.subTest(mutation=next(iter(mutation))):
                        with self.assertRaises(ValueError):
                            self.export(provider, session={**session, **mutation})

    def test_export_records_exact_source_bytes_and_provenance(self):
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                path = getattr(self, provider + "_file")()
                original = path.read_bytes()
                metadata, messages = self.export(provider)
                self.assertEqual(metadata["session_id"], self.session_id)
                self.assertEqual(metadata["provider"], provider)
                self.assertEqual(Path(metadata["source_path"]).resolve(), path.resolve())
                self.assertEqual(Path(metadata["export_path"]).resolve(),
                                 (self.root / "exports" / f"{provider}.jsonl").resolve())
                self.assertEqual(metadata["source_bytes"], len(original))
                self.assertEqual(metadata["captured_bytes"], len(original))
                self.assertEqual(metadata["source_prefix_sha256"], hashlib.sha256(original).hexdigest())
                self.assertEqual(metadata["message_count"], len(messages))
                self.assertTrue(metadata["format"])
                self.assertEqual(path.read_bytes(), original, "export must leave the native source unchanged")


if __name__ == "__main__":
    unittest.main()
