"""Offline native-controller transitions; runtime identity parsing is tested separately."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import lab_models
import lab_native
import lab_run
from lab_fs import trash


def selected_team():
    return {
        "selection": "Claude lead and critic; Sonnet worker.",
        "brains": [
            {"id": "lead", "provider": "claude", "model": "claude-opus-5", "effort": "max"},
            {"id": "critic", "provider": "claude", "model": "claude-sonnet-4-6", "effort": "high"},
        ],
        "workers": [{"id": "coder", "provider": "claude", "model": "claude-sonnet-4-6", "effort": "high"}],
    }


def plan():
    return json.dumps({"action": "execute", "summary": "Produce the answer.", "worker": "coder",
                       "task": "Write result.txt containing correct.", "acceptance": ["Read result.txt"]})


def review():
    return json.dumps({"verdict": "pass", "summary": "Read the deliverable.", "issues": [],
                       "criteria": [{"id": "answer", "passed": True,
                                     "evidence": ["result.txt contains correct"]}]})


def native_report(value):
    # Matches the observed native Plan preamble + JSON block + required footer.
    return ("State is clear. The original goal and project files have been inspected.\n\n"
            + "```json\n" + json.dumps(json.loads(value), indent=2) + "\n```\n\n"
            + "### Critical Files for Implementation\n- `result.txt`\n- `.labkit.json`\n")


class NativeControllerTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"CLAUDECODE": "1", "LABKIT_ROLE": ""}))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        parent = Path(tempfile.gettempdir()).resolve()
        self.project = Path(tempfile.mkdtemp(prefix="labkit-native-controller-", dir=parent)).resolve()
        self.addCleanup(trash, self.project, within=parent)
        (self.project / ".labkit.json").write_text("{}", encoding="utf-8")
        goal = {"objective": "Write the requested answer.", "deliverables": ["result.txt"],
                "acceptance": [{"id": "answer", "text": "result.txt contains correct"}]}
        self.run_dir = lab_run.create_run(self.project, goal, selected_team())
        self.invoke = self.enterContext(patch.object(
            lab_models, "invoke", side_effect=AssertionError("native work must not invoke a CLI")))
        self.verify = self.enterContext(patch.object(lab_native, "verify", side_effect=self.verified))
        self.transcript = self.project / "mock-runtime-transcript.jsonl"

    @staticmethod
    def verified(pending, report, transcript_file):
        # The actual runtime verifier has its own fixtures and live evidence.
        return {"text": report, "actual_models": [pending["member"]["model"]],
                "identity_verified": True, "identity_source": "offline-test-stub",
                "effort_verified": False}

    def state(self):
        return lab_run.load(self.run_dir)

    def submit(self, report, artifact=None):
        artifact = artifact or self.state()["pending"]["artifact"]
        return lab_run.submit(self.run_dir, report, artifact=artifact, transcript_file=self.transcript)

    def reach_worker(self):
        self.assertEqual(lab_run.advance(self.run_dir), 4)
        self.assertEqual(self.submit("Inspect the requested answer."), 4)
        self.assertEqual(self.submit(plan()), 4)
        self.assertEqual(self.state()["pending"]["role"], "worker")

    def write_answer(self, answer="correct"):
        (self.project / "result.txt").write_text(answer, encoding="utf-8")

    def test_every_native_role_advances_once_and_final_review_fits_exact_call_budget(self):
        state = self.state()
        state["call_limit"] = 5
        lab_run.save(self.run_dir, state)
        self.assertEqual(lab_run.advance(self.run_dir), 4)
        transitions = (
            ("advisor", "Inspect the goal.", "planner", "plan", 2, 0),
            ("planner", plan(), "worker", "work", 3, 1),
            ("worker", "Saved and read result.txt.", "reviewer", "review", 4, 1),
            ("reviewer", review(), "reviewer", "review", 5, 1),
        )
        for role, report, next_role, phase, calls, workers in transitions:
            with self.subTest(role=role):
                before = self.state()
                pending = before["pending"]
                self.assertEqual(pending["role"], role)
                if role == "worker":
                    self.write_answer()
                self.assertEqual(self.submit(report), 4)
                after = self.state()
                self.assertEqual((after["pending"]["role"], after["phase"]), (next_role, phase))
                self.assertEqual((after["calls"], after["worker_turns"]), (calls, workers))
                self.assertEqual(after["history"][-1]["artifact"], pending["artifact"])
                self.assertEqual(len(after["history"]), len(before["history"]) + 1)
                self.assertTrue((self.run_dir / (pending["artifact"] + ".result.json")).is_file())
                self.verify.assert_called_with(pending, report, self.transcript)
        self.assertEqual(self.submit(review()), 0)
        final = self.state()
        self.assertEqual((final["status"], final["calls"], final["worker_turns"]), ("complete", 5, 1))
        self.assertIsNone(final["pending"])
        self.assertEqual([item["role"] for item in final["history"]],
                         ["advisor", "planner", "worker", "reviewer", "reviewer"])
        self.assertTrue((self.run_dir / "delivery.json").is_file())
        self.assertEqual(self.verify.call_count, 5)
        self.invoke.assert_not_called()

    def test_stale_and_duplicate_reports_cannot_consume_the_current_pending_request(self):
        self.assertEqual(lab_run.advance(self.run_dir), 4)
        before = self.state()
        original = before["pending"]["artifact"]
        with self.assertRaisesRegex(ValueError, "stale native report"):
            self.submit("Advice", artifact="000-stale")
        self.assertEqual(self.state(), before)
        self.verify.assert_not_called()
        self.assertEqual(self.submit("Advice"), 4)
        after = self.state()
        with self.assertRaisesRegex(ValueError, "stale native report"):
            self.submit("Advice", artifact=original)
        self.assertEqual(self.state(), after)
        self.assertEqual(self.verify.call_count, 1)

    def test_invalid_native_plan_and_review_schema_keep_pending_and_budget(self):
        self.assertEqual(lab_run.advance(self.run_dir), 4)
        self.assertEqual(self.submit("Advice"), 4)
        for role, valid_report in (("planner", plan()), ("reviewer", review())):
            with self.subTest(role=role):
                before = self.state()
                self.assertEqual(before["pending"]["role"], role)
                with self.assertRaises(ValueError):
                    self.submit('{"summary":"Incomplete schema"}')
                self.assertEqual(self.state(), before)
                self.assertEqual(self.submit(valid_report), 4)
                self.assertEqual(self.state()["calls"], before["calls"] + 1)
                if role == "planner":
                    self.write_answer()
                    self.assertEqual(self.submit("Saved result.txt."), 4)

    def test_native_json_block_plan_and_review_keep_raw_attestation_and_structured_output(self):
        self.assertEqual(lab_run.advance(self.run_dir), 4)
        self.assertEqual(self.submit("Advice"), 4)
        for role, value in (("planner", plan()), ("reviewer", review())):
            with self.subTest(role=role):
                pending = self.state()["pending"]
                self.assertEqual(pending["role"], role)
                report = native_report(value)
                self.assertEqual(self.submit(report), 4)
                saved = json.loads((self.run_dir / (pending["artifact"] + ".result.json"))
                                   .read_text(encoding="utf-8"))
                self.assertEqual(saved["text"], report)
                self.assertEqual(saved["structured_output"], json.loads(value))
                self.assertIs(saved["identity_verified"], True)
                self.assertEqual(self.state()["history"][-1]["summary"], report)
                self.verify.assert_called_with(pending, report, self.transcript)
                if role == "planner":
                    self.assertEqual(self.state()["plan"], json.loads(value))
                    self.write_answer()
                    self.assertEqual(self.submit("Saved result.txt."), 4)

    def test_ambiguous_native_json_or_invalid_goal_review_cannot_clear_pending(self):
        self.assertEqual(lab_run.advance(self.run_dir), 4)
        self.assertEqual(self.submit("Advice"), 4)
        missing = json.loads(review())
        missing["criteria"] = []
        false_pass = json.loads(review())
        false_pass["criteria"][0]["passed"] = False
        cases = (
            ("planner", [native_report(plan()) + native_report(plan())], plan()),
            ("reviewer", [native_report(review()) + native_report(review()),
                          native_report(json.dumps(missing)), native_report(json.dumps(false_pass))], review()),
        )
        for role, invalid_reports, valid in cases:
            for report in invalid_reports:
                with self.subTest(role=role, report=report):
                    before = self.state()
                    self.assertEqual(before["pending"]["role"], role)
                    with self.assertRaises(ValueError):
                        self.submit(report)
                    self.assertEqual(self.state(), before)
                    saved = json.loads((self.run_dir / (before["pending"]["artifact"] + ".result.json"))
                                       .read_text(encoding="utf-8"))
                    self.assertEqual(saved["text"], report)
                    self.assertNotIn("structured_output", saved)
            before = self.state()
            self.assertEqual(self.submit(native_report(valid)), 4)
            self.assertEqual(self.state()["calls"], before["calls"] + 1)
            if role == "planner":
                self.write_answer()
                self.assertEqual(self.submit("Saved result.txt."), 4)

    def test_post_report_snapshot_failure_allows_same_submission_without_replaying_worker(self):
        self.reach_worker()
        self.write_answer()
        before = self.state()
        artifact = before["pending"]["artifact"]
        workflow_file = self.run_dir / (artifact + ".workflow.json")
        workflow = workflow_file.read_bytes()
        report = "Saved and read result.txt."
        with patch.object(lab_run, "snapshot", side_effect=OSError("snapshot temporarily unreadable")):
            with self.assertRaisesRegex(OSError, "snapshot temporarily unreadable"):
                self.submit(report)
        self.assertEqual(self.state(), before)
        result_file = self.run_dir / (artifact + ".result.json")
        self.assertEqual(json.loads(result_file.read_text(encoding="utf-8"))["text"], report)
        self.assertEqual(lab_run.advance(self.run_dir, resume=True), 4)
        self.assertEqual(self.state(), before)
        self.assertEqual(workflow_file.read_bytes(), workflow)
        self.assertEqual(self.submit(report, artifact=artifact), 4)
        after = self.state()
        self.assertEqual((after["pending"]["role"], after["worker_turns"]), ("reviewer", 1))
        self.assertEqual(after["calls"], before["calls"] + 1)
        self.assertEqual(sum(item["role"] == "worker" for item in after["history"]), 1)
        self.assertEqual(len(list(self.run_dir.glob("*-worker-*.workflow.json"))), 1)
        self.invoke.assert_not_called()

    def test_recovery_requires_stopped_exact_attempt_and_sends_partial_work_back_to_brains(self):
        self.reach_worker()
        self.write_answer("partial change")
        before = self.state()
        artifact = before["pending"]["artifact"]
        reason = "Workflow stopped after a partial write; inspect result.txt before new work."
        for stopped, selected in ((False, artifact), (True, "000-stale")):
            with self.subTest(stopped=stopped, artifact=selected), self.assertRaises(ValueError):
                lab_run.recover(self.run_dir, artifact=selected, reason=reason, workflow_stopped=stopped)
            self.assertEqual(self.state(), before)
        self.assertEqual(lab_run.recover(self.run_dir, artifact=artifact, reason=reason,
                                        workflow_stopped=True), 4)
        after = self.state()
        self.assertEqual((after["pending"]["role"], after["phase"]), ("advisor", "advice"))
        self.assertEqual((after["calls"], after["worker_turns"]), (before["calls"] + 1, 1))
        for field in ("cycles", "round_limit", "call_limit", "team"):
            self.assertEqual(after[field], before[field])
        self.assertIsNone(after["plan"])
        self.assertEqual(after["interruption"]["pending"], before["pending"])
        self.assertEqual((self.project / "result.txt").read_text(encoding="utf-8"), "partial change")
        prompt = (self.run_dir / (after["pending"]["artifact"] + ".prompt.md")).read_text(encoding="utf-8")
        self.assertIn(reason, prompt)
        self.assertFalse(any(item["role"] == "worker" for item in after["history"]))
        self.invoke.assert_not_called()

    def test_host_output_failure_cannot_let_resume_discard_a_possibly_running_worker(self):
        self.assertEqual(lab_run.advance(self.run_dir), 4)
        self.assertEqual(self.submit("Advice"), 4)
        host_request = lab_run.host_request

        def published_then_output_failed(state, run_dir):
            host_request(state, run_dir)
            raise BrokenPipeError("host may have received the request before output failed")

        with patch.object(lab_run, "host_request", side_effect=published_then_output_failed):
            self.assertEqual(self.submit(plan()), 1)
        before = self.state()
        self.assertEqual(before["pending"]["role"], "worker")
        self.assertEqual(before["pending"]["mode"], "host")
        self.assertEqual(lab_run.advance(self.run_dir, resume=True), 4)
        after = self.state()
        self.assertEqual(after["pending"], before["pending"])
        self.assertEqual((after["calls"], after["worker_turns"]),
                         (before["calls"], before["worker_turns"]))
        self.assertEqual(after["history"], before["history"])
        self.invoke.assert_not_called()

    def test_preexisting_interrupted_host_pending_can_resume_and_submit_the_same_attempt(self):
        self.reach_worker()
        self.write_answer()
        before = self.state()
        before["status"] = "interrupted"
        lab_run.save(self.run_dir, before)
        self.assertEqual(lab_run.advance(self.run_dir, resume=True), 4)
        self.assertEqual(self.state()["pending"], before["pending"])
        self.assertEqual(self.submit("Saved and read result.txt."), 4)
        after = self.state()
        self.assertEqual((after["pending"]["role"], after["worker_turns"]), ("reviewer", 1))
        self.assertEqual(after["calls"], before["calls"] + 1)

    def test_resume_preserves_an_existing_legacy_workflow_envelope_byte_for_byte(self):
        with patch.object(lab_run, "host_request"):
            self.assertEqual(lab_run.advance(self.run_dir), 4)
        before = self.state()
        pending = before["pending"]
        artifact = pending["artifact"]
        legacy = {
            "script": "const input=JSON.parse(args); return await agent(input.prompt,{model:input.model,agentType:input.agent_type});",
            "args": json.dumps({
                "prompt": (self.run_dir / (artifact + ".prompt.md")).read_text(encoding="utf-8"),
                "model": pending["member"]["model"], "agent_type": "Plan"}),
        }
        original = (json.dumps(legacy, ensure_ascii=False, indent=4) + "\n  \n").encode("utf-8")
        workflow_file = self.run_dir / (artifact + ".workflow.json")
        workflow_file.write_bytes(original)
        self.assertEqual(lab_run.advance(self.run_dir, resume=True), 4)
        self.assertEqual(self.state(), before)
        self.assertEqual(workflow_file.read_bytes(), original)
        self.assertFalse((self.run_dir / (artifact + ".workflow.js")).exists())
        self.invoke.assert_not_called()

    @unittest.skipUnless(shutil.which("node"), "Node is needed only to execute the generated offline Workflow script")
    def test_workflow_script_path_preserves_long_multiline_unicode_prompt_and_selected_options(self):
        original_prompt = lab_run.prompt_for
        payload = ('\n"double quotes" and \'single quotes\'; `backticks`; ${literal}; '
                   'C:\\workspace\\file.txt\n中文与 emoji 🧪; separators \u2028 \u2029\n'
                   + ('多行 history "quoted" \\ literal\n' * 6000))

        def prompt_with_long_history(*args, **kwargs):
            return original_prompt(*args, **kwargs) + payload

        with patch.object(lab_run, "prompt_for", side_effect=prompt_with_long_history):
            self.reach_worker()
        workflow_files = sorted(self.run_dir.glob("*.workflow.json"))
        workflows = [json.loads(path.read_text(encoding="utf-8")) for path in workflow_files]
        for workflow in workflows:
            self.assertEqual(set(workflow), {"scriptPath"})
            self.assertTrue(Path(workflow["scriptPath"]).is_absolute())
            self.assertTrue(Path(workflow["scriptPath"]).is_file())
        harness = r"""
import { readFile } from 'node:fs/promises';
let input = '';
for await (const chunk of process.stdin) input += chunk;
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
const output = [];
for (const request of JSON.parse(input)) {
  const script = await readFile(request.scriptPath, 'utf8');
  const run = new AsyncFunction('agent', 'phase',
    script.replace(/^export const meta=/, 'const meta='));
  const calls = [];
  const value = await run(async (prompt, opts) => {
    calls.push({prompt, opts}); return 'OFFLINE_WORKFLOW_OK';
  }, () => {});
  output.push({value, calls});
}
process.stdout.write(JSON.stringify(output));
"""
        completed = subprocess.run([shutil.which("node"), "--input-type=module", "-e", harness],
                                   input=json.dumps(workflows), capture_output=True, text=True,
                                   encoding="utf-8", timeout=15, check=True)
        results = json.loads(completed.stdout)
        self.assertEqual(len(results), 3)
        team = selected_team()
        members = (team["brains"][1], team["brains"][0], team["workers"][0])
        for path, result, member, expected_type in zip(workflow_files, results, members,
                                                      ("Plan", "Plan", "general-purpose")):
            artifact = path.name.removesuffix(".workflow.json")
            prompt = (self.run_dir / (artifact + ".prompt.md")).read_text(encoding="utf-8")
            self.assertEqual(result["value"], "OFFLINE_WORKFLOW_OK")
            self.assertEqual(result["calls"], [{"prompt": prompt, "opts": {
                "model": member["model"], "agentType": expected_type,
                "effort": member["effort"], "label": artifact}}])
            self.assertIn(payload, prompt)
            self.assertGreater(len(prompt.encode("utf-8")), 200000)
            self.assertIn("Labkit request token:", prompt)


if __name__ == "__main__":
    unittest.main()
