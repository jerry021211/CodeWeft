"""Offline behavioral checks at the final SDK boundary, with no paid API calls."""
from copy import deepcopy
from dataclasses import asdict
from datetime import date
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from codeagent import Agent, AgentConfig, AnthropicModelClient, ToolDefinition, ToolRegistry
from codeagent.context import ContextConfig, ContextManager, RuntimeState
from codeagent.context.budget import RequestBudgetError
from codeagent.context.observation import fingerprint, observe_request
from codeagent.context.projection import TOOL_VIEW_MARKER
from codeagent.events import CallbackEventSink, EventEmitter, TokenUsage, UsageTracker
from codeagent.hooks.defaults import create_default_hooks
from codeagent.messages import ToolUse, validate_tool_history
from codeagent.permissions import PermissionPolicy
from codeagent.prompts import PromptMode, PromptRuntime
from codeagent.tools.registry import tool_request_hash, tool_schema_hash
from codeagent.web.factory import _restore_runtime_state, serialize_runtime_state
from tests.test_context_simplification import SummaryClient, rounds


class SDK:
    def __init__(self, responses=()):
        self.messages = self
        self.calls = []
        self.responses = list(responses)

    def with_options(self, **kwargs):
        return self

    def post(self, path, *, body, cast_to):
        return self.create(**body)

    def create(self, **params):
        self.calls.append(deepcopy(params))
        return self.responses.pop(0) if self.responses else SimpleNamespace(
            stop_reason="end_turn", content=[{"type": "text", "text": "done"}],
            usage=SimpleNamespace(input_tokens=10, output_tokens=2,
                                  cache_read_input_tokens=90, cache_creation_input_tokens=0))


class LocalClient(AnthropicModelClient):
    def get_model_window(self, model):
        return {"context_window_tokens": 1_000_000, "context_window_source": "test"}


class PrefixCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.events = []

    def agent(self, *, state=None, messages=None, sdk=None, config=None, hooks=None):
        client = LocalClient(sdk_client=sdk or SDK(), base_url="https://api.deepseek.com/anthropic",
                             event_emitter=EventEmitter(CallbackEventSink(self.events.append)))
        registry = ToolRegistry()
        registry.register_handler(ToolDefinition("echo", "Echo", {"type": "object"}), lambda **_: "fixed output")
        return Agent(client=client, tools=registry, config=AgentConfig(model="deepseek-flash"),
                     allow_subagents=False, messages=messages or [],
                     context=ContextManager(config=config or ContextConfig(mode="off"), state=state),
                     prompt_runtime=PromptRuntime(workspace=self.root),
                     **({"hooks": hooks} if hooks else {}))

    def send(self, agent):
        assembly = agent._assemble_prompt(agent.tools.schemas())
        return agent._create_message(model=agent.config.model, system=assembly.system_prompt,
                                     messages=agent.messages, tools=agent.tools.schemas(),
                                     max_tokens=agent.config.max_tokens, prompt_assembly=assembly)

    def observations(self, kind="main"):
        return [event.payload for event in self.events
                if event.type == "request.observed" and event.payload["call_kind"] == kind]

    def test_actual_tool_loop_retains_every_sent_prefix(self):
        responses = [SimpleNamespace(stop_reason="tool_use", content=[{
            "type": "tool_use", "id": f"call-{i}", "name": "echo", "input": {},
        }], usage=None) for i in range(8)]
        sdk = SDK(responses)
        agent = self.agent(sdk=sdk)
        result = agent.run("inspect the fixed outputs")
        self.assertEqual(result.final_text, "done")
        self.assertEqual(len(sdk.calls), 9)
        for old, new in zip(sdk.calls, sdk.calls[1:]):
            self.assertEqual(old["system"], new["system"])
            self.assertEqual(old["tools"], new["tools"])
            self.assertEqual(old["messages"], new["messages"][:len(old["messages"])])
            validate_tool_history(new["messages"])
        self.assertTrue(all(item["append_only"] for item in self.observations()[1:]))

    def test_guard_new_update_clear_and_retry_only_append(self):
        agent = self.agent(messages=rounds(1, size=20))
        value = [""]
        with patch.object(agent._loop_guard, "feedback", side_effect=lambda: value[0]):
            for feedback in ("", "change approach", "change approach", "inspect error", "", ""):
                value[0] = feedback
                self.send(agent)
        calls = agent.client._client.calls
        self.assertEqual([len(call["messages"]) for call in calls], [3, 4, 4, 5, 6, 6])
        self.assertEqual(len({call["system"] for call in calls}), 1)
        for old, new in zip(calls, calls[1:]):
            self.assertEqual(old["messages"], new["messages"][:len(old["messages"])])
            validate_tool_history(new["messages"])
        self.assertIn("已解除", calls[-1]["messages"][-1]["content"])
        self.assertNotIn("change approach", calls[1]["system"])
        self.assertEqual(agent._loop_guard.state.rounds, 0)  # preflight didn't run hooks again

    def test_final_sdk_parameters_include_reasoning_and_no_bodies_in_events(self):
        agent = self.agent(messages=[{"role": "user", "content": "private message"}])
        agent.client.reasoning_effort = "high"
        with patch("codeagent.anthropic_client.reasoning_capabilities", return_value={}), patch(
                "codeagent.anthropic_client.reasoning_parameters", return_value={"thinking": {"type": "enabled"}}):
            self.send(agent)
        sent = agent.client._client.calls[-1]
        observed = self.observations()[-1]
        self.assertEqual(observed["request"], fingerprint(sent))
        self.assertEqual(observed["system"], fingerprint(sent["system"]))
        self.assertEqual(observed["messages"], [fingerprint(m) for m in sent["messages"]])
        self.assertIn("extra_body", sent)
        self.assertEqual(observed["observation_boundary"], "sdk_parameters")
        self.assertFalse(observed["provider_cache_key"])
        self.assertNotIn("private message", json.dumps(observed))
        self.assertEqual(observed["prompt_revision"], 1)

    def test_object_order_stable_and_tool_array_order_detected(self):
        self.assertEqual(fingerprint({"a": 1, "b": 2}), fingerprint({"b": 2, "a": 1}))
        tools = [{"name": "a"}, {"name": "b"}]
        params = dict(model="same", system="same", tools=tools, messages=[])
        baseline = {}
        observe_request(params, baseline, call_kind="main")
        changed = observe_request({**params, "tools": tools[::-1]}, baseline, call_kind="main")
        self.assertEqual(changed["first_difference"], "tools")
        self.assertFalse(changed["append_only"])
        self.assertNotEqual(tool_request_hash(tools), tool_request_hash(tools[::-1]))
        self.assertEqual(tool_schema_hash(tools), tool_schema_hash(tools[::-1]))

    def test_side_clients_and_children_do_not_pollute_main_or_reminders(self):
        agent = self.agent(messages=rounds(1, size=20))
        self.send(agent)
        before = deepcopy(agent.context.state.runtime_reminders)
        for kind in ("context_summary", "memory_selection", "memory_maintenance"):
            for _ in range(2):
                client = agent._side_query_client(kind)
                client.create_message(model="summary", system="auxiliary", messages=[{"role": "user", "content": kind}],
                                      tools=[], max_tokens=100)
            self.assertTrue(self.observations(kind)[-1]["unchanged"])
        child = agent.client.fork(call_kind="subagent")
        child.create_message(model="same", system="child", messages=[], tools=[], max_tokens=10)
        self.send(agent)
        self.assertTrue(self.observations()[-1]["unchanged"])
        self.assertEqual(before, agent.context.state.runtime_reminders)
        self.assertIsNot(child._request_baselines, agent.context.state.request_baselines)

    def test_checkpoint_roundtrip_preserves_prompt_guard_and_baseline(self):
        agent = self.agent(messages=rounds(1, size=20))
        with patch.object(agent._loop_guard, "feedback", return_value="correct this"):
            self.send(agent)
        payload = json.loads(json.dumps(serialize_runtime_state(agent.context.state)))
        restored = self.agent(state=_restore_runtime_state(payload), messages=deepcopy(agent.messages))
        with patch.object(restored._loop_guard, "feedback", return_value="correct this"):
            self.send(restored)
        self.assertEqual(agent.client._client.calls[-1], restored.client._client.calls[-1])
        self.assertTrue(self.observations()[-1]["unchanged"])
        self.assertIn("checkpoint_restored", self.observations()[-1]["rewrite_reasons"])
        self.assertEqual(restored.context.state.prompt_revision, 1)

    def test_legacy_and_incompatible_checkpoints_rebuild_explicitly(self):
        for snapshot in ({}, {"version": 999}):
            state = _restore_runtime_state({"user_goal": "old task", "prompt_snapshot": snapshot})
            agent = self.agent(state=state, messages=rounds(1, size=20))
            self.send(agent)
            self.assertEqual(agent.context.state.prompt_snapshot["version"], 2)
            self.assertTrue(self.observations()[-1]["rewrite_reasons"])
            self.assertFalse(self.observations()[-1]["baseline_available"])
        self.assertEqual(_restore_runtime_state({"removed_field": True}).summary_revision, 0)

    def test_configuration_permission_rules_and_refresh_rebuild_once(self):
        policy = PermissionPolicy(workspace=self.root)
        hooks = create_default_hooks(permission_policy=policy, log=lambda _: None)
        agent = self.agent(hooks=hooks, messages=rounds(1, size=20))
        agent.prompt_mode = PromptMode.NORMAL
        self.send(agent)
        first = deepcopy(agent.client._client.calls[-1])
        self.send(agent)
        self.assertEqual(agent.context.state.prompt_revision, 1)
        policy.hard_deny_patterns += ("new-danger",)
        self.send(agent)
        self.assertIn("permissions_changed", self.observations()[-1]["rewrite_reasons"])
        self.assertIn("Blocked", agent.hooks.trigger("PreToolUse", ToolUse("deny", "bash", {"command": "new-danger"})))
        before_mode_change = deepcopy(agent.client._client.calls[-1])
        revision = agent.context.state.prompt_revision
        agent.set_read_only(True)
        self.send(agent)
        self.assertNotIn("mode_changed", self.observations()[-1]["rewrite_reasons"])
        self.assertEqual(revision, agent.context.state.prompt_revision)
        self.assertEqual(before_mode_change["system"], agent.client._client.calls[-1]["system"])
        self.assertEqual(before_mode_change["tools"], agent.client._client.calls[-1]["tools"])
        self.assertTrue(self.observations()[-1]["append_only"])
        self.assertIsNotNone(agent.hooks.trigger("PreToolUse", ToolUse("deny2", "write_file", {"file_path": "a"})))
        (self.root / ".prompts").mkdir()
        (self.root / ".prompts/project.md").write_text("New safety rule.", encoding="utf-8")
        self.send(agent)
        self.assertIn("New safety rule.", agent.client._client.calls[-1]["system"])
        self.assertIn("rules_changed", self.observations()[-1]["rewrite_reasons"])
        agent.refresh_project_rules()
        self.send(agent)
        self.assertIn("project_rules_refresh", self.observations()[-1]["rewrite_reasons"])
        version = agent.context.state.prompt_revision
        self.send(agent)
        self.assertEqual(version, agent.context.state.prompt_revision)
        self.assertEqual(first["messages"], agent.client._client.calls[-1]["messages"][:len(first["messages"])])

    def test_date_update_appends_without_changing_frozen_system(self):
        agent = self.agent(messages=rounds(1, size=20))
        with patch("codeagent.prompts.runtime.date") as clock:
            clock.today.return_value = date(2026, 10, 1)
            self.send(agent)
            first = deepcopy(agent.client._client.calls[-1])
            clock.today.return_value = date(2026, 10, 2)
            self.send(agent)
            self.send(agent)
        last = agent.client._client.calls[-1]
        self.assertEqual(first["system"], last["system"])
        self.assertEqual(len(last["messages"]), len(first["messages"]) + 1)
        self.assertIn("2026-10-02", last["messages"][-1]["content"])

    def test_refresh_does_not_leave_an_older_date_reminder_as_latest_fact(self):
        agent = self.agent(messages=rounds(1, size=20))
        with patch("codeagent.prompts.runtime.date") as clock:
            for day in (1, 2, 3):
                clock.today.return_value = date(2026, 10, day)
                if day == 3:
                    agent.refresh_project_rules()
                self.send(agent)
        self.assertIn("2026-10-03", agent.client._client.calls[-1]["messages"][-1]["content"])

    def test_auto_compaction_reinjects_folded_guard_before_send(self):
        config = ContextConfig(summarization_model="summary", compact_threshold_chars=100_000,
                               transcript_dir=self.root / "transcripts", tool_output_dir=self.root / "outputs")
        agent = self.agent(config=config, messages=rounds(1, size=1000))
        agent.context.begin_turn(0)
        with patch.object(agent._loop_guard, "feedback", return_value="preserve corrective instruction"):
            self.send(agent)
            before = deepcopy(agent.messages)
            agent.messages += rounds(8, start=1, size=3000)[1:]
            config.near_context_ratio = 0.01
            config.cache_policy = "legacy"
            self.send(agent)
            self.assertGreater(agent.context.state.summary_revision, 0)
            self.assertEqual(agent.messages[:len(before)], before)
            sent = agent.client._client.calls[-1]
            self.assertIn("preserve corrective instruction", sent["messages"][-1]["content"])
            self.assertEqual(sent["system"], agent.client._client.calls[0]["system"])
            self.send(agent)
            self.assertEqual(len(agent.messages), len(before) + 16 + 1)

    def test_preflight_compaction_reinjects_mode_without_rebuilding_system(self):
        config = ContextConfig(summarization_model="summary", compact_threshold_chars=100_000,
                               transcript_dir=self.root / "transcripts", tool_output_dir=self.root / "outputs")
        agent = self.agent(config=config, messages=rounds(1, size=1000))
        agent.read_only = True
        agent.prompt_mode = PromptMode.NORMAL
        agent.context.begin_turn(0)
        self.send(agent)
        before = deepcopy(agent.messages)
        agent.messages += rounds(8, start=1, size=3000)[1:]
        config.near_context_ratio = 0.01
        config.cache_policy = "legacy"
        self.send(agent)
        self.assertGreater(agent.context.state.summary_revision, 0)
        self.assertEqual(agent.messages[:len(before)], before)
        sent = agent.client._client.calls[-1]
        self.assertIn("当前权限：只读", sent["messages"][-1]["content"])
        self.assertEqual(sent["system"], agent.client._client.calls[0]["system"])
        after_count = len(agent.messages)
        self.send(agent)
        self.assertEqual(len(agent.messages), after_count)
        self.assertEqual(agent.context.state.prompt_revision, 1)

    def test_incomplete_tool_pair_cannot_be_split_by_guard(self):
        agent = self.agent(messages=rounds(1, size=20))
        agent.messages.pop()
        original = deepcopy(agent.messages)
        with patch.object(agent._loop_guard, "feedback", return_value="warn"):
            with self.assertRaises(ValueError):
                self.send(agent)
        self.assertEqual(agent.messages, original)
        self.assertEqual(agent.client._client.calls, [])

    def test_long_history_emits_all_fingerprints_without_redaction_truncation(self):
        agent = self.agent(messages=rounds(120, size=5))
        self.send(agent)
        events = [e.payload for e in self.events if e.type.startswith("request.")]
        self.assertTrue(all(not e.get("truncated") for e in events))
        hashes = [item for e in events for item in e.get("messages", [])]
        self.assertEqual(hashes, [fingerprint(m) for m in agent.client._client.calls[-1]["messages"]])

    def test_usage_is_real_disjoint_and_weighted(self):
        tracker = UsageTracker()
        tracker.record(TokenUsage(input_tokens=10, cache_read_input_tokens=90, output_tokens=2))
        tracker.record(TokenUsage(input_tokens=800, cache_read_input_tokens=200, output_tokens=3,
                                  cache_creation_input_tokens=100, call_kind="context_summary"))
        totals = tracker.snapshot()
        self.assertEqual(totals.prompt_input_tokens, 1200)
        self.assertAlmostEqual(totals.cache_hit_ratio, 290 / 1200)
        agent = self.agent(messages=rounds(1, size=20))
        self.send(agent)
        self.assertEqual(agent.context.state.cache_hit_tokens, 90)
        self.assertEqual(agent.context.state.cache_miss_tokens, 10)
        self.assertEqual(agent.context.state.accumulated_input_tokens, 100)


class StableCleanupTests(unittest.TestCase):
    def manager(self, **options):
        return ContextManager(config=ContextConfig(**{
            "mode": "off", "cache_policy": "cache_friendly", "max_request_chars": 50_000,
            "context_window_tokens": 26_000, "compact_threshold_chars": 1000,
            "investigation_keep_rounds": 1, "tool_clear_min_chars": 2000, **options}))

    def test_below_soft_limit_retains_history_despite_legacy_character_trigger(self):
        manager = self.manager()
        history = rounds(4, name="grep", size=5000)
        self.assertEqual(manager.prepare_before_model_call(history), history)
        self.assertEqual(manager.state.request_view, {})

    def test_boundary_cleanup_stays_stable_after_append_and_checkpoint(self):
        manager = self.manager()
        history = rounds(8, name="grep", size=5500)
        original = deepcopy(history)
        sent = manager.prepare_before_model_call(history)
        self.assertIn(TOOL_VIEW_MARKER, str(sent))
        self.assertEqual(history, original)
        boundary = deepcopy(manager.state.request_view)
        history += rounds(1, start=8, name="grep", size=2100)[1:]
        next_sent = manager.prepare_before_model_call(history)
        self.assertEqual(next_sent[:len(sent)], sent)
        self.assertEqual(boundary, manager.state.request_view)
        restored = ContextManager(config=manager.config,
            state=_restore_runtime_state(json.loads(json.dumps(serialize_runtime_state(manager.state)))))
        self.assertEqual(restored.prepare_before_model_call(history), next_sent)
        validate_tool_history(next_sent)

    def test_hysteresis_does_not_slide_when_cleanup_cannot_reduce_pressure(self):
        manager = self.manager()
        events = []
        emitter = EventEmitter(CallbackEventSink(events.append))
        history = rounds(8, name="unknown_tool", size=5200)
        manager.prepare_before_model_call(history, event_emitter=emitter)
        history += rounds(1, start=8, name="grep", size=2100)[1:]
        manager.prepare_before_model_call(history, event_emitter=emitter)
        self.assertEqual([e.payload["cleanup_boundary"] for e in events if e.type == "context.request_projected"], [True, False])
        history += rounds(1, start=9, name="grep", size=6000)[1:]
        manager.prepare_before_model_call(history, event_emitter=emitter)
        self.assertTrue(events[-1].payload["cleanup_boundary"])
        self.assertIsNone(events[-1].payload["max_request_chars"])

    def test_unknown_window_and_large_window_never_fall_back_to_characters(self):
        for window, capabilities in ((0, {"cheap_prefix_reads": True}), (1_000_000, {})):
            manager = self.manager(cache_policy="auto", context_window_tokens=window)
            history = rounds(4, name="grep", size=5000)
            sent = manager.prepare_before_model_call(history, cache_capabilities=capabilities)
            self.assertEqual(sent, history)
            self.assertEqual(manager.state.request_view, {})

    def test_hard_output_reserve_and_token_window_cannot_be_bypassed(self):
        manager = self.manager(context_window_tokens=10_000)
        with self.assertRaises(RequestBudgetError):
            manager.prepare_before_model_call(rounds(1, size=100), max_tokens=10_000)
        with self.assertRaises(RequestBudgetError):
            self.manager(context_window_tokens=25_000).prepare_before_model_call([{"role": "user", "content": "x" * 51_000}])

    def test_projection_config_and_source_changes_invalidate_snapshot(self):
        manager = self.manager()
        history = rounds(8, name="grep", size=5500)
        manager.prepare_before_model_call(history)
        manager.config.tool_projection_enabled = False
        self.assertEqual(manager.project_messages(history), history)
        self.assertIn("configuration", manager.consume_generation_reason())
        manager.config.tool_projection_enabled = True
        manager.prepare_before_model_call(history)
        history[2]["content"][0]["content"] = "new evidence"
        self.assertEqual(manager.project_messages(history), history)
        self.assertEqual(manager.consume_generation_reason(), "request_view_history_changed")

    def test_request_snapshots_are_not_summary_source(self):
        state = RuntimeState(prompt_snapshot={"system": "private-system"}, request_view={"private": "view"})
        self.assertNotIn("private-system", state.to_summary_source())
        self.assertNotIn("request_view", state.to_summary_source())

    def test_offline_replay_reports_stability_without_inventing_cost_or_quality(self):
        from evals.prefix_cache_replay import replay
        report = replay()
        self.assertLess(report["after"]["history_rewrites"], report["before"]["history_rewrites"])
        self.assertEqual(report["after"]["system_changes"], 0)
        self.assertTrue(report["canonical_tool_results_preserved"])
        self.assertIsNone(report["after"]["total_cost"])
        self.assertFalse(report["after"]["quality_measured"])


if __name__ == "__main__":
    unittest.main()
