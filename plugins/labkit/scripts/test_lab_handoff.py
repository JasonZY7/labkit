"""Offline handoff behavior against real, isolated Git checkouts."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import lab_handoff
import lab_run_legacy
from lab_fs import trash


def context():
    return {
        "objective": "让第二个 host 接续实现，保留最初的约束。",
        "current_state": "The parser works; the export path still needs verification.",
        "constraints": ["Preserve the original command interface."],
        "decisions": ["Use a portable packet with explicit acceptance."],
        "rejected_approaches": ["Do not synthesize native conversation history."],
        "ideas": ["Consider a small export regression fixture."],
        "open_questions": ["Does the parser preserve Unicode text?"],
        "next_steps": ["Check the export output against the saved fixture."],
        "verification": ["Parser unit tests passed; export has not been tested."],
        "important_files": ["source.txt"],
    }


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def file_tree(directory):
    return {
        path.relative_to(directory).as_posix(): digest(path)
        for path in Path(directory).rglob("*") if path.is_file()
    }


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="labkit-handoff-test-")).resolve()
        self.addCleanup(trash, self.temp, within=Path(tempfile.gettempdir()).resolve())
        self.project = self.temp / "project"
        self.project.mkdir()
        self.config_roots = {provider: self.temp / (provider + "-config")
                             for provider in ("claude", "codex")}
        for root in self.config_roots.values():
            root.mkdir()
        self.enterContext(patch.dict(os.environ, {
            "LABKIT_HOST": "", "CLAUDECODE": "", "CODEX_THREAD_ID": "",
            "LABKIT_ROLE": "",
        }))
        self.git("init", "--initial-branch=main")
        (self.project / "source.txt").write_text("baseline\n", encoding="utf-8")
        self.git("add", "source.txt")
        self.git("commit", "-m", "Fixture baseline")
        self.original = context()

    def git(self, *args):
        return subprocess.run(
            ["git", "--no-optional-locks", "-c", "user.name=Labkit fixture",
             "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
             "-c", "core.hooksPath=" + str(self.temp / "no-hooks"), *args],
            cwd=self.project, check=True, text=True, encoding="utf-8",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.strip()

    def prepare(self, *, source="claude", target="codex", **kwargs):
        options = {"source_quiescent": True, "config_roots": self.config_roots}
        options.update(kwargs)
        return Path(lab_handoff.prepare(self.project, self.original, source, target, **options))

    def receipt(self, packet):
        return {"objective": self.original["objective"],
                "next_step": self.original["next_steps"][0],
                "context_sha256": digest(packet / "context.json")}

    def accept(self, packet, **kwargs):
        options = {"project": self.project, "target": "codex", "receipt": self.receipt(packet)}
        options.update(kwargs)
        return lab_handoff.accept(packet, **options)

    def state_bytes(self, project=None):
        path = (project or self.project) / ".labkit-local" / "transfer-state.json"
        return path.read_bytes() if path.exists() else None

    def assert_rejected_unchanged(self, operation, *args, **kwargs):
        before = self.state_bytes()
        with self.assertRaises((ValueError, RuntimeError)):
            operation(*args, **kwargs)
        self.assertEqual(self.state_bytes(), before, "rejection must preserve transfer state")

    def assert_packet_preserved(self, packet, before):
        self.assertEqual({relative: digest(packet / relative) for relative in before}, before)

    def test_projects_without_handoff_have_no_owner_requirement(self):
        self.assertIsNone(lab_handoff.status(self.project))
        lab_handoff.check_owner(self.project)
        lab_handoff.check_owner(self.project, host="claude")
        lab_handoff.check_owner(self.project, host="codex")
        self.assertIsNone(self.state_bytes())

    def test_prepare_requires_explicit_source_quiescence(self):
        self.assert_rejected_unchanged(
            lab_handoff.prepare, self.project, self.original, "claude", "codex",
            config_roots=self.config_roots)
        self.assert_rejected_unchanged(self.prepare, source_quiescent=False)
        self.assertIsNone(lab_handoff.status(self.project))

    def test_invalid_context_and_provider_pairs_never_start_a_transfer(self):
        for field, value in [
            ("objective", ""), ("current_state", 12), ("constraints", "not a list"),
            ("decisions", [False]), ("next_steps", []), ("ideas", [""]),
            ("important_files", ["../outside.txt"]),
            ("important_files", [str(self.temp / "outside.txt")]),
            ("unexpected", "extra context is not part of the schema"),
        ]:
            candidate = copy.deepcopy(self.original)
            candidate[field] = value
            with self.subTest(field=field, value=value):
                self.assert_rejected_unchanged(
                    lab_handoff.prepare, self.project, candidate, "claude", "codex",
                    source_quiescent=True, config_roots=self.config_roots)
        for source, target in [("claude", "claude"), ("codex", "codex"),
                               ("unknown", "codex"), ("claude", "unknown")]:
            with self.subTest(source=source, target=target):
                self.assert_rejected_unchanged(self.prepare, source=source, target=target)

    def test_packet_preserves_context_privately_without_git_or_native_history_writes(self):
        native_files = []
        for provider, root in self.config_roots.items():
            path = root / "untouched-history.txt"
            path.write_text(provider + " native history is private\n", encoding="utf-8")
            native_files.append(path)
        native_before = {path: digest(path) for path in native_files}
        git_before = file_tree(self.project / ".git")
        packet = self.prepare()
        self.assertTrue(packet.is_relative_to(self.project / ".labkit-local"))
        self.assertEqual(json.loads((packet / "context.json").read_text(encoding="utf-8")),
                         self.original)
        briefing = (packet / "BRIEFING.md").read_text(encoding="utf-8")
        for value in self.original.values():
            for text in value if isinstance(value, list) else [value]:
                self.assertIn(text, briefing)
        self.assertTrue((packet / "manifest.json").is_file())
        self.assertEqual((self.project / ".labkit-local" / ".gitignore").read_text().strip(), "*")
        self.assertNotIn(".labkit-local", self.git("status", "--porcelain", "--untracked-files=all"))
        self.assertEqual(file_tree(self.project / ".git"), git_before)
        self.assertEqual({path: digest(path) for path in native_files}, native_before)
        self.assertEqual(lab_handoff.status(self.project)["status"], "ready")

    def test_prepare_respects_an_existing_os_project_lock(self):
        with lab_run_legacy.project_lock(self.project):
            self.assert_rejected_unchanged(self.prepare)
        self.assertIsNone(lab_handoff.status(self.project))

    def test_unfinished_controller_work_must_quiesce_before_prepare(self):
        run_dir = self.project / "handoffs" / "runs" / "fixture"
        run_dir.mkdir(parents=True)
        for run_status, pending in [
            ("running", None), ("awaiting_worker", {"role": "worker"}),
            ("awaiting_host", {"role": "planner"}), ("review_ready", None),
            ("interrupted", {"role": "worker", "mode": "host"}),
            ("paused", {"role": "worker", "mode": "host"}),
            ("paused", {"role": "worker", "mode": "cli"}),
            ("blocked", {"role": "worker", "mode": "cli"}),
        ]:
            state = {"format": 2, "project": str(self.project), "history": [],
                     "status": run_status, "pending": pending}
            (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
            with self.subTest(status=run_status, pending=pending):
                self.assert_rejected_unchanged(self.prepare)
        state.update(status="interrupted", pending={"role": "worker", "mode": "cli"})
        (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
        interrupted_before = (run_dir / "state.json").read_bytes()
        self.assertTrue(self.prepare().is_dir())
        self.assertEqual((run_dir / "state.json").read_bytes(), interrupted_before,
                         "a stopped CLI run must be preserved for safe target-host resume")

    def test_ready_transfer_blocks_both_hosts_and_controller_lock(self):
        self.prepare()
        for host in (None, "claude", "codex"):
            with self.subTest(host=host):
                self.assert_rejected_unchanged(lab_handoff.check_owner, self.project, host=host)
        with self.assertRaises((ValueError, RuntimeError)):
            with lab_run_legacy.project_lock(self.project):
                self.fail("ready handoff must block a controller from starting")

    def test_nested_checkout_directory_cannot_bypass_owner_or_create_a_separate_lock(self):
        nested = self.project / "component"
        nested.mkdir()
        with self.assertRaisesRegex(ValueError, "checkout root"):
            with lab_run_legacy.project_lock(nested):
                self.fail("nested project must not create its own lock or reservation")
        self.assertFalse((nested / ".labkit-run.lock").exists())
        packet = self.prepare()
        with self.assertRaisesRegex(RuntimeError, "awaits acceptance"):
            lab_handoff.check_owner(nested, host="claude")
        self.accept(packet)
        with self.assertRaisesRegex(RuntimeError, "owned by codex"):
            lab_handoff.check_owner(nested, host="claude")
        lab_handoff.check_owner(nested, host="codex")

    def test_accept_changes_owner_and_is_idempotent_without_mutating_packet(self):
        packet = self.prepare()
        packet_before = file_tree(packet)
        accepted = self.accept(packet)
        self.assertEqual((accepted["status"], accepted["owner"]), ("active", "codex"))
        self.assertEqual(lab_handoff.status(self.project), accepted)
        lab_handoff.check_owner(self.project, host="codex")
        self.assert_rejected_unchanged(lab_handoff.check_owner, self.project, host="claude")
        state_before = self.state_bytes()
        accepted_files = file_tree(packet)
        self.assertEqual(self.accept(packet), accepted)
        self.assertEqual(self.state_bytes(), state_before)
        self.assertEqual(file_tree(packet), accepted_files)
        self.assert_packet_preserved(packet, packet_before)

    def test_wrong_target_or_receipt_preserves_the_ready_transfer(self):
        packet = self.prepare()
        self.assert_rejected_unchanged(self.accept, packet, target="claude")
        for field, value in [("objective", "A different task"), ("next_step", ""),
                             ("context_sha256", "0" * 64)]:
            receipt = self.receipt(packet)
            receipt[field] = value
            with self.subTest(field=field):
                self.assert_rejected_unchanged(self.accept, packet, receipt=receipt)
        missing = self.receipt(packet)
        del missing["objective"]
        self.assert_rejected_unchanged(self.accept, packet, receipt=missing)
        self.assertEqual(self.accept(packet)["owner"], "codex")

    def test_tracked_content_change_or_deletion_invalidates_acceptance(self):
        path = self.project / "source.txt"
        original = path.read_bytes()
        packet = self.prepare()
        path.write_text("changed after handoff\n", encoding="utf-8")
        self.assert_rejected_unchanged(self.accept, packet)
        trash(path, within=self.project)
        self.assert_rejected_unchanged(self.accept, packet)
        path.write_bytes(original)
        self.assertEqual(self.accept(packet)["owner"], "codex")

    def test_existing_and_new_untracked_content_is_part_of_the_checkout_snapshot(self):
        path = self.project / "draft.txt"
        path.write_text("initial draft\n", encoding="utf-8")
        packet = self.prepare()
        path.write_text("mutated draft\n", encoding="utf-8")
        self.assert_rejected_unchanged(self.accept, packet)
        path.write_text("initial draft\n", encoding="utf-8")
        newcomer = self.project / "new-file.txt"
        newcomer.write_text("created after handoff\n", encoding="utf-8")
        self.assert_rejected_unchanged(self.accept, packet)
        trash(newcomer, within=self.project)
        self.assertEqual(self.accept(packet)["owner"], "codex")

    def test_branch_or_head_changes_invalidate_even_an_unchanged_working_tree(self):
        packet = self.prepare()
        self.git("switch", "-c", "fixture-other")
        self.assert_rejected_unchanged(self.accept, packet)
        self.git("switch", "main")
        self.git("commit", "--allow-empty", "-m", "Moved HEAD without editing files")
        self.assert_rejected_unchanged(self.accept, packet)

    def test_a_copied_checkout_cannot_accept_the_original_packet(self):
        packet = self.prepare()
        copied = self.temp / "copied-project"
        shutil.copytree(self.project, copied)
        copied_packet = copied / packet.relative_to(self.project)
        copied_state = self.state_bytes(copied)
        self.assert_rejected_unchanged(self.accept, copied_packet, project=copied)
        self.assertEqual(self.state_bytes(copied), copied_state)
        self.assert_rejected_unchanged(self.accept, packet, project=copied)
        self.assertEqual(self.state_bytes(copied), copied_state)

    def test_packet_context_tampering_fails_even_with_a_matching_new_receipt_hash(self):
        packet = self.prepare()
        candidate = copy.deepcopy(self.original)
        candidate["decisions"] = ["Changed after the immutable packet was prepared."]
        (packet / "context.json").write_text(json.dumps(candidate), encoding="utf-8")
        self.assert_rejected_unchanged(self.accept, packet)

    def test_ignored_important_and_project_memory_files_are_still_checked(self):
        (self.project / ".gitignore").write_text("ignored-important.txt\nREADME.md\n", encoding="utf-8")
        important = self.project / "ignored-important.txt"
        important.write_text("original local decision\n", encoding="utf-8")
        memory = self.project / "README.md"
        memory.write_text("original project context\n", encoding="utf-8")
        self.original["important_files"] = ["ignored-important.txt"]
        packet = self.prepare()
        important.write_text("changed after prepare\n", encoding="utf-8")
        self.assert_rejected_unchanged(self.accept, packet)
        important.write_text("original local decision\n", encoding="utf-8")
        memory.write_text("changed project context after prepare\n", encoding="utf-8")
        self.assert_rejected_unchanged(self.accept, packet)

    def test_ignored_run_state_cannot_change_its_goal_during_a_handoff(self):
        (self.project / ".gitignore").write_text("handoffs/runs/\n", encoding="utf-8")
        run_dir = self.project / "handoffs" / "runs" / "ignored-fixture"
        run_dir.mkdir(parents=True)
        state = {"format": 2, "project": str(self.project), "history": [],
                 "status": "paused", "pending": None,
                 "goal": {"objective": "Continue the original run goal"}}
        state_file = run_dir / "state.json"
        state_file.write_text(json.dumps(state), encoding="utf-8")
        packet = self.prepare()
        state["goal"]["objective"] = "A substituted goal after preparation"
        state_file.write_text(json.dumps(state), encoding="utf-8")
        self.assert_rejected_unchanged(self.accept, packet)

    def test_cancel_requires_exact_transfer_and_restores_source_without_deleting_packet(self):
        packet = self.prepare()
        ready = lab_handoff.status(self.project)
        packet_before = file_tree(packet)
        self.assert_rejected_unchanged(
            lab_handoff.cancel, self.project, "wrong-id", "Continue in source", host="claude")
        self.assert_rejected_unchanged(
            lab_handoff.cancel, self.project, ready["handoff_id"], "", host="claude")
        cancelled = lab_handoff.cancel(
            self.project, ready["handoff_id"], "Continue in source", host="claude")
        self.assertEqual((cancelled["status"], cancelled["owner"]), ("active", "claude"))
        self.assert_packet_preserved(packet, packet_before)
        lab_handoff.check_owner(self.project, host="claude")
        self.assert_rejected_unchanged(self.accept, packet)

    def test_only_existing_owner_can_prepare_the_next_transfer(self):
        first = self.prepare()
        self.accept(first)
        self.assert_rejected_unchanged(self.prepare, source="claude", target="codex")
        second = self.prepare(source="codex", target="claude")
        self.assertNotEqual(second, first)
        self.assertEqual(self.accept(second, target="claude")["owner"], "claude")

    def test_old_acceptance_cannot_override_a_new_ready_transfer(self):
        first = self.prepare()
        self.accept(first)
        second = self.prepare(source="codex", target="claude")
        self.assert_rejected_unchanged(self.accept, first)
        self.assertEqual(lab_handoff.status(self.project)["status"], "ready")
        self.assertEqual(self.accept(second, target="claude")["owner"], "claude")
        self.assert_rejected_unchanged(self.accept, first)

    def test_host_detection_has_explicit_precedence_and_unknown_owner_is_denied(self):
        packet = self.prepare(source="codex", target="claude")
        self.accept(packet, target="claude")
        self.assert_rejected_unchanged(lab_handoff.check_owner, self.project)
        with patch.dict(os.environ, {"CLAUDECODE": "1", "CODEX_THREAD_ID": "fixture"}):
            lab_handoff.check_owner(self.project)
            self.assert_rejected_unchanged(lab_handoff.check_owner, self.project, host="codex")
        with patch.dict(os.environ, {"LABKIT_HOST": "codex", "CLAUDECODE": "1"}):
            self.assert_rejected_unchanged(lab_handoff.check_owner, self.project)
            lab_handoff.check_owner(self.project, host="claude")
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "fixture"}):
            self.assert_rejected_unchanged(lab_handoff.check_owner, self.project)
        with patch.dict(os.environ, {"LABKIT_HOST": "claude", "CODEX_THREAD_ID": "fixture"}):
            lab_handoff.check_owner(self.project)


if __name__ == "__main__":
    unittest.main()
