"""Narrow fixture test tool and mutation/exposure evidence, not an Agent loop."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from codeagent.tools.base import ToolDefinition, ToolOutput
from evals.agent_adapter import append_record
from evals.evidence import hashes


def run_checks(workspace, suite):
    if suite not in {"parser", "exporter", "totals"}:
        return {"suite": suite, "passed": False, "error_type": "unknown_suite"}
    # No model credentials, repository import path, or general command arguments.
    env = {k: v for k, v in os.environ.items() if k.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
    try:
        process = subprocess.run([sys.executable, "-I", "-B", str(Path(__file__).with_name("checker.py")),
                                  str(workspace), suite], capture_output=True, text=True, encoding="utf-8",
                                 timeout=6, cwd=workspace, env=env)
        if process.returncode or len(process.stdout) > 10000:
            return {"suite": suite, "passed": False, "error_type": "checker_process_error"}
        return json.loads(process.stdout)
    except subprocess.TimeoutExpired:
        return {"suite": suite, "passed": False, "error_type": "checker_timeout"}


class JourneyObserver:
    def __init__(self, trial, workspace, seed):
        self.trial, self.workspace, self.seed = trial, workspace, seed
        self.phase = "setup"
        self.previous = hashes(workspace)
        self.exposed = {}

    def relative(self, value):
        path = Path(value)
        if not path.is_absolute():
            path = self.workspace / path
        try:
            return path.resolve().relative_to(self.workspace.resolve()).as_posix()
        except (ValueError, OSError):
            return str(value)

    def before(self, tool):
        append_record(self.trial / "tool-intents.jsonl", {"phase": self.phase, "id": tool.id, "name": tool.name,
                      "input": tool.input, "path": self.relative(tool.input.get("file_path", ""))})

    def scan(self, source):
        current = hashes(self.workspace)
        for path in sorted(current.keys() | self.previous.keys()):
            if current.get(path) != self.previous.get(path):
                append_record(self.trial / "mutations.jsonl", {"phase": self.phase, "source": source, "path": path,
                              "before": self.previous.get(path), "after": current.get(path)})
        self.previous = current

    def after(self, tool, output):
        self.scan(tool.id)
        if tool.name == "read_file" and getattr(output, "status", "success") == "success":
            path = self.relative(tool.input.get("file_path", ""))
            if path.startswith("evidence/") and path in self.seed["workspace_files"]:
                # Accumulate individually observed lines so paged reads count too.
                expected = self.seed["workspace_files"][path].splitlines()
                seen = self.exposed.setdefault(path, set())
                seen.update(i for i, line in enumerate(expected) if line in str(output))
                append_record(self.trial / "exposure.jsonl", {"phase": self.phase, "path": path,
                              "observed_lines": len(seen), "total_lines": len(expected), "complete": len(seen) == len(expected)})

    def tests(self, suite):
        result = run_checks(self.workspace, suite)
        append_record(self.trial / "agent-tests.jsonl", {"phase": self.phase, **result,
                      "source_hashes": {k: v for k, v in hashes(self.workspace).items() if k.startswith("src/")}})
        return ToolOutput(json.dumps(result, ensure_ascii=False), status="success" if result["passed"] else "error",
                          exit_code=0 if result["passed"] else 1)


def register_tools(agent, observer):
    agent.tools.register_handler(ToolDefinition("run_project_tests", "运行本练习的固定纯函数测试组，返回实际通过情况；不执行任意命令。",
        {"type": "object", "properties": {"suite": {"type": "string", "enum": ["parser", "exporter", "totals"]}},
         "required": ["suite"]}), observer.tests)
    if observer.seed["case_id"] == "T06":
        called = False

        def incident():
            nonlocal called
            if called:
                return "日志已接收。请使用此前工具输出中提供的真实归档位置，按缺口范围读取。"
            called = True
            return observer.seed["incident_record"]

        agent.tools.register_handler(ToolDefinition("read_incident_log", "接收一次历史事件日志。大结果由现有运行时归档；后续按真实路径回读。",
            {"type": "object", "properties": {}}), incident)
    agent.hooks.register("PreToolUse", observer.before, first=True)
    agent.hooks.register("PostToolUse", observer.after, first=True)
