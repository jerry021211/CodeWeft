"""Only the model window controls full-request capacity and auto-compaction."""
from copy import deepcopy
from dataclasses import replace
import unittest
from unittest.mock import Mock

from codeagent.context.budget import RequestBudgetError, inspect_request
from tests import test_cache_optimization as fixtures
from tests.test_context_simplification import SummaryClient, rounds


class WindowTriggerTests(unittest.TestCase):
    # Reuse only the temporary-directory fixture and manager helper.
    setUp = fixtures.CacheOptimizationTests.setUp
    manager = fixtures.CacheOptimizationTests.manager

    def test_large_characters_do_not_compact_below_window_or_without_window(self):
        for policy, window in (("legacy", 0), ("legacy", 1_000_000), ("cache_friendly", 1_000_000)):
            with self.subTest(policy=policy, window=window):
                manager = self.manager(cache_policy=policy, context_window_tokens=window,
                                       compact_threshold_chars=1, max_request_chars=50_000,
                                       tool_projection_enabled=False)
                history, client = rounds(20, size=2000), SummaryClient()
                original = deepcopy(history)
                emitter = Mock()
                sent = manager.prepare_before_model_call(history, client=client, event_emitter=emitter)
                self.assertEqual(sent, original)
                self.assertEqual(client.calls, [])
                self.assertFalse(manager.state.request_view.get("boundary_config"))
                telemetry = emitter.emit.call_args.args[1]
                self.assertIsNone(telemetry["compact_threshold_chars"])
                self.assertIsNone(telemetry["effective_soft_request_chars"])
                self.assertIsNone(telemetry["compact_target_chars"])

    def test_old_character_cap_is_ignored_even_above_600000_characters(self):
        for window in (0, 1_000_000):
            manager = self.manager(context_window_tokens=window,
                                   max_request_chars=10_000, tool_projection_enabled=False)
            client, history = SummaryClient(), rounds(20, size=32000)
            sent = manager.prepare_before_model_call(history, client=client)
            self.assertEqual(sent, history)
            self.assertGreater(inspect_request(model="", system="", messages=sent, tools=[]).text_request_chars, 600000)
            self.assertEqual(client.calls, [])
            self.assertEqual(manager.state.summary_revision, 0)

    def test_model_window_still_rejects_irreducible_oversized_request(self):
        manager = self.manager(context_window_tokens=1000)
        with self.assertRaises(RequestBudgetError) as error:
            manager.prepare_before_model_call([{"role": "user", "content": "x" * 3000}])
        self.assertEqual(error.exception.reason, "context_window_tokens")

    def test_token_thresholds_include_output_reserve_and_ignore_characters(self):
        manager = self.manager()
        budget = inspect_request(model="", system="", messages=[], tools=[], max_tokens=200)
        for chars in (1, 2_000_000):
            legacy = replace(budget, request_chars=chars, estimated_prompt_tokens=599, estimated_total_tokens=799)
            self.assertFalse(manager._under_pressure(legacy, 1000))
            self.assertTrue(manager._under_pressure(replace(legacy, estimated_prompt_tokens=720, estimated_total_tokens=920), 1000))
            friendly = replace(legacy, estimated_prompt_tokens=719, estimated_total_tokens=839)
            self.assertFalse(manager._under_pressure(friendly, 1000, cache_friendly=True))
            self.assertTrue(manager._under_pressure(replace(friendly, estimated_prompt_tokens=720), 1000, cache_friendly=True))
            self.assertFalse(manager._under_pressure(friendly, 0, cache_friendly=True))

    def test_window_pressure_still_compacts_real_history(self):
        for policy in ("legacy", "cache_friendly"):
            manager = self.manager(cache_policy=policy, context_window_tokens=23_000,
                                   compact_threshold_chars=600_000, tool_projection_enabled=False)
            history, client = rounds(20, size=2000), SummaryClient()
            original = deepcopy(history)
            sent = manager.prepare_before_model_call(history, client=client)
            self.assertTrue(client.calls)
            self.assertGreater(manager.state.summary_revision, 0)
            self.assertEqual(history, original)
            budget = inspect_request(model="", system="", messages=sent, tools=[], max_tokens=0)
            self.assertFalse(manager._under_pressure(budget, 23_000, cache_friendly=policy == "cache_friendly"))

    def test_model_discovery_unknown_overrides_configured_window(self):
        manager = self.manager(context_window_tokens=100, compact_threshold_chars=1)
        client, history = SummaryClient(), rounds(20, size=2000)
        manager.prepare_before_model_call(history, client=client,
            model_window={"context_window_tokens": 0, "context_window_source": "unavailable"})
        self.assertEqual(client.calls, [])
