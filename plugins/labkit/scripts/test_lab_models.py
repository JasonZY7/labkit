"""Offline adapter checks; every model/CLI subprocess is mocked."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import lab_models
from lab_fs import trash


class ModelAdapters(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(
            prefix="labkit-model-tests-", dir=Path(tempfile.gettempdir()).resolve()))
        self.addCleanup(trash, self.temp, within=Path(tempfile.gettempdir()).resolve())
        self.project = self.temp
        self.artifact = self.project / "round.1"
        self.calls = []
        self.stdout = json.dumps({"type": "result", "is_error": False, "result": "done",
                                  "modelUsage": {"claude-opus-5": {}},
                                  "session_id": "worker-session", "permission_denials": []})
        self.stderr = ""
        self.answer = '{"decision":"done"}'
        self.returncode = 0
        self.process = MagicMock(pid=12345)
        self.timeout = False
        self.failure = None
        self.enterContext(patch.dict(os.environ, {"CLAUDECODE": ""}))
        self.enterContext(patch.object(lab_models.shutil, "which", side_effect=lambda name: name + ".exe"))
        self.popen = self.enterContext(patch.object(lab_models.subprocess, "Popen", side_effect=self.launch))

    def launch(self, arguments, **options):
        self.calls.append((arguments, options))
        self.process.returncode = self.returncode

        def communicate(*, input, timeout):
            self.assertEqual(input, self.prompt.encode("utf-8"))
            if self.timeout:
                raise subprocess.TimeoutExpired(arguments, timeout)
            if self.failure is not None:
                raise self.failure
            options["stdout"].write(self.stdout.encode("utf-8"))
            options["stderr"].write(self.stderr.encode("utf-8"))
            if "--output-last-message" in arguments and self.answer is not None:
                output = arguments[arguments.index("--output-last-message") + 1]
                Path(output).write_text(self.answer, encoding="utf-8")

        self.process.communicate.side_effect = communicate
        return self.process

    def invoke(self, role="worker", **kwargs):
        self.prompt = '中文 prompt with "quotes", $variables, and `literal`\nsecond line'
        return lab_models.invoke(role, project=self.project, prompt=self.prompt,
                                 artifact=self.artifact, **kwargs)

    def planner_events(self):
        self.stdout = '\n'.join(json.dumps(event) for event in (
            {"type": "thread.started", "thread_id": "planner-session"},
            {"type": "turn.started"}, {"type": "turn.completed", "usage": {}}))

    def claude_events(self, model, *, response_models=None, usage_models=None, **response):
        events = [{"type": "system", "subtype": "init", "model": model}]
        for reported in response_models or [model]:
            events.append({"type": "assistant", "parent_tool_use_id": None,
                           "message": {"model": reported, "content": [{"type": "text", "text": "done"}]}})
        events.append({"type": "result", "subtype": "success", "is_error": False,
                       "result": "done", "permission_denials": [], "session_id": "worker-session",
                       "modelUsage": {name: {} for name in (usage_models or [model])}, **response})
        self.stdout = "\n".join(json.dumps(event) for event in events)

    def test_planner_argv_schema_and_honest_identity(self):
        self.planner_events()
        schema = {"type": "object", "properties": {"decision": {"type": "string"}}}
        result = self.invoke("planner", schema=schema)
        args, options = self.calls[0]
        self.assertEqual(args[:7], ["codex.exe", "-a", "never", "exec", "--model", "gpt-6-astra", "-c"])
        self.assertEqual(args[args.index("--sandbox") + 1], "read-only")
        self.assertEqual(args[args.index("-C") + 1], str(self.project))
        self.assertIn("--json", args)
        self.assertEqual(args[-1], "-")
        self.assertEqual(json.loads(Path(args[args.index("--output-schema") + 1]).read_text()), schema)
        self.assertEqual(options["env"]["LABKIT_ROLE"], "planner")
        self.assertEqual(result["text"], self.answer)
        self.assertEqual(result["session_id"], "planner-session")
        self.assertEqual(result["actual_models"], [])
        self.assertFalse(result["identity_verified"])

    def test_jsonl_does_not_split_unicode_line_separators_inside_text(self):
        self.planner_events()
        self.stdout += "\n" + json.dumps({"type": "item.completed", "text": "line\u2028separator"}, ensure_ascii=False)
        self.assertEqual(self.invoke("planner")["text"], self.answer)
        self.claude_events("claude-opus-5")
        self.stdout = self.stdout.replace('"done"', '"line\u2028separator"')
        self.assertEqual(self.invoke()["text"], "line\u2028separator")

    def test_worker_keeps_auth_customizations_and_native_approval(self):
        with patch.dict(os.environ, {"LABKIT_TEST_SENTINEL": "preserved"}, clear=True):
            result = self.invoke()
        args, options = self.calls[0]
        self.assertEqual(args[args.index("--model") + 1], "claude-opus-5")
        self.assertEqual(args[args.index("--permission-mode") + 1], "auto")
        self.assertEqual(args[args.index("--plugin-dir") + 1], str(lab_models.PLUGIN_ROOT))
        self.assertEqual(options["cwd"], str(self.project))
        self.assertEqual(options["env"]["LABKIT_TEST_SENTINEL"], "preserved")
        self.assertEqual(options["env"]["LABKIT_ROLE"], "worker")
        for flag in ("--safe-mode", "--bare", "--dangerously-skip-permissions", "--allowedTools"):
            self.assertNotIn(flag, args)
        self.assertEqual(result["actual_models"], ["claude-opus-5"])
        self.assertTrue(result["identity_verified"])
        self.assertEqual(result["identity_scope"], "usage attribution only")
        self.assertEqual(Path(str(self.artifact) + ".prompt.md").read_text(encoding="utf-8"), self.prompt)

    def test_provider_role_matrix_uses_exact_models_and_role_permissions(self):
        for provider in ("codex", "claude"):
            for role in ("planner", "advisor", "reviewer", "worker"):
                with self.subTest(provider=provider, role=role):
                    model = provider + "-selected-exact-20260909"
                    if provider == "codex":
                        self.planner_events()
                        self.stderr = "model: " + model + "\n"
                    else:
                        self.claude_events(model)
                        self.stderr = ""
                    result = self.invoke(role, provider=provider, model=model, effort="high")
                    args, options = self.calls[-1]
                    self.assertEqual(args[0], provider + ".exe")
                    self.assertEqual(args[args.index("--model") + 1], model)
                    self.assertEqual(result["provider"], provider)
                    self.assertEqual(result["requested_model"], model)
                    self.assertEqual(result["requested_effort"], "high")
                    self.assertEqual(result["access_mode"], "workspace-write" if role == "worker" else "read-only")
                    self.assertEqual(options["env"]["LABKIT_ROLE"], role)
                    self.assertNotIn("--fallback-model", args)
                    self.assertNotIn("--dangerously-skip-permissions", args)
                    self.assertNotIn("--allowedTools", args)
                    if provider == "codex":
                        self.assertEqual(args[args.index("--sandbox") + 1], result["access_mode"])
                        self.assertIn("model_reasoning_effort=high", args)
                        self.assertFalse(result["identity_verified"])
                    else:
                        self.assertEqual(args[args.index("--permission-mode") + 1],
                                         "auto" if role == "worker" else "plan")
                        self.assertEqual(result["response_models"], [model])
                        self.assertEqual(result["identity_source"], "assistant.message.model")
                        self.assertTrue(result["identity_verified"])
                        if role == "worker":
                            self.assertNotIn("--tools", args)
                        else:
                            self.assertEqual(args[args.index("--tools") + 1], "Read,Glob,Grep,Bash")
                            self.assertEqual(args[args.index("--disallowedTools") + 1], "mcp__*")
                            self.assertIn("not an OS", result["capability_limits"][0])

    def test_explicit_selection_keeps_cli_default_effort_when_omitted(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                if provider == "codex":
                    self.planner_events()
                else:
                    self.claude_events("claude-opus-5")
                result = self.invoke("reviewer", provider=provider)
                args, _ = self.calls[-1]
                self.assertIsNone(result["requested_effort"])
                self.assertNotIn("--effort", args)
                self.assertFalse(any(arg.startswith("model_reasoning_effort=") for arg in args))

    def test_nested_claude_requires_native_dispatch_without_spawning(self):
        for role in ("planner", "advisor", "reviewer", "worker"):
            with self.subTest(role=role), patch.dict(os.environ, {"CLAUDECODE": "test-nesting-marker"}):
                with self.assertRaisesRegex(lab_models.NativeHostRequired, "native host"):
                    self.invoke(role, provider="claude", model="claude-opus-5")
                self.assertEqual(os.environ["CLAUDECODE"], "test-nesting-marker")
                saved = json.loads(Path(str(self.artifact) + ".result.json").read_text())
                self.assertTrue(saved["native_host_required"])
                self.assertEqual(saved["requested_model"], "claude-opus-5")
        self.popen.assert_not_called()

    def test_codex_from_claude_preserves_nesting_guard(self):
        self.planner_events()
        with patch.dict(os.environ, {"CLAUDECODE": "test-nesting-marker"}):
            self.invoke("worker", provider="codex", model="gpt-6-astra")
        self.assertEqual(self.calls[-1][1]["env"]["CLAUDECODE"], "test-nesting-marker")

    def test_invalid_selection_is_rejected_without_model_calls(self):
        for kwargs in ({"role": "unknown"}, {"provider": "other"}, {"model": ""},
                       {"model": " model "}, {"model": "--other-flag"}, {"effort": "high\n"},
                       {"schema": []}):
            with self.subTest(kwargs=kwargs), self.assertRaises(lab_models.ModelError):
                self.invoke(**kwargs)
        self.popen.assert_not_called()

    def test_explicit_windows_fallback_keeps_read_only_and_is_recorded(self):
        self.planner_events()
        result = self.invoke("planner", windows_sandbox="unelevated")
        args, _ = self.calls[0]
        self.assertIn("windows.sandbox=unelevated", args)
        self.assertEqual(args[args.index("--sandbox") + 1], "read-only")
        self.assertEqual(result["windows_sandbox"], "unelevated")
        with self.assertRaisesRegex(lab_models.ModelError, "unsupported Windows"):
            self.invoke("planner", windows_sandbox="disabled")

    def test_worker_failure_permission_and_model_checks(self):
        base = json.loads(self.stdout)
        for update, expected in (({"is_error": True}, "failed turn"),
                                 ({"permission_denials": [{"tool_name": "Edit"}]}, "permissions"),
                                 ({"modelUsage": {"claude-other": {}}}, "requested exact model")):
            with self.subTest(update=update):
                self.stdout = json.dumps({**base, **update})
                with self.assertRaisesRegex(lab_models.ModelError, expected):
                    self.invoke()
                saved = json.loads(Path(str(self.artifact) + ".result.json").read_text())
                self.assertIn("error", saved)
                self.assertEqual(saved["permission_denials"], update.get("permission_denials", []))

    def test_streamed_model_fallback_is_not_accepted(self):
        self.claude_events("claude-custom-1", response_models=["claude-custom-1", "claude-other"],
                           usage_models=["claude-custom-1", "claude-other"])
        with self.assertRaisesRegex(lab_models.ModelError, "requested exact model"):
            self.invoke("reviewer", provider="claude", model="claude-custom-1")
        saved = json.loads(Path(str(self.artifact) + ".result.json").read_text())
        self.assertFalse(saved["identity_verified"])
        self.assertEqual(saved["response_models"], ["claude-custom-1", "claude-other"])

    def test_top_level_identity_is_distinct_from_auxiliary_usage(self):
        self.claude_events("claude-custom-1", usage_models=["claude-custom-1", "claude-auxiliary"])
        result = self.invoke("reviewer", provider="claude", model="claude-custom-1")
        self.assertTrue(result["identity_verified"])
        self.assertEqual(result["identity_scope"], "top-level responses")
        self.assertEqual(result["actual_models"], ["claude-custom-1", "claude-auxiliary"])

    def test_schema_outputs_are_normalized_for_both_providers(self):
        schema = {"type": "object", "properties": {"decision": {"type": "string"}},
                  "required": ["decision"]}
        value = {"decision": "done"}
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                if provider == "codex":
                    self.planner_events()
                else:
                    self.claude_events("claude-opus-5", structured_output=value)
                result = self.invoke("advisor", provider=provider, schema=schema)
                args, _ = self.calls[-1]
                self.assertEqual(result["structured_output"], value)
                self.assertEqual(json.loads(result["text"]), value)
                if provider == "claude":
                    self.assertEqual(json.loads(args[args.index("--json-schema") + 1]), schema)
                    self.assertEqual(json.loads(Path(str(self.artifact) + ".response.json").read_text()), value)

    def test_schema_missing_or_invalid_output_is_rejected(self):
        self.claude_events("claude-opus-5", result='{"decision":"done"}')
        with self.assertRaisesRegex(lab_models.ModelError, "structured output"):
            self.invoke("planner", provider="claude", schema={"type": "object"})
        self.planner_events()
        self.answer = "not JSON"
        with self.assertRaisesRegex(lab_models.ModelError, "parseable structured JSON"):
            self.invoke("reviewer", provider="codex", schema={"type": "object"})

    def test_nonzero_preserves_raw_error_without_surface_leak(self):
        self.stderr = "upstream error with secret-test-token"
        self.returncode = 1
        with self.assertRaises(lab_models.ModelError) as caught:
            self.invoke()
        self.assertNotIn("secret-test-token", str(caught.exception))
        self.assertEqual(Path(str(self.artifact) + ".stderr.txt").read_text(), self.stderr)

    def test_invalid_json_is_a_model_error(self):
        for role, output in (("worker", "not JSON"), ("worker", "[]"),
                             ("planner", "not JSON"), ("planner", "[]")):
            with self.subTest(role=role, output=output):
                self.stdout = output
                with self.assertRaises(lab_models.ModelError):
                    self.invoke(role)

    def test_planner_does_not_accept_previous_answer_or_wrong_banner(self):
        self.planner_events()
        Path(str(self.artifact) + ".response.json").write_text("stale answer")
        self.answer = None
        with self.assertRaisesRegex(lab_models.ModelError, "empty final"):
            self.invoke("planner")
        self.answer = "fresh answer"
        self.stderr = "model: another-model\n"
        with self.assertRaisesRegex(lab_models.ModelError, "different configured model"):
            self.invoke("planner")

    def test_timeout_kills_windows_tree_and_posix_process_group(self):
        self.timeout = True
        for windows in (True, False):
            with self.subTest(windows=windows), patch.object(lab_models, "WINDOWS", windows), \
                 patch.object(lab_models.subprocess, "run", return_value=MagicMock(returncode=0)) as kill, \
                 patch.object(lab_models.signal, "SIGKILL", 9, create=True), \
                 patch.object(lab_models.os, "killpg", create=True) as killpg:
                with self.assertRaisesRegex(lab_models.ModelError, "timed out"):
                    self.invoke(timeout=7)
                if windows:
                    self.assertEqual(kill.call_args.args[0], ["taskkill", "/PID", "12345", "/T", "/F"])
                else:
                    killpg.assert_called_once_with(12345, lab_models.signal.SIGKILL)
                    self.assertTrue(self.calls[-1][1]["start_new_session"])
                self.process.wait.assert_called_with(timeout=30)

    def test_communication_abort_terminates_tree_before_propagation(self):
        for failure, expected in ((KeyboardInterrupt(), KeyboardInterrupt),
                                  (OSError("secret-test-token"), lab_models.ModelError)):
            with self.subTest(failure=type(failure).__name__), \
                 patch.object(lab_models, "_terminate_tree") as stop:
                self.failure = failure
                with self.assertRaises(expected) as caught:
                    self.invoke()
                stop.assert_called_once_with(self.process)
                self.assertNotIn("secret-test-token", str(caught.exception))
                saved = json.loads(Path(str(self.artifact) + ".result.json").read_text())
                self.assertIn("error", saved)
                self.assertNotIn("secret-test-token", saved["error"])

    def test_doctor_is_no_usage_and_sanitized(self):
        replies = [subprocess.CompletedProcess([], 0, "codex-cli 0.153.4\n", ""),
                   subprocess.CompletedProcess([], 0, "", "Logged in using ChatGPT\n"),
                   subprocess.CompletedProcess([], 0, "2.1.224 (Claude Code)\n", ""),
                   subprocess.CompletedProcess([], 1, json.dumps({"loggedIn": False,
                       "authMethod": "none", "apiProvider": "firstParty", "private": "secret-test-token"}), "")]
        with patch.object(lab_models.subprocess, "run", side_effect=replies) as run:
            result = lab_models.doctor()
        self.assertFalse(result["ready"])
        self.assertTrue(result["planner"]["auth_ready"])
        self.assertFalse(result["worker"]["auth_ready"])
        self.assertEqual(set(result["providers"]), {"codex", "claude"})
        self.assertFalse(result["model_access_verified"])
        self.assertNotIn("secret-test-token", json.dumps(result))
        self.assertEqual([call.args[0][1:] for call in run.call_args_list],
                         [["--version"], ["login", "status"], ["--version"], ["auth", "status", "--json"]])
        self.assertTrue(all(call.kwargs["stdin"] == subprocess.DEVNULL for call in run.call_args_list))
        self.popen.assert_not_called()

    def test_catalog_only_checks_selected_cli_and_does_not_claim_entitlement(self):
        replies = [subprocess.CompletedProcess([], 0, "codex-cli 0.153.4\n", ""),
                   subprocess.CompletedProcess([], 0, "", "Logged in using ChatGPT\n")]
        with patch.object(lab_models.subprocess, "run", side_effect=replies) as run:
            result = lab_models.catalog(["codex", "codex"])
        self.assertTrue(result["ready"])
        self.assertEqual(set(result["providers"]), {"codex"})
        self.assertFalse(result["complete"])
        self.assertFalse(result["model_access_verified"])
        self.assertTrue(result["custom_model_ids"])
        self.assertEqual(result["candidates"][0]["provider"], "codex")
        self.assertFalse(result["candidates"][0]["model_access_verified"])
        self.assertEqual(run.call_count, 2)
        self.popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
