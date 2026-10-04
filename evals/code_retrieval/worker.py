"""One fresh project Agent per question. The worker never opens the gold file."""
from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

from codeagent.agent import Agent, AgentConfig
from codeagent.anthropic_client import AnthropicModelClient
from codeagent.context import ContextConfig, ContextManager
from codeagent.events import EventEmitter, UsageTracker
from codeagent.events.sink import CallbackEventSink
from codeagent.hooks.loop_guard import LoopGuardConfig
from evals.execution_budget import EvaluationBudget
from codeagent.prompts import PromptMode, PromptRuntime
from codeagent.recovery import RecoveryConfig, RecoveryRuntime
from codeagent.tools import ToolRegistry, tool_schema_hash
from codeagent.tools.read import ReadFileTool
from codeagent.tools.grep import GrepTool
from codeagent.tools.glob_tool import GlobTool
from codeagent.tools.workspace import WorkspaceGuard
from evals.agent_adapter import EvidenceSDK, append_record
from evals.evidence import read_json, write_json

ANSWER_INSTRUCTION = '''在当前工作区定位代码，不修改文件。允许使用所有已提供的检索工具。
只查 codeagent/ 与 tests/ 的 Python 源码。最终只返回一个 JSON 对象，不加 Markdown。
有答案：{"status":"found","results":[{"path":"相对路径，使用 /","symbol":"精确函数名或类名.方法名","line":原文片段首行的整数行号,"quote":"连续原文，不包含行号前缀"}]}
候选按相关性从高到低排列，最多 5 个。每个 quote 最多 20 行，要包含说明题目所问行为的代码，不能只引用函数名。
跨文件问题需要给出每个环节，不要重复列同一个函数。找不到内置实现且已检查相关范围：{"status":"not_found","results":[]}。
未能充分检查：{"status":"insufficient_evidence","results":[]}。不要把测试代码或仅有的调用位置当成实际实现。
问题：
'''


class ScriptedSDK:
    """Exercise a real read tool, then abstain. No answer labels, no API calls."""
    def __init__(self):
        self.messages, self.index = self, 0

    def create(self, **kwargs):
        self.index += 1
        if self.index == 1:
            content = [{"type": "tool_use", "id": "smoke-read", "name": "read_file",
                        "input": {"file_path": "codeagent/__init__.py", "limit": 8}}]
        else:
            content = [{"type": "text", "text": json.dumps({"status": "insufficient_evidence", "results": []})}]
        return SimpleNamespace(content=content, stop_reason="tool_use" if self.index == 1 else "end_turn", usage=None)

    def close(self):
        pass

    def with_options(self, **kwargs):
        return self


class EvaluationClient(AnthropicModelClient):
    def get_model_window(self, model):
        # Fixed profile, no metadata network query or variable context discovery.
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trial", type=Path, required=True)
    trial = parser.parse_args().trial.resolve()
    spec = read_json(trial / "trial.json")
    p, workspace = spec["profile"], Path(spec["workspace"])
    setup_start, started, recorder = time.monotonic(), None, None
    result = {"outcome": "setup_error", "answer": None, "duration_ms": None, "setup_ms": None}
    try:
        emitter = EventEmitter(CallbackEventSink(lambda event: append_record(trial / "events.jsonl", event.to_dict())))
        tracker = UsageTracker()
        if spec["mode"] == "offline":
            sdk = ScriptedSDK()
        else:
            from anthropic import Anthropic
            sdk = Anthropic(api_key=os.environ["API_KEY"], base_url=os.getenv("BASE_URL") or None,
                            max_retries=0, timeout=60)
        recorder = EvidenceSDK(sdk, trial, max_calls=p["max_api_calls"])
        client = EvaluationClient(sdk_client=recorder, base_url=os.getenv("BASE_URL") or None,
                                  event_emitter=emitter, usage_tracker=tracker, stream=False)
        guard, registry = WorkspaceGuard(workspace), ToolRegistry()
        for tool in (ReadFileTool(workspace_guard=guard), GlobTool(workspace_guard=guard),
                     GrepTool(workspace_guard=guard)):
            registry.register(tool)
        if spec.get("tool_factory"):
            module, name = spec["tool_factory"].split(":", 1)
            factory = getattr(importlib.import_module(module), name)
            index_dir = trial / "index"
            index_dir.mkdir()
            tool = factory(workspace=workspace, index_dir=index_dir, client=client, model=p["model"], event_emitter=emitter)
            if tool.definition.name != "search_code":
                raise ValueError("Candidate factory must return a tool named search_code")
            registry.register(tool)
        guard_config = LoopGuardConfig(max_total_tokens=p["max_total_tokens"], max_active_seconds=p["timeout_seconds"])
        trial_budget = EvaluationBudget(guard_config, model_calls=p["max_api_calls"], tool_calls=p["max_tool_calls"])
        agent = Agent(execution_budget=trial_budget, client=client, tools=registry, config=AgentConfig(
            model=p["model"], max_tokens=p["max_tokens"], max_iterations=p["max_api_calls"],
            loop_guard=guard_config),
            context=ContextManager(config=ContextConfig(mode="off", context_window_tokens=p["context_window_tokens"],
                                                  transcript_dir=trial / "runtime/transcripts",
                                                  tool_output_dir=trial / "runtime/tool-results")),
            prompt_runtime=PromptRuntime(workspace=workspace), prompt_mode=PromptMode.NORMAL,
            allow_subagents=False, event_emitter=emitter, usage_tracker=tracker,
            recovery_runtime=RecoveryRuntime(RecoveryConfig(max_retries=1, side_query_max_retries=0,
                                                           max_continuations=0, escalated_max_tokens=p["max_tokens"])))
        # Keep archive reading, but disallow manual compaction in this fixed profile.
        agent.tools = agent.tools.copy_without({"compact"})
        agent.context.state.tool_schema_hash = tool_schema_hash(agent.tools.schemas())
        write_json(trial / "tool-schemas.json", agent.tools.schemas())
        started = time.monotonic()
        result["setup_ms"] = round((started - setup_start) * 1000, 3)
        write_json(trial / "started.json", {"setup_ms": result["setup_ms"]})
        instruction = ANSWER_INSTRUCTION
        if spec.get('corpus_scope', '').startswith('explicit language'):
            instruction = instruction.replace('只查 codeagent/ 与 tests/ 的 Python 源码。',
                '检查当前工作区的多语言源码与相关配置。重载方法需要保留完整签名以区分实体。')
        outcome = agent.run(instruction + spec["query"])
        result.update(outcome="completed" if outcome.stop_reason == "end_turn" else "agent_failed",
                      answer=outcome.final_text, stop_reason=outcome.stop_reason)
    except Exception as exc:
        result.update(outcome="error" if started else "setup_error", error_type=type(exc).__name__, error=str(exc))
    finally:
        if started:
            result["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
        else:
            result["setup_ms"] = round((time.monotonic() - setup_start) * 1000, 3)
        if recorder:
            recorder.close()
        append_record(trial / "worker-result.jsonl", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
