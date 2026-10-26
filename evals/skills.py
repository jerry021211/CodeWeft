"""Skill smoke eval: python -m evals.skills --mode offline|live.

Live runs use configured credentials. Each case runs in a fresh subprocess with
only skill loading, guarded reads and (repair cases only) a single writable file
and fixed test command. No shell, MCP, memory, delegation or external tracing.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, UTC
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from uuid import uuid4

from codeagent import Agent, AgentConfig, ModelResponse, ToolRegistry
from codeagent.context import ContextConfig, ContextManager
from codeagent.events import TokenUsage, UsageTracker
from codeagent.hooks.loop_guard import LoopGuardConfig
from codeagent.prompts import PromptRuntime
from codeagent.recovery import RecoveryConfig, RecoveryRuntime
from codeagent.runtime.execution import ExecutionStopped
from codeagent.skills import SkillLoader
from codeagent.skills.bundled import bundled_root
from codeagent.tools import LoadSkillTool
from codeagent.tools.base import ToolDefinition, ToolOutput
from codeagent.tools.read import ReadFileTool
from codeagent.tools.workspace import WorkspaceGuard
from codeagent.tools.write import WriteFileTool
from evals.agent_adapter import append_record
from evals.skills_cases import CORRECT, CHECKS, Case, cases


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def run_checks(workspace: Path) -> ToolOutput:
    # Execute host-owned tests, never a model-editable test file or shell command.
    script = "import sys, unittest\nsys.path.insert(0, sys.argv[1])\n" + CHECKS
    script += "\nunittest.main(argv=['checks'])\n"
    process = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(workspace)],
                             capture_output=True, text=True, timeout=5)
    return ToolOutput(process.stdout + process.stderr, exit_code=process.returncode,
                      status="success" if process.returncode == 0 else "error")


def grade(case: Case, trace: list[dict], answer: str, workspace: Path, stop_reason: str) -> dict:
    loaded = [row["args"].get("name") for row in trace
              if row["tool"] == "load_skill" and row["status"] == "success"]
    def read_paths(row, status="success"):
        if row["tool"] != "read_file":
            return set()
        # Per-file statuses matter: a batch may succeed with some missing files.
        entries = row.get("read_entries", [{"path": row["args"].get("file_path"), "status": row["status"]}])
        paths = set()
        for entry in entries:
            if entry["status"] != status or not entry["path"]:
                continue
            try:
                relative = (workspace / entry["path"]).resolve().relative_to(workspace.resolve())
            except ValueError:
                continue
            paths.add(relative.as_posix())
        return paths

    reads = set().union(*(read_paths(row) for row in trace))
    routing = loaded == [] if case.skill is None else case.skill in loaded
    extra = [name for name in loaded if name != case.skill]
    checks = {"routing": routing, "no_unrelated_skills": not extra,
              "required_reads": set(case.reads) <= reads,
              "completed": stop_reason == "end_turn",
              # This is a factual smoke check, not semantic answer grading.
              "answer_facts": bool(answer.strip()) and all(f.casefold() in answer.casefold() for f in case.facts)}
    original = case.files
    actual = {str(p.relative_to(workspace)).replace("\\", "/"): p.read_text(encoding="utf-8")
              for p in workspace.rglob("*") if p.is_file()}
    allowed = {"totals.py"} if case.repair else set()
    changes = {name for name in set(original) | set(actual) if original.get(name) != actual.get(name)}
    checks["scope_preserved"] = changes <= allowed
    if case.skill:
        load_index = next((i for i, row in enumerate(trace)
                           if row["tool"] == "load_skill" and row["args"].get("name") == case.skill
                           and row["status"] == "success"), None)
        first_write = next((i for i, row in enumerate(trace) if row["tool"] == "write_file"), len(trace))
        checks["loaded_before_write"] = load_index is not None and load_index < first_write
    if case.repair:
        writes = [i for i, row in enumerate(trace) if row["tool"] == "write_file" and row["status"] == "success"]
        checks["read_before_write"] = bool(writes) and all(
            any(path in read_paths(row)
                for row in trace[:writes[0]]) for path in case.reads)
        checks["failing_baseline_observed"] = bool(writes) and any(
            row["tool"] == "run_fixture_tests" and row["exit_code"] not in (None, 0)
            for row in trace[:writes[0]])
        checks["passing_check_after_write"] = bool(writes) and any(
            row["tool"] == "run_fixture_tests" and row["exit_code"] == 0
            for row in trace[writes[-1] + 1:])
        try:
            checks["independent_behavior_check"] = run_checks(workspace).exit_code == 0
        except (OSError, subprocess.TimeoutExpired):
            checks["independent_behavior_check"] = False
    if case.missing:
        checks["missing_read_observed"] = any(case.missing in read_paths(row, status="error") for row in trace)
        checks["missing_reported"] = any(s in answer.casefold() for s in ("不存在", "缺失", "找不到", "not found", "missing"))
    return {"passed": all(checks.values()), "checks": checks, "loaded": loaded,
            "unexpected_skills": extra, "changed_files": sorted(changes)}


class ScriptedClient:
    """Known-good sequence for wiring only; never a model-quality result."""
    def __init__(self, case: Case):
        actions = []
        if case.skill:
            actions.append(("load_skill", {"name": case.skill}))
        actions.extend(("read_file", {"file_path": path}) for path in case.reads)
        if case.missing:
            actions.append(("read_file", {"file_path": case.missing}))
        if case.repair:
            actions.extend([("run_fixture_tests", {}),
                            ("write_file", {"file_path": "totals.py", "content": CORRECT}),
                            ("run_fixture_tests", {})])
        self.actions = actions
        self.answer = "缺失文件，无法评估。" if case.missing else "Offline fixture: " + " ".join(case.facts)
        self.index = 0

    def create_message(self, **kwargs):
        self.index += 1
        if self.index <= len(self.actions):
            name, args = self.actions[self.index - 1]
            return ModelResponse("tool_use", [{"type": "tool_use", "id": f"offline-{self.index}", "name": name, "input": args}])
        return ModelResponse("end_turn", [{"type": "text", "text": self.answer}])


class BoundedClient:
    def __init__(self, client, trial: Path):
        self.client, self.trial, self.calls = client, trial, 0
        self.usage_tracker = UsageTracker()

    def create_message(self, **kwargs):
        if self.calls >= 8:
            raise ExecutionStopped("budget_exceeded:skill_eval_calls")
        self.calls += 1
        append_record(self.trial / "requests.jsonl", normalize_sdk_blocks({"call": self.calls, **kwargs}))
        try:
            response = self.client.create_message(**kwargs)
        except Exception:
            self.usage_tracker.record(TokenUsage(available=False))
            raise
        self.usage_tracker.record(response.usage or TokenUsage(available=False))
        append_record(self.trial / "responses.jsonl", {"call": self.calls, "content": normalize_sdk_blocks(response.content),
                      "stop_reason": response.stop_reason, "usage": asdict(response.usage) if response.usage else None})
        return response


def normalize_sdk_blocks(value):
    """Convert SDK objects before append_record removes private reasoning blocks."""
    if hasattr(value, "model_dump"):
        value = value.model_dump(exclude_none=True)
    if isinstance(value, dict):
        return {key: normalize_sdk_blocks(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize_sdk_blocks(item) for item in value]
    return value


def usage_from_evidence(trial: Path) -> dict:
    tracker = UsageTracker()
    responses = trial / "responses.jsonl"
    rows = [json.loads(line) for line in responses.read_text(encoding="utf-8").splitlines()] if responses.exists() else []
    for row in rows:
        tracker.record(TokenUsage(**row["usage"]) if row.get("usage") else TokenUsage(available=False))
    requests = trial / "requests.jsonl"
    sent = len(requests.read_text(encoding="utf-8").splitlines()) if requests.exists() else 0
    for _ in range(max(0, sent - len(rows))):
        tracker.record(TokenUsage(available=False))
    return tracker.snapshot().to_dict()


def regrade(source: Path, output: Path) -> Path:
    """Recompute checks and usage without model calls or changing original evidence."""
    source = source.resolve()
    report = json.loads((source / "report.json").read_text(encoding="utf-8"))
    for row in report["cases"]:
        trial = source / f"{row['id']}-r{row['repeat']}"
        if not (trial / "result.json").exists():
            continue
        case = Case(**json.loads((trial / "case.json").read_text(encoding="utf-8")))
        trace = [json.loads(line) for line in (trial / "tools.jsonl").read_text(encoding="utf-8").splitlines()] if (trial / "tools.jsonl").exists() else []
        answer = (trial / "answer.txt").read_text(encoding="utf-8")
        row["original_checks"] = row.get("checks", {})
        row.update(grade(case, trace, answer, trial / "workspace", row["stop_reason"]))
        row["usage"] = usage_from_evidence(trial)
    report["passed"] = sum(row["passed"] for row in report["cases"])
    report["regraded_from"] = str(source)
    report["regrade_note"] = "Normalize equivalent read paths (including failed reads); derive usage from provider responses. Criteria unchanged. No model calls."
    root = output.resolve() / ("skills-regrade-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8])
    root.mkdir(parents=True)
    write_json(root / "report.json", report)
    shutil.copyfile(__file__, root / "scorer.py")
    return root


def worker(case: Case, mode: str, trial: Path, skills_root: Path) -> dict:
    # Load dotenv before turning tracing off; configured tracing must not escape this fixture.
    from codeagent.config import EnvironmentConfig
    env = EnvironmentConfig.from_env() if mode == "live" else None
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    workspace = trial / "workspace"
    workspace.mkdir(parents=True)
    for name, content in case.files.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    write_json(trial / "case.json", asdict(case))
    loader = SkillLoader([skills_root])
    trace: list[dict] = []

    def observe(name, args, handler):
        result = handler(**args)
        row = {"tool": name, "args": args, "status": getattr(result, "status", "success"),
               "exit_code": getattr(result, "exit_code", None), "output": str(result)}
        if getattr(result, "read_batch", None):
            row["read_entries"] = [{"path": path, "status": output.status} for path, output in result.read_batch]
        trace.append(row)
        append_record(trial / "tools.jsonl", row)
        return result

    registry = ToolRegistry(execution_wrapper=observe)
    registry.register(LoadSkillTool(loader))
    guard = WorkspaceGuard(workspace)
    registry.register(ReadFileTool(workspace_guard=guard))
    if case.repair:
        writer = WriteFileTool(workspace_guard=guard)

        def write_target(file_path, content):
            if (workspace / file_path).resolve() != (workspace / "totals.py").resolve():
                return ToolOutput("Only totals.py may be changed.", status="error")
            return writer.run(file_path, content)

        registry.register_handler(writer.definition, write_target)
        registry.register_handler(ToolDefinition(name="run_fixture_tests", description="Run the existing immutable regression checks for totals.py.",
                                  input_schema={"type": "object", "properties": {}, "additionalProperties": False}),
                                  lambda: run_checks(workspace))
    raw = env.create_model_client(stream=False) if env else ScriptedClient(case)
    if env:
        raw.request_timeout = 40
        if env.model_protocol == "anthropic":
            raw._client = raw._client.with_options(max_retries=0, timeout=40)
    client = BoundedClient(raw, trial)
    agent = Agent(client=client, tools=registry, allow_subagents=False,
                  prompt_runtime=PromptRuntime(workspace=workspace),
                  config=AgentConfig(model=env.model_id if env else "scripted", max_tokens=2048,
                                     loop_guard=LoopGuardConfig(max_total_tokens=60000, max_active_seconds=100)),
                  context=ContextManager(config=ContextConfig(mode="off", context_window_tokens=131072,
                      transcript_dir=trial / "transcripts", tool_output_dir=trial / "tool-results")),
                  recovery_runtime=RecoveryRuntime(RecoveryConfig(max_retries=0, max_continuations=0)),
                  skill_catalog=loader.catalog_prompt())
    started = time.monotonic()
    try:
        result = agent.run(case.prompt)
        answer, stop = result.final_text, result.stop_reason
        score = grade(case, trace, answer, workspace, stop)
        (trial / "answer.txt").write_text(answer, encoding="utf-8")
        score.update(stop_reason=stop, usage=client.usage_tracker.snapshot().to_dict())
    finally:
        if env and getattr(raw, "_client", None) is not None:
            raw._client.close()
    score.update(id=case.id, kind=case.kind, expected_skill=case.skill, model_calls=client.calls,
                 seconds=round(time.monotonic() - started, 2), model=env.model_id if env else "scripted",
                 protocol=env.model_protocol if env else "scripted",
                 reasoning_effort=env.reasoning_effort if env else None)
    return score


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("offline", "live"), default="offline")
    parser.add_argument("--case", action="append", dest="selected")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--skills-root", type=Path, default=bundled_root())
    parser.add_argument("--output", type=Path, default=Path("eval-results"))
    parser.add_argument("--regrade", type=Path, help="Re-score a saved run without calling a model")
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.regrade:
        root = regrade(args.regrade, args.output)
        report = json.loads((root / "report.json").read_text(encoding="utf-8"))
        print(f"Regraded: {root} ({report['passed']}/{report['total']})")
        return 0 if report["passed"] == report["total"] else 1
    all_cases = cases()
    unknown = set(args.selected or []) - {case.id for case in all_cases}
    if unknown or not 1 <= args.repeats <= 5:
        parser.error("Unknown case or repeats outside 1..5")
    selected = [case for case in all_cases if not args.selected or case.id in args.selected]
    if args.worker:
        score = worker(selected[0], args.mode, args.worker.resolve(), args.skills_root.resolve())
        write_json(args.worker / "result.json", score)
        return 0 if score["passed"] else 1
    loader = SkillLoader([args.skills_root])
    missing = {case.skill for case in selected if case.skill} - {skill.name for skill in loader.list_skills()}
    if missing:
        parser.error(f"Missing skills in catalog: {sorted(missing)}")
    root = args.output.resolve() / ("skills-" + args.mode + "-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8])
    root.mkdir(parents=True)
    snapshot = root / "skills"
    fingerprints = {}
    for skill in loader.list_skills():
        # Snapshot only manifests; references are not needed by this self-contained suite.
        dest = snapshot / skill.name / "SKILL.md"
        dest.parent.mkdir(parents=True)
        shutil.copyfile(skill.path, dest)
        fingerprints[skill.name] = hashlib.sha256(dest.read_bytes()).hexdigest()
    report = {"mode": args.mode, "profile": "guarded-file-skills-smoke-v1", "repeats": args.repeats,
              "skill_sha256": fingerprints, "source_root": str(args.skills_root.resolve()),
              "limits": {"calls_per_case": 8, "seconds_per_case": 120, "max_output_tokens_per_call": 2048},
              "limitations": "Offline checks wiring only. Analysis answers have factual smoke checks, not semantic grading. Repair cases have independent behavior checks. No guarantee of full workflow adherence.",
              "cases": []}
    # Preserve the evaluated harness and prompt version alongside the skill snapshot.
    sources = root / "source"
    for source in (Path(__file__), Path(__file__).with_name("skills_cases.py"),
                   Path(__file__).resolve().parents[1] / "codeagent/prompts/templates/skill.md"):
        sources.mkdir(exist_ok=True)
        shutil.copyfile(source, sources / source.name)
    print(f"Results: {root}", flush=True)
    for repeat in range(1, args.repeats + 1):
        for case in selected:
            trial = root / f"{case.id}-r{repeat}"
            trial.mkdir()
            command = [sys.executable, "-m", "evals.skills", "--mode", args.mode, "--case", case.id,
                       "--worker", str(trial), "--skills-root", str(snapshot)]
            try:
                process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=120,
                                         env={**os.environ, "PYTHONIOENCODING": "utf-8"})
                (trial / "stdout.txt").write_text(process.stdout, encoding="utf-8")
                (trial / "stderr.txt").write_text(process.stderr, encoding="utf-8")
                score = json.loads((trial / "result.json").read_text(encoding="utf-8")) if (trial / "result.json").exists() else {
                    "id": case.id, "kind": case.kind, "passed": False, "error": f"worker exited {process.returncode}; see stderr.txt"}
            except subprocess.TimeoutExpired:
                score = {"id": case.id, "kind": case.kind, "passed": False, "error": "worker timeout (120s)"}
            score["repeat"] = repeat
            report["cases"].append(score)
            report["passed"] = sum(row["passed"] for row in report["cases"])
            report["total"] = len(report["cases"])
            write_json(root / "report.json", report)
            print(f"{case.id} r{repeat}: {'PASS' if score['passed'] else 'FAIL'} {score.get('checks', score.get('error'))}", flush=True)
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
