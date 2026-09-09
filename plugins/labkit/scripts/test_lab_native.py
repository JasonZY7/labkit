"""Offline checks for evidence from real native Claude subagent transcripts."""
import hashlib
import json
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import lab_native
from lab_fs import trash


class NativeTranscriptTests(unittest.TestCase):
    def setUp(self):
        parent = Path(tempfile.gettempdir()).resolve()
        self.root = Path(tempfile.mkdtemp(prefix="labkit-native-tests-", dir=parent)).resolve()
        self.addCleanup(trash, self.root, within=parent)
        self.config = self.root / "claude"
        self.session_id = "01234567-89ab-4cde-8f01-23456789abcd"
        self.agent_id = "a1234567890abcdef"
        self.subagents = self.config / "projects" / "test-project" / self.session_id / "subagents"
        self.pending = {
            "member": {"provider": "claude", "model": "claude-opus-5", "effort": "max"},
            "role": "brain", "token": "unique-nonce", "artifact": "001-brain",
            "started_at": "2026-09-09T00:00:00Z",
        }
        self.report = "结论：通过。"

    def event(self, role, content, **message_fields):
        return {
            "type": role, "sessionId": self.session_id, "agentId": self.agent_id,
            "timestamp": "2026-09-09T00:01:00Z",
            "message": {"role": role, "content": content, **message_fields},
        }

    def events(self):
        return [
            self.event("user", "Review this task. Token: " + self.pending["token"]),
            self.event("assistant", [{"type": "text", "text": "Earlier draft."}],
                       model="claude-opus-5"),
            self.event("assistant", [
                {"type": "thinking", "thinking": "Not part of the report."},
                {"type": "text", "text": "结论："},
                {"type": "text", "text": "通过。"},
            ], model="claude-opus-5"),
        ]

    def completion_events(self, report=None):
        return [
            {"type": "started", "key": "v2:abc", "agentId": self.agent_id},
            {"type": "result", "key": "v2:abc", "agentId": self.agent_id,
             "result": self.report if report is None else report},
        ]

    def write_transcript(self, events=None, *, path=None, journal=None, metadata=None,
                         report=None):
        path = path or self.subagents / f"agent-{self.agent_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(event, ensure_ascii=False)
                                  for event in (self.events() if events is None else events)) + "\n",
                        encoding="utf-8")
        if metadata is None:
            metadata = {"agentType": "general-purpose" if self.pending["role"] == "worker" else "Plan",
                        "spawnDepth": 1, "model": "claude-opus-5"}
        path.with_suffix(".meta.json").write_text(json.dumps(metadata), encoding="utf-8")
        (path.parent / "journal.jsonl").write_text(
            "\n".join(json.dumps(event, ensure_ascii=False)
                      for event in (self.completion_events(report) if journal is None else journal)) + "\n",
            encoding="utf-8")
        return path

    def verify(self, path, report=None):
        return lab_native.verify(self.pending, self.report if report is None else report,
                                 path, config_root=self.config)

    def test_accepts_native_and_workflow_transcripts_with_exact_evidence(self):
        for workflow in (False, True):
            with self.subTest(workflow=workflow):
                directory = self.subagents / "workflows" / "run-1" if workflow else self.subagents
                events = self.events()
                if workflow:
                    events[0]["message"]["content"] = [
                        {"type": "text", "text": "Task token: " + self.pending["token"]}]
                path = self.write_transcript(events, path=directory / f"agent-{self.agent_id}.jsonl")
                result = self.verify(path, "\n  " + self.report + "  \n")
                self.assertEqual(result["text"], self.report)
                self.assertEqual(result["actual_models"], ["claude-opus-5"])
                self.assertIs(result["identity_verified"], True)
                self.assertIs(result["effort_verified"], False)
                self.assertEqual(result["identity_source"], "native-transcript")
                expected_evidence = {
                    "path": str(path.resolve()),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "session_id": self.session_id,
                    "agent_id": self.agent_id,
                }
                self.assertEqual({key: result["transcript"][key] for key in expected_evidence},
                                 expected_evidence)

    def test_accepts_completed_worker_without_claiming_effort_was_verified(self):
        self.pending["role"] = "worker"
        journal = self.completion_events()
        journal.append({"type": "started", "key": "v2:other", "agentId": "another-agent"})
        path = self.write_transcript(journal=journal, metadata={
            "agentType": "general-purpose", "model": "claude-opus-5", "spawnDepth": 2})
        result = self.verify(path)
        self.assertEqual(result["text"], self.report)
        self.assertIs(result["identity_verified"], True)
        self.assertIs(result["effort_verified"], False)

    def test_unfinished_or_restarted_agent_cannot_submit_its_existing_text(self):
        completed = self.completion_events()
        cases = {
            "still running": completed[:1],
            "result without start": completed[1:],
            "restarted after result": completed + [completed[0]],
        }
        for name, journal in cases.items():
            with self.subTest(state=name), self.assertRaises(ValueError):
                self.verify(self.write_transcript(journal=journal))

    def test_other_agent_or_other_key_cannot_prove_completion(self):
        for field, value in (("agentId", "another-agent"), ("key", "v2:other")):
            with self.subTest(field=field):
                journal = self.completion_events()
                journal[-1][field] = value
                with self.assertRaises(ValueError):
                    self.verify(self.write_transcript(journal=journal))

    def test_completion_result_must_match_the_submitted_report(self):
        with self.assertRaises(ValueError):
            self.verify(self.write_transcript(report="A different result."))

    def test_metadata_must_match_the_assigned_role_and_model(self):
        cases = (
            ("brain", "general-purpose", "claude-opus-5"),
            ("worker", "Plan", "claude-opus-5"),
            ("brain", "Plan", "claude-sonnet-5"),
        )
        for role, agent_type, model in cases:
            with self.subTest(role=role, agent_type=agent_type, model=model):
                self.pending["role"] = role
                metadata = {"agentType": agent_type, "model": model, "spawnDepth": 1}
                with self.assertRaises(ValueError):
                    self.verify(self.write_transcript(metadata=metadata))

    def test_rejects_symlink_or_junction_evidence_paths(self):
        path = self.write_transcript()
        original_lstat = Path.lstat
        for target in (path, path.with_suffix(".meta.json"), path.parent / "journal.jsonl"):
            for symlink in (False, True):
                with self.subTest(target=target.name, symlink=symlink):
                    def redirected_info(candidate, *args, **kwargs):
                        info = original_lstat(candidate, *args, **kwargs)
                        if candidate == target:
                            return SimpleNamespace(
                                st_mode=stat.S_IFLNK if symlink else info.st_mode,
                                st_file_attributes=0x0400)
                        return info

                    with patch.object(Path, "lstat", autospec=True, side_effect=redirected_info):
                        with self.assertRaises((ValueError, OSError)):
                            self.verify(path)

    def test_rejects_missing_nonce(self):
        events = self.events()
        events[0]["message"]["content"] = "An unrelated task."
        with self.assertRaises(ValueError):
            self.verify(self.write_transcript(events))

    def test_unicode_line_separators_inside_json_strings_are_not_records(self):
        events = self.events()
        events[1]["message"]["content"] = [
            {"type": "text", "text": "Tool output uses \u2028 and \u2029 inside a JSON string."}]
        self.assertEqual(self.verify(self.write_transcript(events))["text"], self.report)

    def test_assistant_cannot_supply_the_user_nonce(self):
        events = self.events()
        events[0]["message"]["content"] = "An unrelated task."
        events[-1]["message"]["content"] = [{"type": "text", "text": self.pending["token"]}]
        with self.assertRaises(ValueError):
            self.verify(self.write_transcript(events, report=self.pending["token"]),
                        self.pending["token"])

    def test_rejects_no_assistant_response(self):
        with self.assertRaises(ValueError):
            self.verify(self.write_transcript(self.events()[:1]))

    def test_rejects_wrong_or_missing_served_model(self):
        for index, model in ((-1, "claude-sonnet-5"), (1, "claude-sonnet-5"), (-1, None)):
            with self.subTest(index=index, model=model):
                events = self.events()
                if model is None:
                    del events[index]["message"]["model"]
                else:
                    events[index]["message"]["model"] = model
                with self.assertRaises(ValueError):
                    self.verify(self.write_transcript(events))

    def test_report_must_match_the_last_assistant_text(self):
        for report in ("A fabricated report.", "Earlier draft."):
            with self.subTest(report=report), self.assertRaises(ValueError):
                self.verify(self.write_transcript(report=report), report)

    def test_rejects_transcript_outside_the_config_root(self):
        path = self.root / "outside" / "projects" / "test-project" / self.session_id \
            / "subagents" / f"agent-{self.agent_id}.jsonl"
        with self.assertRaises(ValueError):
            self.verify(self.write_transcript(path=path))

    def test_rejects_files_that_are_not_native_agent_jsonl(self):
        paths = (
            self.subagents / "report.jsonl",
            self.subagents / f"agent-{self.agent_id}.txt",
            self.config / "projects" / "test-project" / self.session_id / "report"
            / f"agent-{self.agent_id}.jsonl",
        )
        for path in paths:
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.verify(self.write_transcript(path=path))

    def test_rejects_malformed_json_even_after_valid_evidence(self):
        path = self.write_transcript()
        with path.open("a", encoding="utf-8") as transcript:
            transcript.write('{"type": "assistant", broken}\n')
        with self.assertRaises(ValueError):
            self.verify(path)


if __name__ == "__main__":
    unittest.main()
