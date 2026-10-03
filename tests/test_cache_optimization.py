"""Offline regressions for searchable archives and bounded cache-aware recovery."""
from copy import deepcopy
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from codeagent.config import EnvironmentConfig
from codeagent.context import ContextConfig, ContextManager, ContextCompactionError
from codeagent.context.budget import inspect_request, RequestBudgetError
from codeagent.messages import ToolUse, validate_tool_history
from codeagent.tools.runtime_data import LoadToolOutputTool
from codeagent.web.factory import serialize_runtime_state, _restore_runtime_state
from tests.test_context_simplification import SummaryClient, rounds
from tests.test_summary_character_budget import ScriptedClient


class ArchiveSearchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / "result.txt"
        self.tool = LoadToolOutputTool(self.root)

    def write(self, text):
        self.path.write_text(text, encoding="utf-8")

    def search_all(self, query, **options):
        cursor = {}
        found = []
        for _ in range(100):
            result = self.tool.run(str(self.path), query=query, **options, **cursor)
            found.extend((int(a), int(b)) for a, b in re.findall(r"line=(\d+) char_offset=(\d+)", result))
            self.assertLessEqual(len(result), 16000)
            if "search_complete=true" in result:
                return found
            match = re.search(r"next_offset=(\d+) next_char_offset=(\d+)", result)
            self.assertIsNotNone(match, result)
            cursor = dict(offset=int(match[1]), char_offset=int(match[2]))
        self.fail("search cursor failed to advance")

    def test_literal_search_pages_and_readback_use_original_positions(self):
        self.write("noise\n甲.*乙.*丙\n.*last\n")
        self.assertEqual(self.search_all(".*", max_matches=1), [(2, 1), (2, 4), (3, 0)])
        result = self.tool.run(str(self.path), offset=2, char_offset=4, char_limit=2)
        self.assertIn("2\t.*", result)
        self.assertIn("matches=0", self.tool.run(str(self.path), query="LAST"))

    def test_chunk_and_scan_boundaries_in_huge_unicode_lines_do_not_drop_matches(self):
        text = "甲" * 8190 + "NEEDLE" + "乙" * 8190 + "NEEDLE\nNEEDLE"
        self.write(text)
        expected = [(1, 8190), (1, 16386), (2, 0)]
        self.assertEqual(self.search_all("NEEDLE"), expected)
        self.assertEqual(self.search_all("NEEDLE", scan_limit_chars=8192), expected)

    def test_overlapping_matches_and_scan_budget_are_explicit(self):
        self.write("a" * 1023 + "aaaa" + "x" * 2000)
        self.assertEqual(self.search_all("aaa", max_matches=20, scan_limit_chars=1024),
                         [(1, i) for i in range(1025)])
        result = self.tool.run(str(self.path), query="absent", scan_limit_chars=1024)
        self.assertIn("search_complete=false", result)
        self.assertIn("scanned_chars=1024", result)

    def test_search_obeys_private_scope_and_validates_inputs(self):
        self.write("own data")
        other = self.root / "other"
        other.mkdir()
        private = other / "secret.txt"
        private.write_text("secret", encoding="utf-8")
        scoped = LoadToolOutputTool(other)
        self.assertIn("not allowed", scoped.run(str(self.path), query="own"))
        self.assertIn("not allowed", scoped.run("../result.txt", query="own"))
        for options in ({"query": ""}, {"query": "x\ny"}, {"query": "x", "max_matches": True},
                        {"query": "x", "scan_limit_chars": 0}):
            self.assertIn("not allowed", self.tool.run(str(self.path), **options))


class CacheOptimizationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def manager(self, **options):
        manager = ContextManager(config=ContextConfig(**{
            "summarization_model": "summary", "transcript_dir": self.root / "history",
            "tool_output_dir": self.root / "outputs", **options}))
        manager.begin_turn(0)
        return manager

    def test_command_ingress_saves_input_keeps_status_and_searchable_original(self):
        manager = self.manager()
        output = ("start\n" + "log line\n" * 3000 + "FAILED case_middle\n" + "log line\n" * 3000
                  + "\n[stderr]\ntest failed\n[exit code: 2]")
        tool = ToolUse(id="command_1", name="bash", input={"command": "test"})
        preview = manager.finalize_tool_results([tool], [output])[0]
        self.assertLess(len(preview), 3000)
        self.assertIn("start", preview)
        self.assertIn("[exit code: 2]", preview)
        self.assertNotIn("FAILED case_middle", preview)
        self.assertEqual((manager.config.tool_output_dir / "command_1.txt").read_text(encoding="utf-8"), output)
        self.assertIn("FAILED case_middle", LoadToolOutputTool(manager.config.tool_output_dir).run(
            "command_1.txt", query="FAILED"))
        file_read = ToolUse(id="read_1", name="read_file", input={"path": "file.py"})
        self.assertEqual(manager.finalize_tool_results([file_read], [output]), [output])
        manager.config.command_output_max_chars = 0
        self.assertEqual(manager.finalize_tool_results([tool], [output]), [output])

    def test_archive_failure_retains_existing_bounded_fallback(self):
        manager = self.manager()
        with patch.object(manager, "_write_tool_output", side_effect=OSError("disk full")):
            preview = manager.finalize_tool_results([ToolUse("x", "bash", {})], ["x" * 20000])[0]
        self.assertIn("归档失败", preview)
        self.assertLessEqual(len(preview), manager.config.command_output_max_chars)

    def test_summary_reserves_configured_tokens_before_any_paid_call(self):
        manager = self.manager(summary_max_tokens=2048)
        request = manager._summary_params(rounds(4))
        budget = inspect_request(**request)
        self.assertEqual(budget.output_reserve_tokens, 2048)
        manager.config.summary_context_window_tokens = budget.estimated_prompt_tokens + 2047
        client = SummaryClient()
        with self.assertRaises(RequestBudgetError):
            manager._model_summary([], client=client, params=request)
        self.assertEqual(client.calls, [])

    def test_hard_retry_is_persisted_and_failure_cannot_send_oversized_request(self):
        manager = self.manager(max_request_chars=15000, context_window_tokens=10_000, tool_projection_enabled=False)
        history = rounds(10, size=2000)
        client = ScriptedClient(RuntimeError("unavailable"), RuntimeError("still unavailable"))
        with self.assertRaises(ContextCompactionError):
            manager.force_compact(history, client=client)
        for _ in range(3):
            manager = ContextManager(config=manager.config,
                                     state=_restore_runtime_state(serialize_runtime_state(manager.state)))
            with self.assertRaises(RequestBudgetError):
                manager.prepare_before_model_call(history, client=client)
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(manager.state.summary_recovery_attempted)
        self.assertEqual(manager.state.summary_revision, 0)

    def test_manual_and_hard_recovery_can_escape_a_soft_failure(self):
        for manual in (True, False):
            manager = self.manager(max_request_chars=15000, context_window_tokens=10_000, tool_projection_enabled=False)
            history, client = rounds(10, size=2000), ScriptedClient(RuntimeError("temporary"), "good summary")
            original = deepcopy(history)
            with self.assertRaises(ContextCompactionError):
                manager.force_compact(history, client=client)
            if manual:
                result = manager.force_compact(history, client=client)
            else:
                result = manager.prepare_before_model_call(history, client=client)
            self.assertEqual(len(client.calls), 2)
            self.assertEqual(manager.state.summary_revision, 1)
            self.assertFalse(manager.state.summary_recovery_attempted)
            self.assertEqual(history, original)
            validate_tool_history(result)

    def test_recovery_flag_is_optional_for_old_checkpoints(self):
        restored = _restore_runtime_state({"summary_text": "old", "history_generation": 7})
        self.assertFalse(restored.summary_recovery_attempted)
        self.assertEqual(restored.history_generation, 7)

    def test_compaction_creates_headroom_and_restored_prefix_survives_growth(self):
        manager = self.manager(cache_policy="cache_friendly", context_window_tokens=25_000,
                               max_request_chars=50000, max_fold_rounds=3, tool_projection_enabled=False)
        history, client = rounds(20, size=2000), SummaryClient()
        original = deepcopy(history)
        sent = manager.prepare_before_model_call(history, client=client)
        budget = inspect_request(model="", system="", tools=[], messages=sent, max_tokens=0)
        self.assertLessEqual(budget.estimated_prompt_tokens, 17500)
        self.assertGreaterEqual(len(client.calls), 2)
        self.assertLessEqual(len(client.calls), 3)
        self.assertEqual(history, original)
        restored = ContextManager(config=manager.config,
                                  state=_restore_runtime_state(serialize_runtime_state(manager.state)))
        history += rounds(1, start=20, size=500)[1:]
        next_sent = restored.prepare_before_model_call(history, client=client)
        self.assertEqual(next_sent[:len(sent)], sent)
        validate_tool_history(next_sent)
        self.assertEqual(restored.state.summary_revision, manager.state.summary_revision)

    def test_new_configuration_is_validated_and_loaded(self):
        with patch.dict("os.environ", {"MODEL_ID": "main", "SUMMARIZATION_MODEL_ID": "summary",
                                      "CONTEXT_SUMMARY_MAX_TOKENS": "4096",
                                      "CONTEXT_COMMAND_OUTPUT_MAX_CHARS": "9000"}, clear=True), \
                patch("codeagent.config._load_dotenv"):
            config = EnvironmentConfig.from_env().context_config
        self.assertEqual(config.summary_max_tokens, 4096)
        self.assertEqual(config.command_output_max_chars, 9000)
        for options in ({"summary_max_tokens": 0}, {"summary_max_tokens": True},
                        {"command_output_max_chars": -1}):
            with self.assertRaises(ValueError):
                ContextConfig(**options)


if __name__ == "__main__":
    unittest.main()
