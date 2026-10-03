"""No-cost rejection of cuts that cannot meet the existing savings rule."""
from copy import deepcopy
from dataclasses import asdict, replace
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock

from codeagent.context import ContextConfig, ContextManager
from codeagent.context.budget import RequestBudgetError, inspect_request
from codeagent.context.history import history_hash
from codeagent.messages import validate_tool_history
from tests.test_context_simplification import SummaryClient, rounds


class CompactionPreflightTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def manager(self, **options):
        manager = ContextManager(config=ContextConfig(**{
            "summarization_model": "summary", "context_window_tokens": 1_000_000, "near_context_ratio": 0.025,
            "transcript_dir": self.root / "archive", "tool_output_dir": self.root / "outputs",
            **options,
        }))
        manager.begin_turn(0)
        return manager

    def protected_history(self):
        history = rounds(4, size=20)
        history[0]["content"] = "Current requirement: do not run tests.\n" + "background " * 9000
        return history

    def test_protected_user_skips_without_model_archive_mutation_or_cooldown(self):
        manager, history, client = self.manager(), self.protected_history(), SummaryClient()
        original, state = deepcopy(history), asdict(manager.state)
        emitter = Mock()
        for _ in range(2):
            projected = manager.prepare_before_model_call(history, client=client, event_emitter=emitter)
            self.assertEqual(projected, original)
            self.assertEqual(manager.last_compaction["reason"], "insufficient_compressible_history")
            self.assertFalse(manager.last_compaction["summary_called"])
            self.assertLess(manager.last_compaction["max_possible_saved_chars"], manager.last_compaction["required_saved_chars"])
            self.assertGreater(manager.last_compaction["protected_user_chars"], 90000)
        self.assertEqual(client.calls, [])
        self.assertEqual(history, original)
        self.assertEqual(asdict(manager.state), state)
        self.assertFalse(manager.config.transcript_dir.exists())
        self.assertTrue(any(call.args[0] == "context.compaction_skipped" for call in emitter.emit.call_args_list))

    def test_new_turn_reconsiders_old_background_without_waiting_for_cooldown(self):
        manager, history, client = self.manager(), self.protected_history(), SummaryClient()
        manager.prepare_before_model_call(history, client=client)
        self.assertEqual(client.calls, [])
        history.append({"role": "assistant", "content": "Background received."})
        manager.begin_turn(len(history))
        history.append({"role": "user", "content": "New task: preserve this requirement verbatim."})
        history.extend(rounds(4, start=4, size=1000)[1:])
        original = deepcopy(history)
        projected = manager.prepare_before_model_call(history, client=client)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(manager.state.summary_revision, 1)
        self.assertEqual(manager.state.summary_retry_after_epoch, 0)
        self.assertIn("New task: preserve this requirement verbatim.", str(projected))
        self.assertNotIn("background " * 100, str(projected))
        self.assertEqual(history, original)
        validate_tool_history(projected)
        self.assertTrue(Path(manager.state.summary_transcript).is_file())

    def test_preflight_uses_cleaned_view_and_keeps_pairs(self):
        manager = self.manager(near_context_ratio=0.001, investigation_keep_rounds=0)
        history = rounds(5, name="grep", size=10000)
        history[0]["content"] = "active requirement " * 3000
        original, client = deepcopy(history), SummaryClient()
        projected = manager.prepare_before_model_call(history, client=client)
        measured = inspect_request(model="", system="", messages=projected, tools=[], max_tokens=0)
        self.assertEqual(manager.last_compaction["before_request_chars"], measured.request_chars)
        self.assertEqual(client.calls, [])
        self.assertEqual(history, original)
        validate_tool_history(projected)

    def test_skipping_model_does_not_bypass_main_hard_limit(self):
        manager, client = self.manager(context_window_tokens=50000), SummaryClient()
        with self.assertRaises(RequestBudgetError):
            manager.prepare_before_model_call(self.protected_history(), client=client)
        self.assertEqual(client.calls, [])
        self.assertEqual(manager.state.summary_retry_after_epoch, 0)

    def test_optimistic_bound_still_requires_real_savings_check(self):
        manager = self.manager(near_context_ratio=0.001, summary_max_chars=12000)
        history, client = rounds(size=20), SummaryClient("verbose summary " * 600)
        projected = manager.prepare_before_model_call(history, client=client)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(projected, history)
        self.assertEqual(manager.last_compaction["reason"], "insufficient_savings")
        self.assertTrue(manager.last_compaction["summary_called"])
        self.assertEqual(manager.state.summary_revision, 0)
        self.assertGreater(manager.state.summary_retry_after_epoch, 0)
        self.assertFalse(manager.config.transcript_dir.exists())

    def test_summary_input_budget_can_select_smaller_viable_cut(self):
        manager, history, client = self.manager(), rounds(10, size=5000), SummaryClient()
        cut = manager._eligible_cuts(history)[0]
        manager.config.summary_input_max_chars = inspect_request(**manager._summary_params(history[:cut])).request_chars
        manager.force_compact(history, client=client)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(manager.state.compacted_message_count, cut)
        self.assertEqual(manager.state.summary_revision, 1)

    def test_retained_bound_is_optimistic_for_every_legal_cut(self):
        manager, history = self.manager(), rounds(8, name="grep", size=10000)
        history.insert(5, {"role": "user", "content": "Steering: do not change the locked file."})
        for cut in manager._eligible_cuts(history):
            for clean in (False, True):
                state = replace(manager.state, summary_text="Confirmed task state.", compacted_message_count=cut,
                                compacted_prefix_hash=history_hash(history[:cut]))
                actual = ContextManager(config=manager.config, state=state).project_messages(history, clean_tools=clean)
                lower = manager._retained_messages(history, cut)
                if clean:
                    lower = manager._project_tools(lower)
                params = dict(model="main", system="rules", tools=[], max_tokens=2048)
                self.assertLess(inspect_request(messages=lower, **params).request_chars,
                                inspect_request(messages=actual, **params).request_chars)
                self.assertIn("Steering: do not change the locked file.", str(actual))
                validate_tool_history(actual)


if __name__ == "__main__":
    unittest.main()
