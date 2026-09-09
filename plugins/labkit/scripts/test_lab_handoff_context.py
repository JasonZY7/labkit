"""Handoff integration with disposable native transcripts and scoped memories."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, call, patch

import lab_handoff
import lab_mem
from lab_fs import trash
from test_lab_handoff import context, digest, file_tree


class HandoffContextTests(unittest.TestCase):
    CLAUDE_ID = "01234567-89ab-4cde-8f01-23456789abcd"
    CODEX_ID = "12345678-9abc-4def-8012-3456789abcde"
    FOREIGN_ID = "23456789-abcd-4ef0-8123-456789abcdef"

    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="labkit-context-test-")).resolve()
        self.addCleanup(trash, self.temp, within=Path(tempfile.gettempdir()).resolve())
        self.project = self.temp / "project"
        self.project.mkdir()
        self.config_roots = {name: self.temp / (name + "-config") for name in ("claude", "codex")}
        for directory in self.config_roots.values():
            directory.mkdir()
        self.enterContext(patch.dict(os.environ, {
            "LABKIT_ROLE": "", "LABKIT_HOST": "", "CLAUDECODE": "", "CODEX_THREAD_ID": "",
        }))
        self.git("init", "--initial-branch=main")
        (self.project / "source.txt").write_text("baseline\n", encoding="utf-8")
        (self.project / "README.md").write_text("Project-local decision record.\n", encoding="utf-8")
        (self.project / ".labkit.json").write_text(
            json.dumps({"slug": "Fixture Context Scope"}), encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-m", "Isolated handoff context fixture")
        self.original = context()
        self.seed_native_sources()

    def git(self, *args):
        return subprocess.run(
            ["git", "--no-optional-locks", "-c", "user.name=Labkit fixture",
             "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
             "-c", "core.hooksPath=" + str(self.temp / "no-hooks"), *args],
            cwd=self.project, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout

    def write_jsonl(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n").encode("utf-8"))
        return path

    def claude_directory(self, project):
        key = re.sub(r"[^A-Za-z0-9]", "-", str(project))
        return self.config_roots["claude"] / "projects" / key

    def claude_row(self, role, content, message_id, **extra):
        return {"type": role, "uuid": message_id, "parentUuid": None,
                "sessionId": self.CLAUDE_ID, "cwd": str(self.project),
                "timestamp": "2026-09-09T12:00:00Z", "message": {"role": role, "content": content}, **extra}

    def seed_native_sources(self):
        directory = self.claude_directory(self.project)
        self.claude_history = self.write_jsonl(directory / (self.CLAUDE_ID + ".jsonl"), [
            self.claude_row("user", "用户原始问题：保留所有决定。", "user-1"),
            self.claude_row("assistant", [
                {"type": "thinking", "thinking": "PRIVATE_REASONING_SENTINEL"},
                {"type": "text", "text": "可见回答：保留原始 source references。"},
                {"type": "tool_use", "name": "Bash", "input": {"command": "PRIVATE_TOOL_INPUT_SENTINEL"}},
            ], "assistant-1"),
            self.claude_row("user", [{"type": "tool_result", "content": "PRIVATE_TOOL_OUTPUT_SENTINEL"}], "tool-1"),
            self.claude_row("assistant", "PRIVATE_SIDECHAIN_SENTINEL", "side-1", isSidechain=True),
            {"type": "system", "content": "PRIVATE_SYSTEM_SENTINEL"},
        ])
        with self.claude_history.open("ab") as stream:
            stream.write(b'{"type":"user","message":')
        self.auto_memory = directory / "memory" / "MEMORY.md"
        self.auto_memory.parent.mkdir()
        self.auto_memory.write_text("Original project auto-memory decision.\n", encoding="utf-8")
        foreign = self.temp / "other-project"
        foreign_row = self.claude_row("user", "FOREIGN_PROJECT_HISTORY_SENTINEL", "foreign-1")
        foreign_row.update(sessionId=self.FOREIGN_ID, cwd=str(foreign))
        self.write_jsonl(self.claude_directory(foreign) / (self.FOREIGN_ID + ".jsonl"), [foreign_row])
        foreign_memory = self.claude_directory(foreign) / "memory" / "MEMORY.md"
        foreign_memory.parent.mkdir()
        foreign_memory.write_text("FOREIGN_PROJECT_MEMORY_SENTINEL", encoding="utf-8")
        codex = self.config_roots["codex"]
        self.codex_history = self.write_jsonl(
            codex / "sessions" / "2026" / "09" / "09" / ("rollout-2026-09-09T12-00-00-" + self.CODEX_ID + ".jsonl"), [
                {"type": "session_meta", "payload": {"id": self.CODEX_ID, "cwd": str(self.project), "source": "cli"}},
                {"type": "turn_context", "payload": {"turn_id": "turn-codex", "cwd": str(self.project)}},
                {"type": "event_msg", "payload": {"type": "item_completed", "thread_id": self.CODEX_ID,
                    "turn_id": "turn-codex", "item": {"type": "UserMessage", "id": "codex-user",
                        "content": [{"type": "text", "text": "继续处理更新后的计划。"}]}}},
                {"type": "event_msg", "payload": {"type": "item_completed", "thread_id": self.CODEX_ID,
                    "turn_id": "turn-codex", "item": {"type": "AgentMessage", "id": "codex-agent", "phase": "final_answer",
                        "content": [{"type": "Text", "text": "已验证新的计划。"}]}}},
            ])
        global_memory = codex / "memories" / "MEMORY.md"
        global_memory.parent.mkdir()
        global_memory.write_text("GLOBAL_ACCOUNT_MEMORY_SENTINEL", encoding="utf-8")

    def native_tree(self):
        return {provider: file_tree(root) for provider, root in self.config_roots.items()}

    def prepare(self, source="claude", target="codex", current=None, include_memory=False):
        return Path(lab_handoff.prepare(
            self.project, current or self.original, source, target, source_quiescent=True,
            config_roots=self.config_roots, include_memory=include_memory))

    def receipt(self, packet, current=None):
        current = current or self.original
        return {"objective": current["objective"], "next_step": current["next_steps"][0],
                "context_sha256": digest(packet / "context.json")}

    def accept(self, packet, target="codex", current=None):
        return lab_handoff.accept(packet, self.project, target, self.receipt(packet, current))

    def messages(self, exported):
        return [json.loads(line) for line in Path(exported["export_path"]).read_text(encoding="utf-8").splitlines()]

    def test_native_history_and_scoped_auto_memory_are_bound_and_accepted_read_only(self):
        native_before = self.native_tree()
        packet = self.prepare()
        manifest = lab_handoff.load_packet(packet, self.project)
        self.assertEqual([item["session_id"] for item in manifest["history"]], [self.CLAUDE_ID])
        exported = manifest["history"][0]
        self.assertEqual([(row["role"], row["text"]) for row in self.messages(exported)], [
            ("user", "用户原始问题：保留所有决定。"),
            ("assistant", "可见回答：保留原始 source references。"),
        ])
        self.assertEqual(exported["message_count"], 2)
        prefix = self.claude_history.read_bytes()[:exported["captured_bytes"]]
        self.assertEqual(hashlib.sha256(prefix).hexdigest(), exported["source_prefix_sha256"])
        self.assertLess(exported["captured_bytes"], exported["source_bytes"])
        self.assertTrue(any("trailing" in gap for gap in manifest["coverage_gaps"]))
        exported_relative = Path(exported["export_path"]).relative_to(packet).as_posix()
        self.assertEqual(manifest["packet_files"][exported_relative], digest(packet / exported_relative))
        memory = manifest["memory"]["claude_auto_memory"]
        self.assertEqual(len(memory), 1)
        self.assertEqual(Path(memory[0]["source"]), self.auto_memory)
        self.assertEqual(memory[0]["source_sha256"], digest(self.auto_memory))
        self.assertEqual((packet / memory[0]["snapshot"]).read_text(encoding="utf-8"), self.auto_memory.read_text(encoding="utf-8"))
        self.assertEqual(manifest["packet_files"][memory[0]["snapshot"]], digest(packet / memory[0]["snapshot"]))
        contents = "\n".join(path.read_text(encoding="utf-8") for path in packet.rglob("*") if path.is_file())
        for marker in ("PRIVATE_REASONING_SENTINEL", "PRIVATE_TOOL_INPUT_SENTINEL", "PRIVATE_TOOL_OUTPUT_SENTINEL",
                       "PRIVATE_SYSTEM_SENTINEL", "PRIVATE_SIDECHAIN_SENTINEL", "FOREIGN_PROJECT_HISTORY_SENTINEL",
                       "FOREIGN_PROJECT_MEMORY_SENTINEL", "GLOBAL_ACCOUNT_MEMORY_SENTINEL"):
            self.assertNotIn(marker, contents)
        self.assertEqual(self.accept(packet)["owner"], "codex")
        self.assertEqual(self.native_tree(), native_before)

    def test_mem0_reads_only_configured_namespace_and_reports_unavailable_capture(self):
        native_before = self.native_tree()
        expected_namespace = "proj-fixture-context-scope"
        rows = [{"id": "memory-1", "memory": "A project-only cloud decision.", "user_id": expected_namespace}]
        cloud = Mock(spec=["get_all", "add", "delete"])
        cloud.get_all.return_value = {"results": rows}
        with patch("lab_mem.resolve_api_key", return_value="fixture-credential"), \
                patch("lab_mem.client", return_value=cloud), \
                patch("lab_mem.namespace", wraps=lab_mem.namespace) as namespace:
            packet = self.prepare(include_memory=True)
        namespace.assert_called_once_with("Fixture Context Scope")
        self.assertEqual(cloud.mock_calls, [call.get_all(filters={"user_id": expected_namespace})])
        manifest = lab_handoff.load_packet(packet, self.project)
        memory = manifest["memory"]["mem0"]
        self.assertEqual((memory["namespace"], memory["status"], memory["rows"]), (expected_namespace, "captured", 1))
        snapshot = lab_handoff.read_json(packet / memory["snapshot"])
        self.assertEqual(snapshot["namespace"], expected_namespace)
        self.assertEqual(snapshot["rows"], rows)
        self.assertEqual(manifest["packet_files"][memory["snapshot"]], digest(packet / memory["snapshot"]))
        self.accept(packet)
        unavailable = Mock(spec=["get_all", "add", "delete"])
        unavailable.get_all.side_effect = RuntimeError("PRIVATE_CLOUD_ERROR_SENTINEL")
        with patch("lab_mem.resolve_api_key", return_value="fixture-credential"), \
                patch("lab_mem.client", return_value=unavailable):
            next_packet = self.prepare("codex", "claude", include_memory=True)
        self.assertEqual(unavailable.mock_calls, [call.get_all(filters={"user_id": expected_namespace})])
        next_manifest = lab_handoff.load_packet(next_packet, self.project)
        missing = next_manifest["memory"]["mem0"]
        self.assertEqual(missing["status"], "unavailable: RuntimeError")
        self.assertNotIn("snapshot", missing)
        self.assertFalse((next_packet / "memory" / "mem0.json").exists())
        self.assertTrue(any("mem0 could not be captured" in gap for gap in next_manifest["coverage_gaps"]))
        self.assertNotIn("PRIVATE_CLOUD_ERROR_SENTINEL", (next_packet / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(self.accept(next_packet, target="claude")["owner"], "claude")
        self.assertEqual(self.native_tree(), native_before)

    def test_roundtrip_links_immutable_prior_evidence_and_requires_latest_context(self):
        first = self.prepare()
        first_manifest = lab_handoff.load_packet(first, self.project)
        self.assertIsNone(first_manifest["previous_handoff_id"])
        self.accept(first)
        first_before = file_tree(first)
        first_memory = first / first_manifest["memory"]["claude_auto_memory"][0]["snapshot"]
        original_memory = first_memory.read_bytes()
        self.auto_memory.write_text("LATEST auto-memory decision after accepted work.\n", encoding="utf-8")
        latest = copy.deepcopy(self.original)
        latest["current_state"] = "LATEST state: export verified and next implementation ready."
        latest["decisions"] = ["LATEST decision: use the verified second revision."]
        latest["next_steps"] = ["Implement the verified second revision."]
        native_before = self.native_tree()
        second = self.prepare("codex", "claude", current=latest)
        second_manifest = lab_handoff.load_packet(second, self.project)
        self.assertEqual(second_manifest["previous_handoff_id"], first_manifest["id"])
        previous = second.parent / second_manifest["previous_handoff_id"]
        preserved = lab_handoff.load_packet(previous, self.project)
        self.assertEqual(self.messages(preserved["history"][0])[0]["text"], "用户原始问题：保留所有决定。")
        self.assertEqual(first_memory.read_bytes(), original_memory)
        latest_memory = second / second_manifest["memory"]["claude_auto_memory"][0]["snapshot"]
        self.assertIn("LATEST auto-memory", latest_memory.read_text(encoding="utf-8"))
        self.assertEqual(lab_handoff.read_json(second / "context.json"), latest)
        self.assertIn(latest["decisions"][0], (second / "BRIEFING.md").read_text(encoding="utf-8"))
        self.assertNotIn(self.original["decisions"][0], (second / "BRIEFING.md").read_text(encoding="utf-8"))
        with self.assertRaises(ValueError):
            lab_handoff.accept(second, self.project, "claude", self.receipt(first))
        self.assertEqual(lab_handoff.status(self.project)["status"], "ready")
        self.assertEqual(self.accept(second, target="claude", current=latest)["owner"], "claude")
        self.assertEqual(lab_handoff.status(self.project)["handoff_id"], second_manifest["id"])
        self.assertEqual(file_tree(first), first_before)
        self.assertEqual(self.native_tree(), native_before)

    def test_codex_source_keeps_claude_memory_scope_warning(self):
        discover = lab_handoff.lab_history.discover
        warning = "Claude auto-memory skipped: fixture has ambiguous project identity."

        def scoped(provider, project, config_root=None):
            result = discover(provider, project, config_root=config_root)
            if provider == "claude":
                result["memory_files"] = []
                result["warnings"].append(warning)
            return result

        with patch("lab_handoff.lab_history.discover", side_effect=scoped):
            packet = self.prepare("codex", "claude")
        manifest = lab_handoff.load_packet(packet, self.project)
        self.assertIn(warning, manifest["coverage_gaps"])
        self.assertEqual(manifest["memory"]["claude_auto_memory"], [])


if __name__ == "__main__":
    unittest.main()
