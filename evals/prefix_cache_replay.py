"""Deterministic offline replay of the two former prefix-breaking paths.

This is a path-level control, NOT a simulation of provider caching or model
quality. No SDK credentials, network calls, or synthetic billing are used.
"""
from __future__ import annotations

import json
from copy import deepcopy
from time import perf_counter

from codeagent import Agent, AgentConfig, ModelResponse, ToolRegistry
from codeagent.context import ContextConfig, ContextManager
from codeagent.context.budget import inspect_request
from codeagent.context.observation import observe_request
from codeagent.context.projection import build_tool_projection
from codeagent.events import CallbackEventSink, EventEmitter
from codeagent.messages import validate_tool_history


class CaptureClient:
    def __init__(self):
        self.calls = []

    def create_message(self, **params):
        self.calls.append(deepcopy(params))
        return ModelResponse("end_turn", [{"type": "text", "text": "scripted response"}])


def tool_round(index):
    return [
        {"role": "assistant", "content": [{"type": "tool_use", "id": str(index),
            "name": "grep", "input": {"pattern": "fixed evidence"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": str(index),
            "content": f"fixed result {index}: " + "evidence " * 1000}]},
    ]


def metrics(calls):
    baseline = {}
    observations = [observe_request(call, baseline, call_kind="main") for call in calls]
    rewrites = [o for o in observations if o["history_rewritten"]]
    return {
        "requests": len(calls),
        "history_rewrites": len(rewrites),
        "earliest_changed_message": min((o["first_changed_message"] for o in rewrites), default=None),
        "system_changes": sum("system" in o["changed_components"] for o in observations),
        "append_only_transitions": sum(o["append_only"] for o in observations[1:]),
        "total_serialized_request_chars": sum(inspect_request(**call).request_chars for call in calls),
        "provider_cache_hit_input": None, "provider_cache_miss_input": None,
        "provider_output_tokens": None, "main_cost": None, "auxiliary_cost": None,
        "total_cost": None, "quality_measured": False, "compactions": 0,
    }


def replay():
    config = ContextConfig(mode="off", cache_policy="cache_friendly", context_window_tokens=83_000,
                           max_request_chars=150_000, compact_threshold_chars=75_000,
                           investigation_keep_rounds=2, tool_clear_min_chars=8000)
    events, canonical, before = [], [{"role": "user", "content": "Inspect the fixed evidence."}], []
    client = CaptureClient()
    agent = Agent(client=client, tools=ToolRegistry(), config=AgentConfig(model="offline-script"),
                  context=ContextManager(config=config), allow_subagents=False,
                  event_emitter=EventEmitter(CallbackEventSink(events.append)))
    # Fixed system avoids comparing unrelated prompt template changes.
    # Date is controlled as a runtime fact, and never drives this replay.
    agent.prompt_runtime = None
    started = perf_counter()
    for index in range(24):
        addition = tool_round(index)
        canonical.extend(deepcopy(addition))
        agent.messages = deepcopy(canonical) if index == 0 else agent.messages + deepcopy(addition)
        feedback = "Change the search strategy." if 5 <= index < 10 else (
            "Inspect the error before retrying." if 10 <= index < 16 else "")
        # Prior path: feedback changed system; every pressure check re-cleaned
        # canonical history using a sliding window without saved request edits.
        old = dict(model="offline-script", system="Stable rules." + (
                       "\n\n[本次执行的运行时纠正；不授予新权限]\n" + feedback if feedback else ""),
                   messages=deepcopy(canonical), tools=[], max_tokens=8000)
        if inspect_request(**old).request_chars > config.compact_threshold_chars:
            old["messages"] = build_tool_projection(old["messages"], investigation_keep=2, min_chars=8000)
        validate_tool_history(old["messages"])
        before.append(old)
        agent._loop_guard.feedback = lambda feedback=feedback: feedback
        agent._create_message(model="offline-script", system="Stable rules.",
                              messages=agent.messages, tools=[], max_tokens=8000)
        validate_tool_history(client.calls[-1]["messages"])
    return {
        "mode": "offline_path_replay", "paid_model_calls": 0,
        "controlled": ["model", "tool outputs", "feedback timeline", "max_tokens", "system rules"],
        "not_measured": ["task completion quality", "provider caching", "API latency", "actual cost"],
        "elapsed_local_seconds": round(perf_counter() - started, 4),
        "before": metrics(before), "after": metrics(client.calls),
        "cleanup_boundaries_after": sum(e.type == "context.request_projected" and e.payload["cleanup_boundary"] for e in events),
        "canonical_tool_results_preserved": all(
            any(message == expected for message in agent.messages)
            for expected in canonical if message_is_tool_result(expected)),
    }


def message_is_tool_result(message):
    return message["role"] == "user" and isinstance(message["content"], list)


if __name__ == "__main__":
    print(json.dumps(replay(), ensure_ascii=False, indent=2))
