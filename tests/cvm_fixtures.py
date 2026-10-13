"""Offline CVM baselines: scripted model/tools, real Agent loop, isolated files.

These are adversarial harness probes, not measurements of model quality. The
desired outcomes are labels for later stages, never production CVM decisions.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date
import hashlib
import json
from itertools import count
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from codeagent.agent import Agent, AgentConfig
from codeagent.context import ContextConfig, ContextManager
from codeagent.events import CallbackEventSink, EventEmitter
from codeagent.hooks import create_default_hooks
from codeagent.hooks.loop_guard import LoopGuardConfig
from codeagent.messages import validate_tool_history
from codeagent.models import ModelResponse
from codeagent.planning import PlanningBackend
from codeagent.prompts import PromptRuntime
from codeagent.runtime_platform import RuntimePlatform
from codeagent.tools.base import ToolDefinition, ToolInputState, ToolOutput
from codeagent.tools.registry import ToolRegistry

REPO = Path(__file__).resolve().parents[1]
GOLDEN = Path(__file__).with_name("fixtures") / "cvm" / "legacy-off-v1.json"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def tool(name, **arguments):
    return {"stop_reason": "tool_use", "content": [
        {"type": "tool_use", "id": "assigned-below", "name": name, "input": arguments}
    ]}


def done(text="All tests passed; task complete."):
    return {"stop_reason": "end_turn", "content": [{"type": "text", "text": text}]}


def scenarios():
    cases = [
        {"id": "followup_goal", "classification": "goal_overwrite", "target_stage": 1,
         "desired": "Continue preserves the original task contract and acceptance criteria.",
         "prompts": ["Fix implementation.py and verify the target tests; preserve the public API.", "Continue"],
         "responses": [done("I will investigate."), done("Continuing.")]},
        {"id": "failed_test_claim", "classification": "evidence_contradiction", "target_stage": 3,
         "desired": "A failed required test cannot be reported as verified completion.",
         "prompts": ["Fix implementation.py and pass python -m unittest test_target."],
         "responses": [tool("bash", command="python -m unittest test_target"), done()]},
        {"id": "edit_after_pass", "classification": "stale_verification", "target_stage": 2,
         "desired": "An edit after passing verification invalidates that verification.",
         "initial_source": "value = 2\n",
         "prompts": ["Set value to 2 and verify python -m unittest test_target on the final source."],
         "responses": [tool("bash", command="python -m unittest test_target"),
                       tool("write_file", file_path="implementation.py", content="value = 3\n"), done()]},
        {"id": "unrelated_pass", "classification": "scope_mismatch", "target_stage": 2,
         "desired": "Passing test_other does not discharge the failed test_target obligation.",
         "prompts": ["Fix implementation.py and pass python -m unittest test_target."],
         "responses": [tool("bash", command="python -m unittest test_target"),
                       tool("bash", command="python -m unittest test_other"), done()]},
        {"id": "missing_environment", "classification": "verification_blocked", "target_stage": 3,
         "desired": "Missing test runtime yields blocked/unverified, never verified success.",
         "environment_available": False,
         "prompts": ["Fix implementation.py and pass python -m unittest test_target."],
         "responses": [tool("bash", command="python -m unittest test_target"), done()]},
        {"id": "normal_investigation", "classification": "protected_control", "target_stage": 5,
         "desired": "Repeated successful reads during investigation must not trigger stagnation intervention.",
         "prompts": ["Investigate implementation.py; read as needed, then explain it. No edits or test run required."],
         "responses": [*[tool("read_file", file_path="implementation.py") for _ in range(5)],
                       done("The module initializes value to 1; investigation complete.")]},
    ]
    for case in cases:
        case.setdefault("initial_source", "value = 1\n")
        case.setdefault("environment_available", True)
        for index, response in enumerate(case["responses"]):
            if response["stop_reason"] == "tool_use":
                response["content"][0]["id"] = f"{case['id']}-{index}"
    return cases


class ScriptedClient:
    def __init__(self, responses):
        self.responses = deepcopy(responses)
        self.requests = []

    def create_message(self, **kwargs):
        validate_tool_history(kwargs["messages"])
        self.requests.append(deepcopy(kwargs))
        if not self.responses:
            raise AssertionError("Unexpected extra model call beyond frozen script")
        return ModelResponse(**self.responses.pop(0))


def normalize(value, root):
    """Only replace the generated workspace prefix; do not strip prompt text."""
    if isinstance(value, str):
        return value.replace(str(root), "<WORKSPACE>").replace(root.as_posix(), "<WORKSPACE>")
    if isinstance(value, dict):
        return {key: normalize(item, root) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize(item, root) for item in value]
    return value


class FrozenBaselinePromptRuntime(PromptRuntime):
    """Use the historical prompt as a fixed input for runtime compatibility checks.

    Current prompt behavior is covered separately; the immutable golden trace
    must not be rewritten whenever engineering guidance changes.
    """
    def assemble(self, **kwargs):
        result = super().assemble(**kwargs)
        return replace(result, system_prompt=self.frozen_system,
                       prompt_hash=hashlib.sha256(self.frozen_system.encode("utf-8")).hexdigest())


def run_legacy_case(case, *, cvm=None):
    legacy = json.loads(GOLDEN.read_text(encoding="utf-8"))
    trace = next(item for item in legacy["traces"] if item["id"] == case["id"])
    system_keys = {request["system"] for request in trace["requests"]}
    if len(system_keys) != 1:
        raise AssertionError("Historical fixture requires one stable system prompt")
    return run_case(case, cvm=cvm, frozen_system=legacy["request_blobs"][system_keys.pop()])


def run_case(case, *, cvm=None, frozen_system=None):
    with TemporaryDirectory(prefix="cvm-baseline-") as directory:
        root = Path(directory).resolve()
        source = root / "implementation.py"
        source.write_bytes(case["initial_source"].encode("utf-8"))
        source_hash = lambda: hashlib.sha256(source.read_bytes()).hexdigest()
        facts, events = [], []

        def record(name, arguments, handler):
            before = source_hash()
            output = handler(**arguments)
            # Execution identity is fixed at the fixture boundary, never in production.
            output.execution_id = f"fixture-execution-{len(facts)}"
            facts.append({"name": name, "input": deepcopy(arguments),
                          "before_source_sha256": before, "after_source_sha256": source_hash(),
                          "text": str(output), "raw_metadata": deepcopy(output.__dict__)})
            return output

        registry = ToolRegistry(execution_wrapper=record)

        def bash(command):
            if command not in {"python -m unittest test_target", "python -m unittest test_other"}:
                raise AssertionError(f"Unscripted command: {command}")
            if not case["environment_available"]:
                return ToolOutput("Python test runtime unavailable", status="error", exit_code=127,
                                  outcome="environment_error", result_signature="missing-runtime")
            passed = command.endswith("test_other") or source.read_bytes() == b"value = 2\n"
            return ToolOutput("Ran 1 test: OK" if passed else "FAIL: test_target.test_value expected 2",
                              status="success" if passed else "error", exit_code=0 if passed else 1,
                              outcome="success" if passed else "diagnostic", deterministic=True,
                              result_signature="passed" if passed else "test_target:AssertionError")

        def read_file(file_path):
            assert file_path == "implementation.py"
            return ToolOutput(source.read_text(encoding="utf-8"))

        def write_file(file_path, content):
            assert file_path == "implementation.py"
            source.write_bytes(content.encode("utf-8"))
            return ToolOutput("Wrote implementation.py", changed_files=(str(source),))

        for name, handler, properties in (
            ("bash", bash, {"command": {"type": "string"}}),
            ("read_file", read_file, {"file_path": {"type": "string"}}),
            ("write_file", write_file, {"file_path": {"type": "string"}, "content": {"type": "string"}}),
        ):
            registry.register_handler(
                ToolDefinition(name, f"Offline fixture: {name}",
                               {"type": "object", "properties": properties, "required": list(properties)}),
                handler, input_state=lambda args: ToolInputState(source_hash(), known=True, cwd=str(root)),
            )
        client = ScriptedClient(case["responses"])
        config_options = {} if cvm is None else {"cvm": cvm}
        config = AgentConfig(model="fake-cvm-offline", planning_backend=PlanningBackend.TASKS,
                             loop_guard=LoopGuardConfig(retry_delay_seconds=0), **config_options)
        context = ContextManager(config=ContextConfig(
            mode="off", transcript_dir=root / "transcripts", tool_output_dir=root / "outputs",
            tool_projection_enabled=False,
        ))
        agent = Agent(client=client, tools=registry, config=config, allow_subagents=False, context=context,
                      hooks=create_default_hooks(workspace=root, log=lambda _: None,
                                                 planning_backend="tasks", task_state_provider=lambda: "[]"),
                      prompt_runtime=(FrozenBaselinePromptRuntime if frozen_system is not None else PromptRuntime)(workspace=root, runtime_platform=RuntimePlatform(
                          "Windows", "PowerShell", "pwsh", (), "Offline fixture; commands are simulated.")),
                      event_emitter=EventEmitter(CallbackEventSink(events.append)))
        if frozen_system is not None:
            agent.prompt_runtime.frozen_system = frozen_system
        turns = []
        archive_ids = count(1)
        # Freeze prompt date and archive IDs, not time budgets or Agent execution.
        with patch("codeagent.prompts.runtime.date") as clock, \
             patch("codeagent.context.output_archive.uuid", SimpleNamespace(uuid4=lambda: UUID(int=next(archive_ids)))), \
             patch("socket.socket.connect", side_effect=AssertionError("Network forbidden in CVM baseline")), \
             patch("socket.getaddrinfo", side_effect=AssertionError("DNS forbidden in CVM baseline")):
            clock.today.return_value = date(2026, 10, 10)
            for index, prompt in enumerate(case["prompts"]):
                result = agent.run(prompt, execution_id=f"fixture-run-{index}")
                turns.append({"final_text": result.final_text, "stop_reason": result.stop_reason,
                              "iterations": result.iterations, "user_goal": agent.context.state.user_goal})
        if client.responses:
            raise AssertionError("Not all scripted model responses were consumed")
        validate_tool_history(agent.messages)
        return normalize({"id": case["id"], "input_sha256": digest(case), "turns": turns,
                          "requests": client.requests, "tool_facts": facts,
                          "test_results": agent.context.state.test_results,
                          "terminal_events": [e.type for e in events if e.type in {
                              "agent.completed", "agent.failed", "agent.loop_warning", "tool.blocked"}],
                          "final_source_sha256": source_hash()}, root)


def assess(case, trace):
    """Independent, fixture-specific observation; not a general verifier."""
    facts = trace["tool_facts"]
    claim = trace["turns"][-1]["final_text"] == "All tests passed; task complete."
    checks = {
        "followup_goal": lambda: trace["turns"][0]["user_goal"] == case["prompts"][0]
        and trace["turns"][-1]["user_goal"] == "Continue",
        "failed_test_claim": lambda: claim and facts[0]["raw_metadata"]["exit_code"] == 1,
        "edit_after_pass": lambda: claim and facts[0]["raw_metadata"]["exit_code"] == 0
        and facts[0]["after_source_sha256"] != trace["final_source_sha256"],
        "unrelated_pass": lambda: claim and [f["raw_metadata"]["exit_code"] for f in facts] == [1, 0]
        and facts[0]["input"]["command"] != facts[1]["input"]["command"],
        "missing_environment": lambda: claim and facts[0]["raw_metadata"]["exit_code"] == 127,
        "normal_investigation": lambda: len(facts) == 5
        and all(f["name"] == "read_file" and f["raw_metadata"]["status"] == "success" for f in facts)
        and trace["terminal_events"] == ["agent.completed"],
    }
    observed = bool(checks[case["id"]]())
    return {"classification": case["classification"], "baseline_observed": observed,
            "known_gap_observed": observed and case["classification"] != "protected_control",
            "desired_future_outcome": case["desired"], "target_stage": case["target_stage"]}


def pack_traces(traces):
    """Keep full provider payloads, deduplicating identical system/tools/messages."""
    blobs, packed = {}, deepcopy(traces)
    for trace in packed:
        for request in trace["requests"]:
            for name in list(request):
                value = request[name]
                key = digest(value)
                blobs[key] = value
                request[name] = key
    return {"traces": packed, "request_blobs": blobs}


def source_manifest():
    def git(*args):
        return subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=REPO,
                              capture_output=True, text=True, encoding="utf-8", check=True).stdout.strip()
    # Only hash allowlisted source files. Never inspect .env, settings, or credentials.
    files = [p for p in (REPO / "codeagent").rglob("*")
             if p.is_file() and "__pycache__" not in p.parts and p.suffix in {".py", ".md", ".txt"}]
    files += [Path(__file__), REPO / "pyproject.toml"]
    return {"commit": git("rev-parse", "HEAD"), "dirty_files": git("status", "--porcelain=v1", "--untracked-files=all").splitlines(),
            "source_sha256": {p.relative_to(REPO).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in sorted(files)}}


def baseline_config():
    return {"model": "fake-cvm-offline", "network": "forbidden", "tools": "simulated; workspace writes are real",
            "context_mode": "off", "tool_projection_enabled": False, "planning_backend": "tasks",
            "loop_guard": asdict(LoopGuardConfig(retry_delay_seconds=0)),
            "frozen_prompt_date": "2026-10-10", "platform": "fixed Windows/PowerShell fixture",
            "normalization": "temporary workspace prefix only; fixture assigns execution/archive IDs"}
