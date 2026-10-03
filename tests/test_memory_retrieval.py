from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from codeagent import Agent, AgentConfig, MemoryConfig, MemoryManager, MemoryStore, ModelResponse
from codeagent.events import CallbackEventSink, EventEmitter
from codeagent.context.budget import BoundModelClient, RequestBudgetError
from codeagent.memory import MemoryAccessController
from codeagent.memory.manager import _retrieval_query
from codeagent.memory.models import MemoryRecord
from codeagent.memory.retrieval import INDEX_NAME, terms
from codeagent.tools.registry import ToolRegistry


class Selector:
    def __init__(self, names=(), *, raw=None, callback=None):
        self.names = names
        self.raw = raw
        self.callback = callback
        self.calls = []

    def create_message(self, **kwargs):
        self.calls.append(kwargs)
        if self.callback:
            self.callback()
        text = self.raw if self.raw is not None else json.dumps({"selected_memories": self.names})
        return ModelResponse("end_turn", [{"type": "text", "text": text}])


class MemoryRetrievalTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = MemoryStore(self.root / "memory")
        self.events = []
        self.emitter = EventEmitter(CallbackEventSink(self.events.append))

    def remember(self, name="SSE Policy", content="事件断线后应继续回放，依据序号去重。", description="项目约定"):
        return self.store.remember(name=name, description=description, content=content)

    def select(self, query, client, config=None, **kwargs):
        return MemoryManager(self.store, config or MemoryConfig()).select_context(
            [{"role": "user", "content": query}], current_query=query,
            client=client, model="scripted", max_tokens=800,
            event_emitter=self.emitter, **kwargs,
        )

    def trace(self):
        return self.events[-1].payload

    def test_relevant_record_beyond_first_fifty_is_selected_and_traced(self):
        self.store.root.mkdir()
        for i in range(70):
            record = MemoryRecord(name=f"a-{i:03}", description="Unrelated filler", content="background")
            (self.store.root / f"a-{i:03}.md").write_text(self.store._serialize(record), encoding="utf-8")
        target = self.remember("z-needle", "NEEDLE_ROUTING uses route_v2.")
        self.assertNotIn(target.filename, [r.filename for r in self.store.list_memories()[:50]])
        client = Selector([target.filename])
        context = self.select("NEEDLE_ROUTING", client)
        self.assertIn("route_v2", context)
        trace = self.trace()
        self.assertEqual(trace["candidate_ids"], [target.filename])
        self.assertEqual(trace["selected_ids"], [target.filename])
        self.assertEqual(trace["injected_ids"], [target.filename])
        self.assertEqual(trace["backend"], "sqlite_fts5")
        self.assertNotIn("NEEDLE_ROUTING", json.dumps(trace))
        self.assertNotIn("route_v2", json.dumps(trace))
        self.assertEqual(trace["status"], "injected")

    def test_chinese_sentence_and_manual_search_find_same_record(self):
        record = self.remember()
        for query in ("事件", "事件 回放", "断线后如何回放事件"):
            with self.subTest(query=query):
                self.assertEqual([r.filename for r in self.store.search(query)], [record.filename])
        self.assertFalse((self.store.root / INDEX_NAME).exists())

    def test_code_symbols_paths_and_query_syntax_are_literal(self):
        record = self.remember("Scheduler", "RunScheduler lives in codeagent/web/scheduler.py")
        for query in ("RunScheduler", "run scheduler", "codeagent/web/scheduler.py", 'RunScheduler\" OR (NOT *)'):
            with self.subTest(query=query):
                self.assertEqual(self.store.retrieve(query).hits[0].record.filename, record.filename)
        self.assertIn("runscheduler", terms("RunScheduler"))
        self.assertIn("scheduler", terms("RunScheduler"))

    def test_no_candidates_and_empty_query_skip_model_call(self):
        self.remember()
        client = Selector()
        self.assertEqual(self.select("QUASAR_UNRELATED_937", client), "")
        self.assertEqual(client.calls, [])
        self.assertEqual(self.trace()["status"], "no_candidates")
        self.assertEqual(self.select("", client), "")
        self.assertEqual(self.trace()["retrieval_reason"], "empty_query")
        self.assertEqual(client.calls, [])

    def test_evidence_snippet_reaches_match_deep_inside_body(self):
        record = self.remember(content="irrelevant filler. " * 200 + "TAIL_CONVENTION is the critical rule.")
        client = Selector([record.filename])
        self.select("TAIL_CONVENTION", client)
        prompt = client.calls[0]["messages"][0]["content"]
        catalog = json.loads(prompt.split("长期记忆清单：\n")[1])
        self.assertIn("TAIL_CONVENTION", catalog[0]["evidence"])
        self.assertLessEqual(len(catalog[0]["evidence"]), 502)

    def test_long_current_query_tail_is_preserved_and_truncation_traced(self):
        record = self.remember(content="TAIL_CONVENTION must be applied.")
        query = "irrelevant background. " * 1000 + "TAIL_CONVENTION"
        client = Selector([record.filename])
        context = self.select(query, client)
        self.assertIn("TAIL_CONVENTION", context)
        self.assertTrue(self.trace()["query_truncated"])
        self.assertIn("TAIL_CONVENTION", client.calls[0]["messages"][0]["content"])

    def test_followup_uses_previous_user_intent_but_never_tool_or_injected_memory(self):
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "INJECTED_MEMORY_NOISE"},
                                         {"type": "text", "text": "处理事件回放"}]},
            {"role": "assistant", "content": "ASSISTANT_NOISE"},
            {"role": "user", "content": [{"type": "tool_result", "content": "TOOL_NOISE"}]},
            {"role": "user", "content": "继续"},
        ]
        query, _ = _retrieval_query(messages, "继续")
        self.assertIn("处理事件回放", query)
        self.assertNotIn("NOISE", query)
        query, _ = _retrieval_query(messages, "新任务")
        self.assertEqual(query, "新任务")

    def test_warm_index_does_not_reread_unchanged_markdown(self):
        self.remember()
        first = self.store.retrieve("事件")
        self.assertEqual(first.files_read, 1)
        with patch.object(self.store, "_read_record", side_effect=AssertionError("No full-body reread")):
            warm = self.store.retrieve("事件")
        self.assertEqual(warm.files_read, 0)
        self.assertEqual(warm.hits[0].version, first.hits[0].version)
        self.assertEqual(warm.revision, first.revision)

    def test_external_edit_rename_delete_and_new_file_refresh_index(self):
        record = self.remember(content="uniqueoldalpha")
        self.store.retrieve("uniqueoldalpha")
        path = self.store.root / record.filename
        path.write_text(path.read_text(encoding="utf-8").replace("uniqueoldalpha", "uniquenewbetalonger"), encoding="utf-8")
        changed = self.store.retrieve("uniquenewbetalonger")
        self.assertEqual(changed.files_read, 1)
        self.assertEqual(len(changed.hits), 1)
        self.assertEqual(self.store.retrieve("uniqueoldalpha").hits, [])
        path.rename(self.store.root / "renamed.md")
        self.assertEqual(self.store.retrieve("uniquenewbetalonger").hits[0].record.filename, "renamed.md")
        (self.store.root / "renamed.md").unlink()
        self.assertEqual(self.store.retrieve("uniquenewbetalonger").hits, [])
        self.remember("Another", "uniquenewbetalonger")
        self.assertEqual([h.record.filename for h in self.store.retrieve("uniquenewbetalonger").hits], ["another.md"])

    def test_full_verification_catches_same_size_edit_with_restored_mtime(self):
        record = self.remember(content="uniqueold")
        self.store.retrieve("uniqueold")
        path = self.store.root / record.filename
        before = path.stat()
        path.write_text(path.read_text(encoding="utf-8").replace("uniqueold", "uniquenew"), encoding="utf-8")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        result = self.store.retrieve("uniquenew", verify_seconds=0)
        self.assertTrue(result.full_verification)
        self.assertEqual(len(result.hits), 1)
        self.assertEqual(self.store.retrieve("uniqueold").hits, [])

    def test_readonly_refresh_uses_private_snapshot_without_changing_disk(self):
        record = self.remember(content="oldkeyword")
        self.store.retrieve("oldkeyword")
        cache = self.store.root / INDEX_NAME
        before = cache.read_bytes()
        path = self.store.root / record.filename
        path.write_text(path.read_text(encoding="utf-8").replace("oldkeyword", "newkeywordlonger"), encoding="utf-8")
        readonly = self.store.retrieve("newkeywordlonger", allow_index_write=False)
        self.assertEqual(readonly.backend, "sqlite_memory")
        self.assertEqual(len(readonly.hits), 1)
        self.assertEqual(cache.read_bytes(), before)

    def test_active_team_cannot_create_index_but_can_retrieve(self):
        self.remember()
        controller = MemoryAccessController(lambda _: True)
        self.store.access_policy = controller.policy(self.root)
        result = self.store.retrieve("事件")
        self.assertEqual(result.backend, "sqlite_memory")
        self.assertEqual(len(result.hits), 1)
        self.assertFalse((self.store.root / INDEX_NAME).exists())

    def test_corrupt_index_falls_back_without_overwriting_it_or_markdown(self):
        self.remember()
        cache = self.store.root / INDEX_NAME
        cache.write_bytes(b"not a SQLite database")
        before = {p.name: p.read_bytes() for p in self.store.root.iterdir()}
        result = self.store.retrieve("事件")
        self.assertEqual(len(result.hits), 1)
        self.assertEqual(result.reason, "index_unavailable")
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.store.root.iterdir()})

    def test_missing_fts_uses_python_bm25(self):
        self.remember()
        with patch("codeagent.memory.retrieval._schema", side_effect=sqlite3.OperationalError("no such module: fts5")):
            result = self.store.retrieve("断线后如何回放事件")
        self.assertEqual(result.backend, "python_bm25")
        self.assertEqual(result.hits[0].record.filename, "sse-policy.md")

    def test_outside_index_symlink_is_not_followed(self):
        self.remember()
        outside = self.root / "outside.db"
        outside.write_bytes(b"must not touch")
        try:
            (self.store.root / INDEX_NAME).symlink_to(outside)
        except OSError:
            self.skipTest("Host does not permit symlink creation")
        self.assertEqual(len(self.store.retrieve("事件").hits), 1)
        self.assertEqual(outside.read_bytes(), b"must not touch")

    def test_changed_or_deleted_record_after_selection_is_not_injected(self):
        for mutation in ("change", "delete"):
            with self.subTest(mutation=mutation):
                record = self.remember()
                def mutate():
                    path = self.store.root / record.filename
                    if mutation == "delete":
                        path.unlink()
                    else:
                        path.write_text(path.read_text(encoding="utf-8") + "\nchanged", encoding="utf-8")
                context = self.select("事件", Selector([record.filename], callback=mutate))
                self.assertEqual(context, "")
                self.assertEqual(self.trace()["selected_ids"], [record.filename])
                self.assertEqual(self.trace()["injected_ids"], [])
                reason = "missing_or_invalid" if mutation == "delete" else "changed_since_retrieval"
                self.assertEqual(self.trace()["skipped"][0]["reason"], reason)

    def test_budget_skips_large_record_and_still_injects_small_record(self):
        big = self.remember("Big", "convention " * 300)
        small = self.remember("Small", "convention: keep IDs")
        context = self.select("convention", Selector([big.filename, small.filename]), MemoryConfig(session_budget_chars=350))
        self.assertLessEqual(len(context), 350)
        self.assertEqual(self.trace()["selected_ids"], [big.filename, small.filename])
        self.assertEqual(self.trace()["injected_ids"], [small.filename])
        self.assertEqual(self.trace()["skipped"][0], {"id": big.filename, "reason": "budget"})

    def test_parse_failure_empty_selection_and_api_failure_have_distinct_status(self):
        self.remember()
        self.select("事件", Selector(raw="not json"))
        self.assertEqual(self.trace()["status"], "selection_parse_error")
        self.select("事件", Selector())
        self.assertEqual(self.trace()["status"], "selected_empty")
        def fail():
            raise RuntimeError("API failed with secret body")
        with self.assertRaises(RuntimeError):
            self.select("事件", Selector(callback=fail))
        self.assertEqual(self.trace()["status"], "error")
        self.assertNotIn("secret body", json.dumps(self.trace()))

    def test_model_cannot_select_non_candidates_or_path_aliases(self):
        record = self.remember()
        unrelated = self.remember("Other", "QUASAR_MARKER")
        self.select("事件", Selector(["../" + record.filename, unrelated.filename, record.filename, record.filename]))
        self.assertEqual(self.trace()["selected_ids"], [record.filename])
        self.assertEqual(len(self.trace()["skipped"]), 3)

    def test_legacy_mode_remains_available_for_control_experiments(self):
        record = self.remember()
        context = self.select("totally unrelated", Selector([record.filename]), MemoryConfig(retrieval_mode="legacy"))
        self.assertIn(record.content, context)
        self.assertEqual(self.trace()["backend"], "legacy")
        self.assertFalse((self.store.root / INDEX_NAME).exists())

    def test_default_maintenance_and_catalog_do_not_scan_memory_again(self):
        self.remember()
        manager = MemoryManager(self.store)
        with patch.object(self.store, "list_memories", side_effect=AssertionError("Unexpected full scan")):
            self.assertEqual(manager.catalog_prompt(), "")
            manager.after_turn([], client=None, model="fake", max_tokens=10)

    def test_invalid_config_fails_early(self):
        for kwargs in ({"retrieval_mode": "unknown"}, {"index_verify_seconds": -1}, {"index_verify_seconds": float("nan")}):
            with self.assertRaises(ValueError):
                MemoryConfig(**kwargs)

    def test_index_schema_version_change_rebuilds_from_markdown(self):
        self.remember()
        self.store.retrieve("事件")
        with closing(sqlite3.connect(self.store.root / INDEX_NAME)) as connection:
            with connection:
                connection.execute("UPDATE index_meta SET value='old-version' WHERE key='version'")
        result = self.store.retrieve("事件")
        self.assertEqual(len(result.hits), 1)
        self.assertEqual(result.files_read, 1)

    def test_independent_store_instances_can_query_same_index_concurrently(self):
        self.remember()
        stores = [MemoryStore(self.store.root) for _ in range(4)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda store: store.retrieve("事件"), stores))
        self.assertTrue(all(result.hits[0].record.filename == "sse-policy.md" for result in results))

    def test_indexed_selection_still_enforces_side_request_budget(self):
        self.remember()
        client = Selector()
        bounded = BoundModelClient(client, max_request_chars=50)
        with self.assertRaises(RequestBudgetError):
            self.select("事件", bounded)
        self.assertEqual(client.calls, [])
        self.assertEqual(self.trace()["error_type"], "RequestBudgetError")

    def test_agent_passes_explicit_query_and_discuss_disables_index_writes(self):
        record = self.remember()
        class Client:
            calls = []
            def fork(inner, **kwargs):
                return inner
            def create_message(inner, **kwargs):
                inner.calls.append(kwargs)
                text = json.dumps({"selected_memories": [record.filename]}) if len(inner.calls) == 1 else "done"
                return ModelResponse("end_turn", [{"type": "text", "text": text}])
        agent = Agent(client=Client(), tools=ToolRegistry(), config=AgentConfig(model="fake"),
                      memory_manager=MemoryManager(self.store))
        agent.set_read_only(True)
        result = agent.run("断线后如何回放事件")
        self.assertEqual(result.final_text, "done")
        self.assertFalse((self.store.root / INDEX_NAME).exists())
        self.assertIn("事件断线后", json.dumps(agent.messages, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
