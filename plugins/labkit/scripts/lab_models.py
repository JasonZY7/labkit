"""Recorded local CLI turns with explicit providers, models, and role permissions."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess


ROLE_PROVIDERS = {"planner": "codex", "advisor": "codex", "reviewer": "codex", "worker": "claude"}
DEFAULT_MODELS = {"codex": "gpt-6-astra", "claude": "claude-opus-5"}
# Old saved runs and callers may still use these role defaults.
MODELS = {role: DEFAULT_MODELS[provider] for role, provider in ROLE_PROVIDERS.items()}
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
WINDOWS = os.name == "nt"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


class ModelError(RuntimeError):
    """A CLI could not complete the requested model turn."""


class NativeHostRequired(ModelError):
    """The controller must dispatch this role through the current native host."""


def _selection(role, provider, model, effort):
    if not isinstance(role, str) or role not in ROLE_PROVIDERS:
        raise ModelError("role must be planner, advisor, reviewer, or worker")
    legacy_defaults = provider is None and model is None
    provider = ROLE_PROVIDERS[role] if provider is None else provider
    if not isinstance(provider, str) or provider not in DEFAULT_MODELS:
        raise ModelError("provider must be codex or claude")
    model = DEFAULT_MODELS[provider] if model is None else model
    if (not isinstance(model, str) or not model or model.startswith("-")
            or re.search(r"[\s\x00-\x1f\x7f]", model)):
        raise ModelError("model must be a nonempty exact model identifier")
    if effort is None and legacy_defaults:
        effort = "max"
    if effort is not None and (not isinstance(effort, str)
                               or not re.fullmatch(r"[a-z][a-z0-9_-]*", effort)):
        raise ModelError("effort must be a CLI effort identifier or None")
    return provider, model, effort


def _path(prefix: Path, suffix: str) -> Path:
    return Path(str(prefix) + suffix)


def _write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _terminate_tree(process) -> None:
    if WINDOWS:
        # Killing only the CLI leaves its shell/MCP children running on Windows.
        try:
            stopped = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=30,
                creationflags=NO_WINDOW,
            )
            if stopped.returncode and process.poll() is None:
                raise ModelError("Windows could not terminate the model process tree")
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ModelError("Windows could not terminate the model process tree") from exc
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise ModelError("The model process did not exit after tree termination") from exc


def _planner_result(events_path: Path, response_path: Path, stderr_path: Path,
                    requested_model: str = MODELS["planner"]) -> dict:
    events = []
    try:
        for line in events_path.read_text(encoding="utf-8").split("\n"):
            if line.strip():
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ModelError("Codex returned an unexpected JSONL event")
                events.append(event)
    except (ValueError, UnicodeError) as exc:
        raise ModelError("Codex returned invalid JSONL; inspect the events artifact") from exc
    if any(event.get("type") == "turn.failed" for event in events):
        raise ModelError("Codex reported a failed turn; inspect the events artifact")
    if not any(event.get("type") == "turn.completed" for event in events):
        raise ModelError("Codex did not report a completed turn; inspect the events artifact")
    try:
        text = response_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise ModelError("Codex did not save a readable final response") from exc
    if not text:
        raise ModelError("Codex returned an empty final response")
    stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
    banner = re.search(r"(?m)^model:\s*([^\r\n]+)", stderr)
    reported_model = banner.group(1).strip() if banner else None
    if reported_model and reported_model != requested_model:
        raise ModelError("Codex reported a different configured model; inspect stderr")
    return {
        "text": text,
        # Codex exec JSONL does not expose a response-model identity. Its stderr
        # banner (when present) identifies configuration, not the backend response.
        "actual_models": [], "identity_verified": False,
        "reported_model": reported_model, "identity_source": "unavailable",
        "session_id": next((event.get("thread_id") for event in events
                            if event.get("type") == "thread.started"), None),
        "permission_denials": [],
    }


def _worker_result(events_path: Path, requested_model: str = MODELS["worker"],
                   *, structured: bool = False) -> dict:
    try:
        raw = events_path.read_text(encoding="utf-8")
        try:
            decoded = json.loads(raw)
            events = [decoded]
        except json.JSONDecodeError:
            events = [json.loads(line) for line in raw.split("\n") if line.strip()]
    except (ValueError, UnicodeError) as exc:
        raise ModelError("Claude returned invalid JSON/JSONL; inspect the events artifact") from exc
    if not events or any(not isinstance(event, dict) for event in events):
        raise ModelError("Claude returned an unexpected JSON response")
    responses = [event for event in events if event.get("type") == "result"]
    # Accept the previous single-result format for existing artifacts/callers.
    response = responses[-1] if responses else (events[0] if len(events) == 1 else None)
    if not isinstance(response, dict):
        raise ModelError("Claude did not report a final result")
    usage = response.get("modelUsage") or {}
    models = list(usage) if isinstance(usage, dict) else []
    response_models = list(dict.fromkeys(
        event["message"]["model"] for event in events
        if event.get("type") == "assistant" and event.get("parent_tool_use_id") is None
        and isinstance(event.get("message"), dict)
        and isinstance(event["message"].get("model"), str)))
    init = next((event for event in events
                 if event.get("type") == "system" and event.get("subtype") == "init"), {})
    identity_models = response_models or models
    result = {
        "text": (json.dumps(response["structured_output"], ensure_ascii=False)
                 if "structured_output" in response else response.get("result", "")),
        "actual_models": list(dict.fromkeys(models + response_models)),
        "response_models": response_models,
        "reported_model": init.get("model"),
        "identity_verified": identity_models == [requested_model],
        "identity_source": "assistant.message.model" if response_models else "modelUsage",
        "identity_scope": "top-level responses" if response_models else "usage attribution only",
        "session_id": response.get("session_id"),
        "permission_denials": response.get("permission_denials") or [],
    }
    if response.get("is_error") is not False or response.get("subtype", "success") != "success":
        result["error"] = "Claude reported a failed turn; inspect the events artifact"
    elif result["permission_denials"]:
        result["error"] = "Claude denied required permissions; inspect the result artifact"
    elif init.get("model") and init["model"] != requested_model:
        result["error"] = "Claude reported a different configured model; inspect the events artifact"
    elif not result["identity_verified"]:
        result["error"] = "Claude did not unambiguously report the requested exact model"
    elif structured and response.get("structured_output") is None:
        result["error"] = "Claude did not return the requested structured output"
    elif not isinstance(result["text"], str) or not result["text"].strip():
        result["error"] = "Claude returned an empty final response"
    return result


def invoke(role, *, project: Path, prompt: str, artifact: Path, timeout: int = 1200,
           schema: dict | None = None, windows_sandbox: str | None = None,
           provider: str | None = None, model: str | None = None,
           effort: str | None = None) -> dict:
    """Run one turn without model substitution; omitted legacy selections retain max.

    An explicit provider/model with effort=None leaves effort to that CLI's default.
    Claude plan permissions constrain nonworkers but are not an OS read-only sandbox.
    """
    provider, model, effort = _selection(role, provider, model, effort)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ModelError("timeout must be greater than zero")
    if schema is not None and not isinstance(schema, dict):
        raise ModelError("schema must be a JSON Schema object")
    if windows_sandbox not in (None, "elevated", "unelevated"):
        raise ModelError("unsupported Windows sandbox implementation")
    project = Path(project).expanduser().resolve()
    if not project.is_dir():
        raise ModelError("project directory does not exist")
    executable = shutil.which(provider)
    if not executable:
        raise ModelError("{0} CLI is not on PATH".format(provider))
    artifact = Path(artifact).expanduser().resolve()
    artifact.parent.mkdir(parents=True, exist_ok=True)
    prompt_path = _path(artifact, ".prompt.md")
    events_path = _path(artifact, ".events.jsonl")
    stderr_path = _path(artifact, ".stderr.txt")
    response_path = _path(artifact, ".response.json")
    result_path = _path(artifact, ".result.json")
    prompt_path.write_text(prompt, encoding="utf-8")
    access_mode = "workspace-write" if role == "worker" else "read-only"
    if provider == "codex":
        # A retry must not accept a previous run's answer if Codex saves no result.
        response_path.write_text("", encoding="utf-8")
        arguments = [executable, "-a", "never", "exec", "--model", model]
        if effort is not None:
            arguments += ["-c", "model_reasoning_effort=" + effort]
        arguments += ["--sandbox", access_mode,
                     "-C", str(project), "--color", "never", "--skip-git-repo-check",
                     "--json", "--output-last-message", str(response_path)]
        if schema is not None:
            schema_path = _path(artifact, ".schema.json")
            _write_json(schema_path, schema)
            arguments += ["--output-schema", str(schema_path)]
        if windows_sandbox:
            arguments += ["-c", "windows.sandbox=" + windows_sandbox]
        arguments.append("-")
    else:
        arguments = [executable, "-p", "--model", model,
                     "--output-format", "stream-json", "--verbose", "--permission-mode",
                     "auto" if role == "worker" else "plan",
                     "--plugin-dir", str(PLUGIN_ROOT), "--no-session-persistence",
                     "--prompt-suggestions", "false"]
        if effort is not None:
            arguments += ["--effort", effort]
        if role != "worker":
            # Native plan/classifier checks still govern Bash. --tools is an
            # inventory limit, not an approval allowlist; MCP tools are separate.
            arguments += ["--tools", "Read,Glob,Grep,Bash", "--disallowedTools", "mcp__*"]
        if schema is not None:
            arguments += ["--json-schema", json.dumps(schema, ensure_ascii=False)]
    environment = os.environ.copy()
    environment["LABKIT_ROLE"] = role
    options = ({"creationflags": NEW_PROCESS_GROUP | NO_WINDOW}
               if WINDOWS else {"start_new_session": True})
    result = {"role": role, "provider": provider, "requested_model": model,
              "requested_effort": effort, "access_mode": access_mode, "text": "",
              "actual_models": [], "identity_verified": False, "session_id": None,
              "permission_denials": []}
    if provider == "codex":
        result["windows_sandbox"] = windows_sandbox or "host-configured"
        result["permission_enforcement"] = "codex-sandbox"
    else:
        result["permission_enforcement"] = "claude-auto" if role == "worker" else "claude-plan"
        if role != "worker":
            result["capability_limits"] = ["native plan permissions, not an OS read-only sandbox",
                                           "built-in tools: Read, Glob, Grep, Bash; MCP tools denied"]
    try:
        if provider == "claude" and os.getenv("CLAUDECODE"):
            result["native_host_required"] = True
            raise NativeHostRequired("Claude CLI cannot be nested in Claude Code; "
                                     "the controller must dispatch this role through the native host")
        with events_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            try:
                process = subprocess.Popen(arguments, cwd=str(project), env=environment,
                                           stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                                           **options)
            except OSError as exc:
                raise ModelError("Could not start {0} CLI".format(provider)) from exc
            try:
                process.communicate(input=prompt.encode("utf-8"), timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                _terminate_tree(process)
                raise ModelError("{0} timed out after {1}s; process tree terminated".format(
                    role, timeout)) from exc
            except (KeyboardInterrupt, OSError) as exc:
                _terminate_tree(process)
                if isinstance(exc, KeyboardInterrupt):
                    raise
                raise ModelError("{0} process communication failed; process tree terminated".format(
                    role)) from exc
        result["exit_code"] = process.returncode
        if process.returncode:
            raise ModelError("{0} CLI exited with code {1}; inspect {2}".format(
                role, process.returncode, stderr_path))
        result.update(_planner_result(events_path, response_path, stderr_path, model)
                      if provider == "codex" else _worker_result(events_path, model,
                                                                 structured=schema is not None))
        if result.get("error"):
            raise ModelError(result["error"])
        if schema is not None:
            try:
                result["structured_output"] = json.loads(result["text"])
            except (ValueError, TypeError) as exc:
                raise ModelError("CLI did not return parseable structured JSON") from exc
            # The provider CLI validates the supplied JSON Schema; this adapter
            # normalizes the returned value without adding a second validator.
        if provider == "claude":
            response_path.write_text(result["text"], encoding="utf-8")
    except (ModelError, KeyboardInterrupt) as exc:
        result["error"] = str(exc) or "Model invocation interrupted"
        _write_json(result_path, result)
        raise
    _write_json(result_path, result)
    return result


def doctor(providers=None) -> dict:
    """Check selected provider CLIs/auth, never model availability or entitlement."""
    selected = list(DEFAULT_MODELS) if providers is None else list(dict.fromkeys(providers))
    if not selected or any(provider not in DEFAULT_MODELS for provider in selected):
        raise ModelError("providers must contain codex and/or claude")
    result = {"providers": {}, "model_access_verified": False}
    options = {"creationflags": NO_WINDOW} if WINDOWS else {}
    for command in selected:
        executable = shutil.which(command)
        info = {"cli": command, "installed": bool(executable), "version": None,
                "auth_ready": False, "auth_method": None, "model_access_verified": False}
        result["providers"][command] = info
        if not executable:
            continue
        try:
            version = subprocess.run([executable, "--version"], stdin=subprocess.DEVNULL,
                                     capture_output=True,
                                     encoding="utf-8", errors="replace", timeout=30, **options)
            match = re.search(r"\b\d+\.\d+\.\d+\b", version.stdout)
            info["version"] = match.group() if match else None
            args = ["auth", "status", "--json"] if command == "claude" else ["login", "status"]
            auth = subprocess.run([executable] + args, stdin=subprocess.DEVNULL, capture_output=True,
                                  encoding="utf-8", errors="replace", timeout=30, **options)
            if command == "claude":
                status = json.loads(auth.stdout)
                if not isinstance(status, dict):
                    raise ValueError("unexpected auth response")
                info["auth_ready"] = auth.returncode == 0 and status.get("loggedIn") is True
                info["auth_method"] = status.get("authMethod")
                info["api_provider"] = status.get("apiProvider")
            else:
                output = auth.stdout + auth.stderr
                info["auth_ready"] = auth.returncode == 0 and "Logged in" in output
                if info["auth_ready"]:
                    info["auth_method"] = "chatgpt" if "ChatGPT" in output else "api_key"
        except (OSError, ValueError, subprocess.TimeoutExpired):
            info["error"] = "CLI status could not be read"
    for role in ("planner", "worker"):
        command = ROLE_PROVIDERS[role]
        if command in result["providers"]:
            result[role] = {**result["providers"][command], "requested_model": MODELS[role]}
    result["ready"] = all(info["installed"] and info["auth_ready"]
                          for info in result["providers"].values())
    return result


def catalog(providers=None) -> dict:
    """Return provider status and editable suggestions, not an entitlement catalog."""
    health = doctor(providers)
    return {"providers": health["providers"], "ready": health["ready"],
            "candidates": [{"provider": provider, "model": DEFAULT_MODELS[provider],
                            "source": "adapter default suggestion", "model_access_verified": False}
                           for provider in health["providers"]],
            "complete": False, "model_access_verified": False, "custom_model_ids": True,
            "note": "Suggestions are not a complete model list; CLI authentication does not prove model access."}
