"""Deterministic controller transitions; real CLI behavior is tested separately."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import lab_models
import lab_run_legacy as lab_run
from lab_fs import trash


def decision(action, task="Fix the requested function"):
    return json.dumps({"action": action, "summary": "Review: " + action,
                       "task": task if action == "execute" else "",
                       "acceptance": ["Run the relevant test"] if action == "execute" else [],
                       "evidence": ["Inspected the diff and passing test"] if action == "complete" else []})


class RunTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(
            prefix="labkit-run-test-", dir=Path(tempfile.gettempdir()).resolve()))
        self.addCleanup(trash, self.temp, within=Path(tempfile.gettempdir()).resolve())
        self.project = self.temp
        (self.project / ".labkit.json").write_text('{"slug":"fixture"}', encoding="utf-8")
        self.run_dir = lab_run.create_run(self.project, "Original task with its exact constraints.", "cli")
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.enterContext(patch.dict(os.environ, {"LABKIT_ROLE": ""}))
        self.calls = []

    def models(self, *outputs):
        iterator = iter(outputs)

        def invoke(role, **kwargs):
            self.calls.append((role, kwargs))
            text = next(iterator)
            if isinstance(text, Exception):
                raise text
            return {"text": text, "requested_model": role, "actual_models": [role],
                    "session_id": "fixture", "permission_denials": []}

        return patch.object(lab_models, "invoke", side_effect=invoke)

    def test_astra_controls_execution_rework_and_acceptance(self):
        with self.models(decision("execute"), "Changed file; test failed.",
                         decision("execute", "Correct the failed test case"), "Tests passed.",
                         decision("complete")):
            self.assertEqual(lab_run.advance(self.run_dir), 0)
        self.assertEqual([role for role, _ in self.calls],
                         ["planner", "worker", "planner", "worker", "planner"])
        self.assertIn("Correct the failed test case", self.calls[3][1]["prompt"])
        self.assertIn("Original task with its exact constraints.", self.calls[4][1]["prompt"])
        self.assertIn("test failed", self.calls[2][1]["prompt"])
        state = lab_run.load(self.run_dir)
        self.assertEqual((state["status"], state["worker_turns"]), ("complete", 2))

    def test_round_limit_pauses_without_claiming_completion(self):
        with self.models(decision("execute"), "Work done", decision("execute")):
            self.assertEqual(lab_run.advance(self.run_dir, max_rounds=1), 3)
        self.assertEqual(lab_run.load(self.run_dir)["status"], "paused")
        with self.models(decision("complete")):
            self.assertEqual(lab_run.advance(self.run_dir), 0)
        self.assertEqual(lab_run.load(self.run_dir)["worker_turns"], 1)

    def test_selected_windows_sandbox_survives_resume(self):
        with self.models(decision("blocked")):
            self.assertEqual(lab_run.advance(self.run_dir, windows_sandbox="unelevated"), 2)
        self.assertEqual(lab_run.load(self.run_dir)["windows_sandbox"], "unelevated")
        with self.models(decision("complete")):
            self.assertEqual(lab_run.advance(self.run_dir), 0)
        self.assertEqual(self.calls[-1][1]["windows_sandbox"], "unelevated")

    def test_interrupted_worker_is_reviewed_before_retry(self):
        with self.models(decision("execute"), lab_models.ModelError("permission denied")):
            self.assertEqual(lab_run.advance(self.run_dir), 1)
        state = lab_run.load(self.run_dir)
        self.assertEqual(state["status"], "interrupted")
        self.assertEqual(state["pending"]["role"], "worker")
        with self.models(decision("blocked")):
            self.assertEqual(lab_run.advance(self.run_dir), 2)
        self.assertEqual(self.calls[-1][0], "planner")
        self.assertIn("Previous process ended", self.calls[-1][1]["prompt"])

    def test_complete_requires_evidence(self):
        invalid = json.loads(decision("complete"))
        invalid["evidence"] = []
        with self.models(json.dumps(invalid)):
            self.assertEqual(lab_run.advance(self.run_dir), 1)
        self.assertEqual(lab_run.load(self.run_dir)["status"], "interrupted")
        self.assertEqual(len(self.calls), 1)

    def test_invalid_decision_never_reaches_worker(self):
        for text in ('{"action":"execute"}', '```json\n{}\n```',
                     decision("execute", "")):
            with self.subTest(text=text):
                with self.models(text):
                    self.assertEqual(lab_run.advance(self.run_dir), 1)
        self.assertTrue(all(role == "planner" for role, _ in self.calls))

    def test_nested_controller_is_refused(self):
        with patch.dict(os.environ, {"LABKIT_ROLE": "worker"}):
            with self.assertRaisesRegex(RuntimeError, "nested"):
                lab_run.advance(self.run_dir)
        self.assertEqual(lab_run.load(self.run_dir)["status"], "ready")

    def test_copied_run_cannot_target_its_old_project(self):
        copied = self.project / "copied-run"
        copied.mkdir()
        (copied / "state.json").write_bytes((self.run_dir / "state.json").read_bytes())
        with patch.object(lab_models, "invoke") as invoke:
            with self.assertRaisesRegex(ValueError, "location disagrees"):
                lab_run.advance(copied)
            invoke.assert_not_called()

    def test_project_lock_prevents_concurrent_worker_loops(self):
        with lab_run.project_lock(self.project):
            with self.assertRaisesRegex(RuntimeError, "another labkit run"):
                lab_run.advance(self.run_dir)
        self.assertEqual(lab_run.load(self.run_dir)["history"], [])

    def test_claude_host_executes_natively_and_astra_reviews_report(self):
        with self.models(decision("execute")):
            self.assertEqual(lab_run.advance(self.run_dir, worker_mode="host"), 4)
        state = lab_run.load(self.run_dir)
        self.assertEqual(state["status"], "awaiting_worker")
        self.assertEqual([role for role, _ in self.calls], ["planner"])
        prompt = self.run_dir / (state["pending"]["artifact"] + ".prompt.md")
        self.assertIn("Fix the requested function", prompt.read_text(encoding="utf-8"))
        with self.models(decision("complete")):
            self.assertEqual(lab_run.submit(self.run_dir, "Opus edited file and ran tests.", "claude-opus-5",
                                            artifact=state["pending"]["artifact"]), 0)
        self.assertEqual([role for role, _ in self.calls], ["planner", "planner"])
        self.assertIn("Opus edited file", self.calls[-1][1]["prompt"])
        with self.assertRaisesRegex(ValueError, "not awaiting"):
            lab_run.submit(self.run_dir, "duplicate report", "claude-opus-5", artifact="002-worker")

    def test_host_submissions_preserve_round_limit(self):
        with self.models(decision("execute")):
            self.assertEqual(lab_run.advance(self.run_dir, max_rounds=1, worker_mode="host"), 4)
        with self.models(decision("execute")):
            self.assertEqual(lab_run.submit(self.run_dir, "Work needs another pass", "claude-opus-5",
                                            artifact="002-worker"), 3)
        self.assertEqual(lab_run.load(self.run_dir)["worker_turns"], 1)

    def test_host_resume_preserves_exact_task_and_budget(self):
        with self.models(decision("execute")):
            self.assertEqual(lab_run.advance(self.run_dir, max_rounds=1, worker_mode="host"), 4)
        before = lab_run.load(self.run_dir)
        with patch.object(lab_models, "invoke") as invoke:
            self.assertEqual(lab_run.advance(self.run_dir, max_rounds=10, worker_mode="cli"), 4)
            invoke.assert_not_called()
        self.assertEqual(lab_run.load(self.run_dir), before)
        with self.assertRaisesRegex(ValueError, "stale worker report"):
            lab_run.submit(self.run_dir, "wrong task", "claude-opus-5", artifact="000-worker")
        self.assertEqual(lab_run.load(self.run_dir), before)

    def test_host_work_and_pending_review_reserve_project(self):
        second = lab_run.create_run(self.project, "Another independent task", "cli")
        with self.models(decision("execute")):
            self.assertEqual(lab_run.advance(self.run_dir, worker_mode="host"), 4)
        with self.assertRaisesRegex(RuntimeError, "awaits host work/review"):
            lab_run.advance(second)
        # Simulate process exit after saving the report but before calling Astra.
        with patch.object(lab_run, "advance", return_value=1):
            lab_run.submit(self.run_dir, "Partial work; interrupted", "claude-opus-5", artifact="002-worker")
        self.assertEqual(lab_run.load(self.run_dir)["status"], "review_ready")
        with self.assertRaisesRegex(RuntimeError, "awaits host work/review"):
            lab_run.advance(second)
        with self.models(decision("complete")):
            self.assertEqual(lab_run.advance(self.run_dir), 0)
        with self.models(decision("complete")):
            self.assertEqual(lab_run.advance(second), 0)

    def test_interrupted_worker_attempt_does_not_escape_budget(self):
        with self.models(decision("execute"), lab_models.ModelError("interrupted")):
            self.assertEqual(lab_run.advance(self.run_dir, max_rounds=1), 1)
        state = lab_run.load(self.run_dir)
        self.assertEqual((state["worker_turns"], state["round_limit"]), (1, 1))
        with self.models(decision("execute")):
            self.assertEqual(lab_run.advance(self.run_dir), 3)
        self.assertEqual([role for role, _ in self.calls], ["planner", "worker", "planner"])

    def test_worker_result_and_attempt_count_are_saved_together(self):
        original_save = lab_run.save

        def save_then_exit(run_dir, state):
            original_save(run_dir, state)
            if state["history"] and state["history"][-1]["role"] == "worker":
                raise SystemExit("simulate abrupt process exit")

        with self.models(decision("execute"), "Worker completed"), \
                patch.object(lab_run, "save", side_effect=save_then_exit):
            with self.assertRaises(SystemExit):
                lab_run.advance(self.run_dir, max_rounds=1)
        state = lab_run.load(self.run_dir)
        self.assertEqual((state["worker_turns"], state["round_limit"]), (1, 1))
        self.assertIsNone(state["pending"])
        with self.models(decision("execute")):
            self.assertEqual(lab_run.advance(self.run_dir), 3)


if __name__ == "__main__":
    unittest.main()

