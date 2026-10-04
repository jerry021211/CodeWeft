from copy import deepcopy
import unittest

from codeagent.context import ContextConfig, ContextManager
from codeagent.context.telemetry import tool_projection_metrics
from codeagent.context.budget import RequestBudgetError
from codeagent.events import CallbackEventSink, EventEmitter


def result(key, content):
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": key, "content": content}]}


class ContextTelemetryTests(unittest.TestCase):
    def test_savings_exclude_folded_history_and_do_not_mutate_messages(self):
        original = [result("folded", "x" * 1000), result("shortened", "你好" * 100), result("same", "text")]
        projected = [result("shortened", "preview"), result("same", "text")]
        before = deepcopy(original)
        self.assertEqual(tool_projection_metrics(original, projected), {
            "projected_tool_results": 1, "tool_result_chars_saved": 193,
        })
        self.assertEqual(original, before)
        self.assertEqual(tool_projection_metrics(projected, projected)["tool_result_chars_saved"], 0)

    def test_emitted_request_snapshot_uses_selected_model_window(self):
        events = []
        manager = ContextManager(config=ContextConfig(
            context_window_tokens=10000, model_context_windows={"selected": 20000},
        ))
        messages = [{"role": "user", "content": "hello"}]
        projected = manager.prepare_before_model_call(
            messages, model="selected", max_tokens=100, event_emitter=EventEmitter(CallbackEventSink(events.append)),
        )
        self.assertEqual(projected, messages)
        payload = events[-1].payload
        self.assertEqual(events[-1].type, "context.request_projected")
        self.assertEqual(payload["context_window_tokens"], 20000)
        self.assertEqual(payload["output_reserve_tokens"], 100)
        self.assertEqual(payload["canonical_messages"], 1)
        self.assertEqual(payload["projected_tool_results"], 0)
        self.assertTrue(payload["token_count_is_estimate"])
        self.assertNotIn("hello", str(payload))

    def test_unknown_window_is_zero_not_inferred_from_model_name(self):
        events = []
        ContextManager().prepare_before_model_call(
            [{"role": "user", "content": "hello"}], model="unknown-model",
            event_emitter=EventEmitter(CallbackEventSink(events.append)),
        )
        self.assertEqual(events[-1].payload["context_window_tokens"], 0)

    def test_pressure_preserves_current_evidence_and_reports_no_artificial_savings(self):
        events = []
        manager = ContextManager(config=ContextConfig(
            mode="off", context_window_tokens=10_000, investigation_keep_rounds=0,
            tool_clear_min_chars=1000,
        ))
        messages = [{"role": "user", "content": "find matches"},
                    {"role": "assistant", "content": [{"type": "tool_use", "id": "grep-1", "name": "grep", "input": {"pattern": "x"}}]},
                    result("grep-1", "file.py:1:match\n" * 1000)]
        original = deepcopy(messages)
        for _ in range(2):
            projected = manager.prepare_before_model_call(messages, event_emitter=EventEmitter(CallbackEventSink(events.append)))
            self.assertEqual(len(projected[-1]["content"][0]["content"]), len(messages[-1]["content"][0]["content"]))
        self.assertEqual(events[-1].payload["projected_tool_results"], 0)
        self.assertEqual(events[-1].payload["tool_result_chars_saved"], 0)
        self.assertEqual(events[-1].payload["tool_result_chars_saved"], events[-2].payload["tool_result_chars_saved"])
        self.assertEqual(messages, original)

    def test_blocked_request_also_reports_window_and_budget(self):
        events = []
        manager = ContextManager(config=ContextConfig(mode="off", context_window_tokens=1000))
        with self.assertRaises(RequestBudgetError):
            manager.prepare_before_model_call(
                [{"role": "user", "content": "x" * 2000}],
                event_emitter=EventEmitter(CallbackEventSink(events.append)),
            )
        self.assertEqual(events[-1].type, "context.request_blocked")
        self.assertIsNone(events[-1].payload["max_request_chars"])
        self.assertEqual(events[-1].payload["context_window_tokens"], 1000)
        self.assertEqual(events[-1].payload["canonical_messages"], 1)


if __name__ == "__main__":
    unittest.main()
