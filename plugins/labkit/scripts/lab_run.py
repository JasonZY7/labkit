#!/usr/bin/env python3
"""Durable, user-selected multi-model goal / execution / review controller.

run --directory PROJECT --goal-file GOAL.json --team-file TEAM.json
resume RUN_DIRECTORY (--reuse-team | --team-file TEAM.json)
status RUN_DIRECTORY | doctor | catalog
Exit 0 complete/healthy; 1 interrupted/error; 2 blocked; 3 paused; 4 native work.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
import uuid
from pathlib import Path

import lab_models
import lab_run_legacy as legacy
from lab_mem import _atomic_write

ROOT = Path(__file__).resolve().parent.parent
now, save, project_lock, positive = legacy.now, legacy.save, legacy.project_lock, legacy.positive


def object_schema(properties):
    return {"type": "object", "additionalProperties": False,
            "properties": properties, "required": list(properties)}


TEXT = {"type": "string"}
STRINGS = {"type": "array", "items": TEXT}
PLAN_SCHEMA = object_schema({
    "action": {"type": "string", "enum": ["execute", "verify", "blocked"]},
    "summary": TEXT, "worker": TEXT, "task": TEXT, "acceptance": STRINGS,
})
REVIEW_SCHEMA = object_schema({
    "verdict": {"type": "string", "enum": ["pass", "fail", "blocked"]},
    "summary": TEXT, "issues": STRINGS,
    "criteria": {"type": "array", "items": object_schema({
        "id": TEXT, "passed": {"type": "boolean"}, "evidence": STRINGS})},
})
SKIP_DIRS = {".git", ".labkit-local", "node_modules", ".venv", "venv", "__pycache__"}


def nonempty(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(name + " must be nonempty text")
    return value


def strings(value, name, required=False):
    if not isinstance(value, list) or (required and not value):
        raise ValueError(name + " must be a list" + (" with at least one item" if required else ""))
    for item in value:
        nonempty(item, name)
    return value


def validate_team(team):
    if not isinstance(team, dict) or set(team) != {"selection", "brains", "workers"}:
        raise ValueError("team requires selection (the user's answer), brains, workers")
    nonempty(team["selection"], "user model selection")
    ids = set()
    for group in ("brains", "workers"):
        if not isinstance(team[group], list) or not team[group]:
            raise ValueError("select at least one " + group)
        for member in team[group]:
            if not isinstance(member, dict) or not {"id", "provider", "model"} <= set(member) or set(member) - {"id", "provider", "model", "effort"}:
                raise ValueError("member requires id/provider/model and optional effort")
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", str(member["id"])) or member["id"] in ids:
                raise ValueError("member ids must be unique lowercase identifiers")
            ids.add(member["id"])
            if member["provider"] not in ("codex", "claude"):
                raise ValueError("supported providers: codex, claude")
            model = nonempty(member["model"], "exact model id")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", model):
                raise ValueError("invalid exact model id")
            if model in ("opus", "sonnet", "haiku", "fable", "default", "inherit"):
                raise ValueError("use an exact model id, not a moving alias")
            if member.get("effort") is not None and member["effort"] not in ("minimal", "low", "medium", "high", "xhigh", "max", "ultra"):
                raise ValueError("invalid effort")
    return team


def project_file(project, relative):
    nonempty(relative, "deliverable path")
    value = Path(relative)
    if value.is_absolute() or ".." in value.parts:
        raise ValueError("deliverables must be relative paths within the project")
    resolved = (project / value).resolve()
    if not resolved.is_relative_to(project) or resolved == project or resolved.is_relative_to(project / "handoffs" / "runs"):
        raise ValueError("deliverables must be project files outside controller records")
    return resolved


def validate_goal(goal, project):
    if not isinstance(goal, dict) or set(goal) != {"objective", "acceptance", "deliverables"}:
        raise ValueError("goal requires objective, acceptance, deliverables")
    nonempty(goal["objective"], "objective")
    if not isinstance(goal["acceptance"], list) or not goal["acceptance"]:
        raise ValueError("goal needs explicit acceptance criteria")
    ids = set()
    for criterion in goal["acceptance"]:
        if not isinstance(criterion, dict) or set(criterion) != {"id", "text"}:
            raise ValueError("each criterion requires id and text")
        nonempty(criterion["id"], "criterion id")
        nonempty(criterion["text"], "criterion text")
        if criterion["id"] in ids:
            raise ValueError("duplicate criterion id")
        ids.add(criterion["id"])
    strings(goal["deliverables"], "deliverables", required=True)
    if len(set(goal["deliverables"])) != len(goal["deliverables"]):
        raise ValueError("duplicate deliverable")
    for path in goal["deliverables"]:
        project_file(project, path)
    return goal


def load(run_dir):
    state = legacy.load(run_dir)
    if state["format"] == 2:
        validate_team(state["team"])
        validate_goal(state["goal"], Path(state["project"]))
        original_goal = json.loads((Path(run_dir) / "goal.json").read_text(encoding="utf-8"))
        if state["goal"] != original_goal:
            raise ValueError("run goal differs from its frozen goal.json; start a new goal for scope changes")
    return state


def snapshot(project, deliverables):
    """Detect ordinary concurrent edits via file metadata, and hash every deliverable.

    This is a stability check, not a sandbox or an adversarial integrity guarantee.
    Model caches, dependency trees and controller-owned records are excluded.
    """
    entries = []
    for directory, dirs, files in os.walk(project, followlinks=False):
        relative = Path(directory).relative_to(project)
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and
                         not (relative == Path("handoffs") and d == "runs") and
                         not (relative == Path(".claude") and d == "agents") and
                         not (Path(directory) / d).is_symlink() and
                         not getattr((Path(directory) / d).lstat(), "st_file_attributes", 0) & 0x0400)
        for name in sorted(files):
            path = Path(directory) / name
            if path.is_symlink() or name == ".labkit-run.lock":
                continue
            stat = path.stat()
            entries.append((path.relative_to(project).as_posix(), stat.st_size, stat.st_mtime_ns))
    hashes = {}
    missing = []
    for relative in deliverables:
        path = project_file(project, relative)
        if not path.is_file():
            missing.append(relative)
            continue
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        hashes[relative] = digest.hexdigest()
    return {"revision": hashlib.sha256(json.dumps(entries).encode()).hexdigest(),
            "deliverables": hashes, "missing": missing}


def parse_response(text, schema):
    value = json.loads(text)
    if not isinstance(value, dict) or set(value) != set(schema["required"]):
        raise ValueError("response must contain exactly the supplied schema fields")
    return value


def native_json(text):
    """Native Plan can add its required prose/footer around one JSON block.

    Keep the attested text intact. Extract only an unambiguous labelled block,
    then apply the exact same schema and goal validators as direct CLI JSON.
    """
    try:
        json.loads(text)
        return text
    except ValueError:
        blocks = re.findall(r"(?m)^```json[ \t]*\r?\n(.*?)^```[ \t]*$", text, flags=re.DOTALL)
        if len(blocks) != 1:
            raise ValueError("native planner/reviewer must return JSON or exactly one labelled JSON block")
        return blocks[0].strip()


def plan_from(text, team):
    value = parse_response(text, PLAN_SCHEMA)
    if value["action"] not in ("execute", "verify", "blocked"):
        raise ValueError("invalid planner action")
    nonempty(value["summary"], "planner summary")
    for key in ("worker", "task"):
        if not isinstance(value[key], str):
            raise ValueError(key + " must be text")
    strings(value["acceptance"], "task acceptance", value["action"] == "execute")
    if value["action"] == "execute":
        if value["worker"] not in {m["id"] for m in team["workers"]}:
            raise ValueError("planner selected a worker outside the user's team")
        nonempty(value["task"], "bounded worker task")
    return value


def review_from(text, goal):
    value = parse_response(text, REVIEW_SCHEMA)
    if value["verdict"] not in ("pass", "fail", "blocked"):
        raise ValueError("invalid review verdict")
    nonempty(value["summary"], "review summary")
    strings(value["issues"], "review issues")
    if not isinstance(value["criteria"], list):
        raise ValueError("review criteria must be a list")
    ids = []
    for criterion in value["criteria"]:
        if not isinstance(criterion, dict) or set(criterion) != {"id", "passed", "evidence"}:
            raise ValueError("each review criterion requires id/passed/evidence")
        if not isinstance(criterion["passed"], bool):
            raise ValueError("criterion passed must be boolean")
        nonempty(criterion["id"], "review criterion id")
        ids.append(criterion["id"])
        strings(criterion["evidence"], "independently checked evidence", True)
    if sorted(ids) != sorted(c["id"] for c in goal["acceptance"]):
        raise ValueError("review must cover every goal criterion exactly once")
    all_passed = all(c["passed"] for c in value["criteria"])
    if value["verdict"] == "pass" and (not all_passed or value["issues"]):
        raise ValueError("passing review cannot contain failed criteria or unresolved issues")
    if value["verdict"] != "pass" and not value["issues"]:
        raise ValueError("failed or blocked review must explain concrete unresolved issues")
    return value


def context(state, run_dir, role=None):
    # Full outputs remain in numbered artifacts. Keep context bounded on long runs.
    hidden = {item["artifact"] for item in state["reviews"]} if role == "reviewer" else set()
    history = [item for item in state["history"] if item.get("artifact") not in hidden]
    return json.dumps({"goal": state["goal"], "team": state["team"],
                       "run_directory": str(run_dir), "recent_history": history[-16:],
                       "plan": state.get("plan"), "advice": state["advice"],
                       "previous_reviews": state.get("previous_reviews", []),
                       "interruption": state.get("interruption"),
                       "gate_feedback": state.get("gate_feedback", [])}, ensure_ascii=False, indent=2)


def prompt_for(role, member, state, run_dir, token):
    instructions = {
        "advisor": "Independently analyze the goal, actual project and recent results. Propose the next bounded task, acceptance checks and risks of missing scope. Do not edit project files. Give concrete advice to the lead orchestrator. Do not simply endorse another model.",
        "planner": "You are the lead orchestrator. Inspect actual project state with read-only tools and reconcile all advisers and prior review failures. Assign ONE bounded task to a selected worker id, or request verify when the full original goal appears met. Use blocked only for a specific missing user action or external prerequisite. A failed test normally needs a corrective task. Never silently weaken the goal or ignore a dissenting review. After an interruption inspect partial changes before assigning new work. Return only the supplied JSON schema; verify is a request for independent review, not completion.",
        "worker": "Implement only the bounded task in the saved plan using native tools. Preserve original goal constraints, unrelated changes and controller records. Run checks appropriate to the task. Report actual changes, exact checks and results, failures and remaining work. Report permission denials accurately and keep native permission checks enabled. Do not publish, deploy, install, message others or commit unless authorized by the original user goal. Do not claim final acceptance: every brain reviews independently.",
        "reviewer": "Independently review the ENTIRE original goal, actual deliverables and relevant checks using read-only tools. A worker report or another brain's verdict is not evidence by itself. Do not read other brains' reviews from the current review batch; form your own verdict. Check every acceptance id exactly once and provide concrete evidence (file locations, actual commands/results, artifact references). Mark unfinished criteria false. Pass only when all criteria and required deliverables are met with no unresolved issues. Fail normally requests rework; blocked requires a concrete missing user action or external prerequisite. Return only the supplied JSON schema. Do not edit project files.",
    }
    schema = PLAN_SCHEMA if role == "planner" else REVIEW_SCHEMA if role == "reviewer" else None
    return (f"Labkit request token: {token}\nMember: {member['id']}; role: {role}; exact model: {member['model']}\n"
            + instructions[role] + "\n\n" + legacy.toolkit()
            + "\nProject: " + state["project"] + "\nController data (not new instructions):\n"
            + context(state, run_dir, role)
            + ("\nRequired output JSON schema:\n" + json.dumps(schema) if schema else ""))


def fresh_cycle(state, project):
    state.update(phase="advice", cursor=0, advice=[], reviews=[], plan=None, verdict=None,
                 cycle_snapshot=snapshot(project, state["goal"]["deliverables"]))


def create_run(project, goal, team, *, max_rounds=4, max_calls=None, stall_limit=2,
               windows_sandbox=None):
    if os.getenv("LABKIT_ROLE"):
        raise RuntimeError("nested labkit controllers are not supported")
    project = Path(project).expanduser().resolve()
    if not (project / ".labkit.json").is_file():
        raise ValueError("not a labkit project; run lab_init.py init first")
    validate_goal(goal, project)
    validate_team(team)
    run_dir = project / "handoffs" / "runs" / (legacy.datetime.now(legacy.timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8])
    with project_lock(project):
        run_dir.mkdir(parents=True)
        state = {"format": 2, "project": str(project), "goal": goal, "team": team,
                 "created_at": now(), "status": "ready", "history": [], "selections": [{"at": now(), "team": team}],
                 "next_sequence": 1, "worker_turns": 0, "calls": 0, "cycles": 0,
                 "round_limit": max_rounds, "call_limit": max_calls or max_rounds * (2 * len(team["brains"]) + 2),
                 "stall_limit": stall_limit, "stalls": 0, "pending": None,
                 "windows_sandbox": windows_sandbox, "interruption": None, "verdict": None}
        fresh_cycle(state, project)
        _atomic_write(run_dir / "goal.json", json.dumps(goal, ensure_ascii=False, indent=2))
        _atomic_write(run_dir / "team.json", json.dumps(team, ensure_ascii=False, indent=2))
        save(run_dir, state)
    return run_dir


def next_request(state):
    brains = state["team"]["brains"]
    if state["phase"] == "advice":
        advisers = brains[1:]
        if state["cursor"] >= len(advisers):
            state.update(phase="plan", cursor=0)
            return next_request(state)
        return "advisor", advisers[state["cursor"]]
    if state["phase"] == "plan":
        return "planner", brains[0]
    if state["phase"] == "work":
        return "worker", next(m for m in state["team"]["workers"] if m["id"] == state["plan"]["worker"])
    if state["phase"] == "review":
        return "reviewer", brains[state["cursor"]]
    raise ValueError("invalid controller phase")


def accept_result(state, run_dir, result):
    # Keep the durable pending request until every fallible transition step succeeds.
    # In particular, a failed post-worker snapshot must never leave phase=work with
    # pending cleared: that would allow a non-idempotent task to run twice.
    current_state = state
    state = copy.deepcopy(state)
    pending = state["pending"]
    role = pending["role"]
    text = nonempty(result["text"], "model response")
    structured_text = native_json(text) if pending.get("mode") == "host" and role in ("planner", "reviewer") else text
    value = plan_from(structured_text, state["team"]) if role == "planner" else review_from(structured_text, state["goal"]) if role == "reviewer" else None
    if pending.get("mode") == "host" and value is not None:
        result = {**result, "structured_output": value}
        _atomic_write(run_dir / (pending["artifact"] + ".result.json"), json.dumps(result, ensure_ascii=False, indent=2))
    record = {"role": role, "member": pending["member"]["id"], "artifact": pending["artifact"],
              "summary": text[:12000], "requested_model": pending["member"]["model"],
              "provider": pending["member"]["provider"], "actual_models": result.get("actual_models", []),
              "identity_verified": result.get("identity_verified", False)}
    state["history"].append(record)
    state["pending"] = None
    state["status"] = "running"
    project = Path(state["project"])
    if role == "advisor":
        state["advice"].append(record)
        state["cursor"] += 1
    elif role == "planner":
        state["plan"] = value
        state["cycles"] += 1
        if value["action"] == "blocked":
            state.update(status="blocked", verdict=value)
        elif value["action"] == "execute":
            state["phase"] = "work"
        else:
            state.update(phase="review", cursor=0, reviews=[], review_snapshot=snapshot(project, state["goal"]["deliverables"]))
    elif role == "worker":
        state.update(phase="review", cursor=0, reviews=[], interruption=None,
                     review_snapshot=snapshot(project, state["goal"]["deliverables"]))
    else:
        state["reviews"].append({"member": record["member"], "artifact": record["artifact"], **value})
        state["cursor"] += 1
    save(run_dir, state)
    current_state.clear()
    current_state.update(state)


def finalize_reviews(state, run_dir):
    current = snapshot(Path(state["project"]), state["goal"]["deliverables"])
    reviews = state["reviews"]
    if [r.get("member") for r in reviews] != [m["id"] for m in state["team"]["brains"]]:
        raise ValueError("completion requires one review from every current brain, in dispatch order")
    for item in reviews:
        review_from(json.dumps({key: item[key] for key in REVIEW_SCHEMA["required"]}), state["goal"])
    feedback = []
    if current != state["review_snapshot"]:
        feedback.append("Project changed during independent review; all verdicts need a fresh review.")
    if current["missing"]:
        feedback.append("Missing required deliverables: " + ", ".join(current["missing"]))
    passed = all(r["verdict"] == "pass" for r in reviews)
    if passed and not feedback:
        state.update(status="complete", verdict={"summary": "Every selected brain passed every goal criterion; required deliverables exist and remained stable during review.",
                                                 "reviews": reviews, "snapshot": current})
        delivery = {"goal": state["goal"], "team": state["team"], "completed_at": now(),
                    "verdict": state["verdict"], "calls": state["calls"], "worker_turns": state["worker_turns"]}
        _atomic_write(run_dir / "delivery.json", json.dumps(delivery, ensure_ascii=False, indent=2))
        lines = ["# Delivery", "", state["goal"]["objective"], "", "## Deliverables", ""]
        lines += [f"- {p} (SHA256 `{h}`)" for p, h in current["deliverables"].items()]
        lines += ["", "## Independent reviews", ""]
        lines += [f"- {r['member']}: {r['summary']} ({r['artifact']}.result.json)" for r in reviews]
        _atomic_write(run_dir / "delivery.md", "\n".join(lines) + "\n")
    elif current == state["review_snapshot"] and any(r["verdict"] == "blocked" for r in reviews):
        state.update(status="blocked", verdict={"summary": "Review requires user action or an external prerequisite.", "reviews": reviews, "gate_feedback": feedback})
    else:
        state["stalls"] = state["stalls"] + 1 if current == state["cycle_snapshot"] else 0
        state["previous_reviews"] = reviews
        state["gate_feedback"] = feedback
        fresh_cycle(state, Path(state["project"]))
        if state["stalls"] >= state["stall_limit"]:
            state.update(status="paused", pause_reason="no_progress")
    save(run_dir, state)


def host_request(state, run_dir):
    pending = state["pending"]
    workflow_file = run_dir / (pending["artifact"] + ".workflow.json")
    if not workflow_file.exists():
        inputs = {"prompt": (run_dir / (pending["artifact"] + ".prompt.md")).read_text(encoding="utf-8"),
                  "model": pending["member"]["model"], "effort": pending["member"].get("effort"),
                  "agent_type": "general-purpose" if pending["role"] == "worker" else "Plan",
                  "label": pending["artifact"]}
        # A scriptPath keeps long prompts out of host tool-call JSON. The host
        # transports a short path instead of reconstructing escaped conversation.
        script_file = run_dir / (pending["artifact"] + ".workflow.js")
        script = ("export const meta={name:'labkit-native-turn',description:'Execute one selected model turn',phases:[{title:'Turn'}]};\n"
                  + "const input=" + json.dumps(inputs, ensure_ascii=True) + ";\n"
                  + "if(!input?.prompt || !input?.model || !input?.agent_type) throw new Error('Missing exact model or prompt');\n"
                  + "phase('Turn'); return await agent(input.prompt,{model:input.model,agentType:input.agent_type,...(input.effort ? {effort:input.effort} : {}),label:input.label});\n")
        _atomic_write(script_file, script)
        _atomic_write(workflow_file, json.dumps({"scriptPath": str(script_file)}, ensure_ascii=False, indent=2))
    print(json.dumps({"status": "awaiting_host", "role": pending["role"], "member": pending["member"],
                      "artifact": pending["artifact"], "token": pending["token"],
                      "workflow_file": str(workflow_file),
                      "prompt_file": str(run_dir / (pending["artifact"] + ".prompt.md")),
                      "instruction": "Use a native Claude subagent with the requested exact model; verify its runtime transcript when submitting. Resume returns this same request without dispatching it again."}, ensure_ascii=False, indent=2), flush=True)


def dispatch(state, run_dir, timeout):
    role, member = next_request(state)
    artifact = f"{state['next_sequence']:03d}-{role}-{member['id']}"
    token = uuid.uuid4().hex
    prompt = prompt_for(role, member, state, run_dir, token)
    schema = PLAN_SCHEMA if role == "planner" else REVIEW_SCHEMA if role == "reviewer" else None
    state["next_sequence"] += 1
    state["calls"] += 1
    if role == "worker":
        state["worker_turns"] += 1
    state["pending"] = {"role": role, "member": member, "artifact": artifact, "token": token,
                        "started_at": now(), "phase": state["phase"]}
    _atomic_write(run_dir / (artifact + ".prompt.md"), prompt)
    if schema:
        _atomic_write(run_dir / (artifact + ".schema.json"), json.dumps(schema))
    if member["provider"] == "claude" and os.getenv("CLAUDECODE"):
        state["pending"]["mode"] = "host"
        state["status"] = "awaiting_host"
        save(run_dir, state)
        host_request(state, run_dir)
        return 4
    state["pending"]["mode"] = "cli"
    save(run_dir, state)
    print(f"{role} {member['id']}: {member['provider']}/{member['model']} ({artifact})", flush=True)
    result = lab_models.invoke(role, provider=member["provider"], model=member["model"], effort=member.get("effort"),
                               project=Path(state["project"]), prompt=prompt, artifact=run_dir / artifact,
                               timeout=timeout, schema=schema, windows_sandbox=state.get("windows_sandbox"))
    accept_result(state, run_dir, result)
    return None


def advance(run_dir, *, timeout=1200, resume=False, team=None, max_rounds=None,
            max_calls=None, reset_stall=False, windows_sandbox=None):
    if os.getenv("LABKIT_ROLE"):
        raise RuntimeError("nested labkit controllers are not supported")
    run_dir = Path(run_dir).resolve()
    state = load(run_dir)
    if state["status"] == "cancelled":
        raise ValueError("cancelled runs retain their records but cannot be resumed; start a new goal")
    if state["format"] == 1:
        if team is not None:
            raise ValueError("legacy run has fixed roles; finish it with --reuse-team before starting a configurable run")
        return legacy.advance(run_dir, timeout=timeout, max_rounds=max_rounds or 4, windows_sandbox=windows_sandbox)
    with project_lock(Path(state["project"]), run_dir):
        state = load(run_dir)
        if state["status"] == "complete":
            return 0
        if state["status"] == "awaiting_host" or (state.get("pending") or {}).get("mode") == "host":
            if team is not None and team != state["team"]:
                raise ValueError("cannot change team while an exact native request is pending")
            if state["status"] != "awaiting_host":
                state["status"] = "awaiting_host"
                save(run_dir, state)
            host_request(state, run_dir)
            return 4
        if team is not None:
            validate_team(team)
            if team != state["team"]:
                if state.get("pending"):
                    raise ValueError("recover interrupted work with the existing team before changing models")
                state["team"] = team
                fresh_cycle(state, Path(state["project"]))
        if resume:
            state["selections"].append({"at": now(), "team": state["team"], "reuse": team is None})
            if state["status"] in ("paused", "blocked"):
                state["round_limit"] = state["cycles"] + (max_rounds or 4)
                state["call_limit"] = state["calls"] + (max_calls or (max_rounds or 4) * (2 * len(state["team"]["brains"]) + 2))
                if state.get("pause_reason") == "no_progress" and not reset_stall:
                    print("No-progress pause: inspect the saved reviews and use --reset-stall only after choosing a new approach.")
                    save(run_dir, state)
                    return 3
                if reset_stall:
                    state["stalls"] = 0
                if state["status"] == "blocked":
                    fresh_cycle(state, Path(state["project"]))
        if state.get("pending"):
            state["interruption"] = {"pending": state["pending"], "note": "Previous process ended before confirming this turn. Inspect partial changes and the original artifacts; do not blindly replay work."}
            state["history"].append({"role": "interruption", "summary": state["interruption"]["note"], "pending": state["pending"]})
            state["pending"] = None
            fresh_cycle(state, Path(state["project"]))
        if windows_sandbox:
            state["windows_sandbox"] = windows_sandbox
        state.pop("pause_reason", None)
        state["status"] = "running"
        save(run_dir, state)
        try:
            while state["status"] == "running":
                if state["phase"] == "review" and state["cursor"] == len(state["team"]["brains"]):
                    finalize_reviews(state, run_dir)
                    continue
                if state["calls"] >= state["call_limit"] or (state["phase"] in ("advice", "plan") and state["cycles"] >= state["round_limit"]):
                    state.update(status="paused", pause_reason="budget")
                    save(run_dir, state)
                    break
                code = dispatch(state, run_dir, timeout)
                if code is not None:
                    return code
            print(json.dumps({"status": state["status"], "verdict": state.get("verdict"), "pause_reason": state.get("pause_reason")}, ensure_ascii=False), flush=True)
            return {"complete": 0, "blocked": 2, "paused": 3}[state["status"]]
        except (lab_models.ModelError, ValueError, OSError, KeyboardInterrupt) as exc:
            status = "awaiting_host" if (state.get("pending") or {}).get("mode") == "host" else "interrupted"
            state.update(status=status, interruption={"pending": state.get("pending"), "error": type(exc).__name__ + ": " + str(exc)})
            save(run_dir, state)
            print("Run interrupted; exact artifacts and consumed budget retained: " + str(exc), file=sys.stderr)
            return 1


def submit(run_dir, report, *, artifact, transcript_file, timeout=1200):
    """Accept only the exact native request's runtime-backed final output."""
    import lab_native
    if os.getenv("LABKIT_ROLE"):
        raise RuntimeError("nested labkit controllers are not supported")
    run_dir = Path(run_dir).resolve()
    state = load(run_dir)
    if state["format"] != 2:
        raise ValueError("legacy reports use lab_run_legacy.py submit")
    with project_lock(Path(state["project"]), run_dir):
        state = load(run_dir)
        pending = state.get("pending") or {}
        if state["status"] != "awaiting_host" or pending.get("mode") != "host":
            raise ValueError("this run is not awaiting a native host report")
        if artifact != pending.get("artifact"):
            raise ValueError("stale native report: artifact does not match the pending request")
        result = lab_native.verify(pending, report, transcript_file)
        _atomic_write(run_dir / (artifact + ".result.json"), json.dumps(result, ensure_ascii=False, indent=2))
        accept_result(state, run_dir, result)
    return advance(run_dir, timeout=timeout)


def cancel(run_dir, reason):
    """Release a logical reservation while preserving all execution evidence.

    The native host must stop any outstanding Workflow before calling this command.
    The OS lock still prevents cancelling a live CLI controller process.
    """
    if os.getenv("LABKIT_ROLE"):
        raise RuntimeError("nested labkit controllers are not supported")
    nonempty(reason, "cancellation reason")
    run_dir = Path(run_dir).resolve()
    state = load(run_dir)
    with project_lock(Path(state["project"]), run_dir, check_reservations=False):
        state = load(run_dir)
        if state["status"] == "complete":
            raise ValueError("completed runs retain their completion record")
        state["status"] = "cancelled"
        state["history"].append({"role": "cancellation", "summary": reason, "at": now(),
                                 "pending": state.get("pending")})
        save(run_dir, state)
    print("Cancelled; records preserved: " + str(run_dir))
    return 0


def recover(run_dir, *, artifact, reason, workflow_stopped=False, timeout=1200):
    """Return an interrupted native attempt to planning, without accepting its work."""
    if os.getenv("LABKIT_ROLE"):
        raise RuntimeError("nested labkit controllers are not supported")
    nonempty(reason, "recovery evidence and reason")
    if not workflow_stopped:
        raise ValueError("first stop or confirm completion of the native Workflow; then pass --workflow-stopped")
    run_dir = Path(run_dir).resolve()
    state = load(run_dir)
    with project_lock(Path(state["project"]), run_dir):
        state = load(run_dir)
        pending = state.get("pending") or {}
        if state["status"] != "awaiting_host" or pending.get("mode") != "host":
            raise ValueError("this run is not awaiting a native request")
        if artifact != pending.get("artifact"):
            raise ValueError("stale recovery: artifact does not match the pending request")
        next_state = copy.deepcopy(state)
        next_state["interruption"] = {"pending": pending, "note": reason,
                                      "stop_source": "host-reported Workflow stop/completion; not independently attested"}
        next_state["history"].append({"role": "interruption", **next_state["interruption"]})
        fresh_cycle(next_state, Path(state["project"]))
        next_state.update(pending=None, status="interrupted")
        save(run_dir, next_state)
    return advance(run_dir, timeout=timeout)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("run")
    start.add_argument("--directory", required=True)
    start.add_argument("--goal-file", required=True)
    start.add_argument("--team-file", required=True)
    start.add_argument("--stall-limit", type=positive, default=2)
    resume_parser = commands.add_parser("resume")
    resume_parser.add_argument("run_directory")
    choice = resume_parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--reuse-team", action="store_true")
    choice.add_argument("--team-file")
    resume_parser.add_argument("--reset-stall", action="store_true")
    for command in (start, resume_parser):
        command.add_argument("--max-rounds", type=positive)
        command.add_argument("--max-calls", type=positive)
        command.add_argument("--timeout", type=positive, default=1200)
        command.add_argument("--windows-sandbox", choices=["elevated", "unelevated"])
    status = commands.add_parser("status")
    status.add_argument("run_directory")
    report = commands.add_parser("submit")
    report.add_argument("run_directory")
    report.add_argument("--artifact", required=True)
    report.add_argument("--report-file", required=True)
    report.add_argument("--transcript-file", required=True)
    report.add_argument("--timeout", type=positive, default=1200)
    cancel_parser = commands.add_parser("cancel")
    cancel_parser.add_argument("run_directory")
    cancel_parser.add_argument("--reason", required=True)
    recovery = commands.add_parser("recover")
    recovery.add_argument("run_directory")
    recovery.add_argument("--artifact", required=True)
    recovery.add_argument("--reason", required=True)
    recovery.add_argument("--workflow-stopped", action="store_true", required=True)
    recovery.add_argument("--timeout", type=positive, default=1200)
    commands.add_parser("doctor")
    commands.add_parser("catalog")
    args = parser.parse_args()
    try:
        if args.command in ("doctor", "catalog"):
            value = lab_models.doctor() if args.command == "doctor" else lab_models.catalog()
            print(json.dumps(value, ensure_ascii=False, indent=2))
            return 0 if args.command == "catalog" or value["ready"] else 1
        if args.command == "status":
            state = load(Path(args.run_directory).resolve())
            print(json.dumps(state, ensure_ascii=False, indent=2))
            return 0
        if args.command == "cancel":
            return cancel(args.run_directory, args.reason)
        if args.command == "recover":
            return recover(args.run_directory, artifact=args.artifact, reason=args.reason,
                           workflow_stopped=args.workflow_stopped, timeout=args.timeout)
        if args.command == "submit":
            return submit(args.run_directory, Path(args.report_file).read_text(encoding="utf-8-sig"),
                          artifact=args.artifact, transcript_file=args.transcript_file, timeout=args.timeout)
        team = json.loads(Path(args.team_file).read_text(encoding="utf-8-sig")) if args.team_file else None
        if args.command == "run":
            goal = json.loads(Path(args.goal_file).read_text(encoding="utf-8-sig"))
            run_dir = create_run(args.directory, goal, team, max_rounds=args.max_rounds or 4,
                                 max_calls=args.max_calls, stall_limit=args.stall_limit,
                                 windows_sandbox=args.windows_sandbox)
            print("Run directory: " + str(run_dir), flush=True)
            return advance(run_dir, timeout=args.timeout)
        return advance(args.run_directory, timeout=args.timeout, resume=True, team=team,
                       max_rounds=args.max_rounds, max_calls=args.max_calls,
                       reset_stall=args.reset_stall, windows_sandbox=args.windows_sandbox)
    except (ValueError, OSError, RuntimeError, KeyError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
