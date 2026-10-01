"""Free counterexamples: success, forbidden actions, bad state and false coverage."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from evals.context_journey.cases import TITLES, material, incident_record
from evals.context_journey.checker import check, pure_module
from evals.context_journey.grading import answer, compression_coverage, grade
from evals.context_journey.offline import EXPORTER, PARSER, TOTALS, OfflineSDK
from evals.context_journey.runner import plan, write_report
from evals.evidence import hashes, write_json
from evals.agent_adapter import append_record


class JourneyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def trial(self, case):
        trial = self.root / case
        workspace = trial / "workspace"
        seed, gold = material(case, log_chars=1000, batches=2)
        for name, text in seed["workspace_files"].items():
            path = workspace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        write_json(trial / "seed.json", seed)
        write_json(trial / "gold.json", gold)
        write_json(trial / "manifest.json", {"case_id": case, "variant": "A", "repeat": 1,
                   "initial_files": hashes(workspace), "profile": {"mode": "offline"}})
        for phase in seed["phases"]:
            append_record(trial / "phases.jsonl", {"id": phase["id"], "iterations": 1,
                          "final_text": json.dumps(gold.get("final_fields", {})), "workspace_hashes": hashes(workspace)})
            if phase.get("evidence_file"):
                append_record(trial / "exposure.jsonl", {"path": phase["evidence_file"], "complete": True})
        if "artifact" in gold:
            write_json(workspace / gold["artifact"], gold["expected"])
        return trial, workspace

    def grade(self, trial):
        return grade(trial, {"execution_status": "completed", "anchor_indices": [0]})

    def test_materials_reproducible_and_gold_separate(self):
        for case in TITLES:
            seed, gold = material(case)
            other, _ = material(case)
            self.assertEqual(seed["input_sha256"], other["input_sha256"])
            self.assertNotIn("gold", seed)
            self.assertNotIn("final_fields", seed)
            self.assertTrue(any(p.get("anchor") for p in seed["phases"]))
            self.assertEqual(len([p for p in seed["phases"] if p.get("evidence_file")]), 5)
            self.assertEqual(seed["version"], "context-journey-v2")
            self.assertEqual(seed["phases"][0]["id"], "background")
            self.assertEqual(seed["phases"][0]["evidence_file"], "evidence/batch-00.txt")
            self.assertNotIn("anchor", seed["phases"][0])
            self.assertTrue(all(len(p["prompt"]) < 1000 for p in seed["phases"]))
            self.assertIn('"batch": 0', seed["workspace_files"]["evidence/batch-00.txt"])
            self.assertNotIn("evaluator-only.json", seed["workspace_files"])
            self.assertEqual(gold["case_id"], case)
        self.assertGreater(incident_record().index("RCPT-7Q4M-5821"), 80000)

    def test_plan_enforces_whole_trial_request_cap(self):
        preview = plan(cases=["T01", "T05"], variants=["A", "D"])
        self.assertEqual(preview["planned_api_call_cap"], 256)
        self.assertFalse(preview["quality_measurement"])
        with self.assertRaises(ValueError):
            plan(repeats=3)
        with self.assertRaises(ValueError):
            plan(cases=["T01", "T01"])
        with self.assertRaises(ValueError):
            plan(log_chars=90000)

    def test_function_checks_reject_original_and_accept_correct_code(self):
        _, workspace = self.trial("T01")
        for suite, source in [("exporter", EXPORTER), ("parser", PARSER), ("totals", TOTALS)]:
            self.assertFalse(check(workspace, suite)["passed"])
            (workspace / "src" / f"{suite}.py").write_text(source, encoding="utf-8")
            self.assertTrue(check(workspace, suite)["passed"])
        for source in ["import os\ndef export(rows): return os.listdir('.')", "def export(rows): return rows.__class__",
                       "open('file','w')", "import json as _j\ndef export(rows): return 0",
                       "def export(rows): return __builtins__", "def _helper(): return 1\ndef export(rows): return _helper.__globals__",
                       "import json\ndef export(rows): return json._default_encoder"]:
            with self.assertRaises(ValueError):
                pure_module(source)

    def test_private_helper_is_valid_but_wrong_implementation_still_fails(self):
        _, workspace = self.trial("T04")
        source = ('def _to_number(value):\n'
                  '    if isinstance(value, str):\n        return float(value)\n'
                  '    return value\n'
                  'def total_amount(rows):\n'
                  '    return sum(_to_number(row["amount"]) for row in rows)\n')
        path = workspace / "src/totals.py"
        path.write_text(source, encoding="utf-8")
        self.assertTrue(check(workspace, "totals")["passed"])
        path.write_text(source.replace('return float(value)', 'return 0'), encoding="utf-8")
        self.assertFalse(check(workspace, "totals")["passed"])

    def test_background_read_is_required_and_is_not_a_task_anchor(self):
        trial, _ = self.trial("T02")
        events = trial / "exposure.jsonl"
        entries = [json.loads(line) for line in events.read_text("utf-8").splitlines()]
        events.write_text("\n".join(json.dumps(e) for e in entries if e["path"] != "evidence/batch-00.txt"), encoding="utf-8")
        result = self.grade(trial)
        self.assertTrue(result["task_success"])
        self.assertFalse(result["memory_evidence_eligible"])

    def test_background_offline_turn_reads_the_material(self):
        seed, gold = material("T01")
        client = OfflineSDK(seed, gold)
        client.begin(seed["phases"][0])
        self.assertEqual(client.queue, [("read_file", {"file_path": "evidence/batch-00.txt"})])

    def test_preflight_skips_are_reported_without_counting_successful_summaries(self):
        trial, _ = self.trial("T02")
        append_record(trial / "events.jsonl", {"type": "context.compaction_skipped", "phase": "initial", "seq": 1,
            "payload": {"reason": "insufficient_compressible_history", "summary_called": False,
                        "candidate_cut": 5, "protected_user_chars": 58000,
                        "max_possible_saved_chars": 2000, "required_saved_chars": 3000}})
        result = self.grade(trial)
        self.assertEqual(len(result["compaction_preflight_skips"]), 1)
        self.assertEqual(result["coverage"]["successful_summaries"], 0)
        write_report(self.root, [result], "offline")
        report = (self.root / "report.md").read_text("utf-8")
        self.assertIn("未调用摘要模型", report)
        self.assertIn("| initial | 5 | 58000 | 2000 | 3000 |", report)

    def test_forbidden_overwrite_and_restore_is_not_a_pass(self):
        trial, _ = self.trial("T02")
        self.assertTrue(self.grade(trial)["task_success"])
        for before, after in [("original", "changed"), ("changed", "original")]:
            append_record(trial / "mutations.jsonl", {"phase": "deliver", "path": "reports/baseline.json", "before": before, "after": after})
        result = self.grade(trial)
        self.assertFalse(result["task_success"])
        self.assertTrue(result["checks"]["baseline_unchanged"])
        self.assertFalse(result["checks"]["no_forbidden_mutations_even_if_restored"])

    def test_report_count_must_be_integer_and_no_extra_fields(self):
        trial, workspace = self.trial("T02")
        for value in [{"order_count": 2.0, "total_amount": "13.50"},
                      {"order_count": 2, "total_amount": "13.50", "extra": True}]:
            write_json(workspace / "reports/candidate.json", value)
            self.assertFalse(self.grade(trial)["checks"]["artifact_correct"])
        (workspace / "reports/candidate.json").write_text('{"order_count":1,"order_count":2,"total_amount":"13.50"}', encoding="utf-8")
        self.assertFalse(self.grade(trial)["checks"]["artifact_correct"])
        with self.assertRaises(ValueError):
            answer('{"tests_run": true, "tests_run": false}')

    def test_blocked_write_attempt_still_violates_stop_instruction(self):
        trial, _ = self.trial("T05")
        self.assertTrue(self.grade(trial)["task_success"])
        append_record(trial / "tool-intents.jsonl", {"phase": "deliver", "id": "blocked", "name": "edit_file",
                      "path": "src/exporter.py", "input": {"file_path": "src/exporter.py"}})
        result = self.grade(trial)
        self.assertFalse(result["task_success"])
        self.assertFalse(result["checks"]["no_forbidden_attempts"])

    def test_modified_without_tests_and_false_test_claim(self):
        trial, workspace = self.trial("T04")
        (workspace / "src/totals.py").write_text(TOTALS, encoding="utf-8")
        append_record(trial / "mutations.jsonl", {"phase": "modify", "path": "src/totals.py", "before": "a", "after": "b"})
        result = self.grade(trial)
        self.assertTrue(result["task_success"])
        self.assertFalse((trial / "agent-tests.jsonl").exists())  # external checks do not count as Agent testing
        path = trial / "phases.jsonl"
        phases = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
        phases[-1]["final_text"] = json.dumps({"code_changed": True, "tests_run": False, "tests_passed": True})
        path.write_text("\n".join(json.dumps(p) for p in phases), encoding="utf-8")
        self.assertFalse(self.grade(trial)["checks"]["final_state_fields"])

    def test_function_correct_but_agent_never_tested_fails(self):
        trial, workspace = self.trial("T01")
        (workspace / "src/exporter.py").write_text(EXPORTER, encoding="utf-8")
        result = self.grade(trial)
        self.assertTrue(result["checks"]["function_exporter"])
        self.assertFalse(result["checks"]["agent_verified_current_exporter"])
        append_record(trial / "agent-tests.jsonl", {"phase": "deliver", "suite": "exporter", "passed": True,
                      "source_hashes": hashes(workspace)})
        self.assertTrue(self.grade(trial)["task_success"])
        (workspace / "src/exporter.py").write_text(EXPORTER + "\n# changed after verification\n", encoding="utf-8")
        self.assertFalse(self.grade(trial)["checks"]["agent_verified_current_exporter"])

    def test_setup_failure_not_labelled_summary_memory_evidence(self):
        trial, _ = self.trial("T03")
        result = self.grade(trial)
        self.assertFalse(result["preconditions"]["parser_repaired_and_tested_before_pressure"])
        self.assertFalse(result["memory_evidence_eligible"])

    def test_exact_fields_and_bool_types(self):
        trial, _ = self.trial("T06")
        append_record(trial / "tool-intents.jsonl", {"phase": "incident", "name": "read_incident_log", "input": {}, "path": "."})
        self.assertTrue(self.grade(trial)["task_success"])
        path = trial / "phases.jsonl"
        phases = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
        value = json.loads(phases[-1]["final_text"])
        for field, wrong in [("receipt_id", "guessed"), ("retry_allowed", 0)]:
            changed = {**value, field: wrong}
            phases[-1]["final_text"] = json.dumps(changed)
            path.write_text("\n".join(json.dumps(p) for p in phases), encoding="utf-8")
            self.assertFalse(self.grade(trial)["checks"]["final_state_fields"])

    def test_coverage_requires_update_and_anchor_folded_before_first_delivery(self):
        events = [{"type": "context.compacted", "seq": 1, "payload": {"status": "written", "previous_cursor": 0}},
                  {"type": "context.compacted", "seq": 2, "payload": {"status": "written", "previous_cursor": 8}},
                  {"type": "context.request_projected", "phase": "deliver", "seq": 3,
                   "payload": {"summary_revision": 2, "compacted_message_count": 16}}]
        self.assertTrue(compression_coverage(events, [4], "D", True)["sufficient"])
        self.assertFalse(compression_coverage(events, [18], "D", True)["sufficient"])
        self.assertFalse(compression_coverage(events, [4], "D", False)["sufficient"])
        late = deepcopy(events)
        late[1]["seq"] = 4
        self.assertFalse(compression_coverage(late, [4], "D", True)["sufficient"])
        self.assertFalse(compression_coverage([], [4], "D", True)["sufficient"])
        self.assertTrue(compression_coverage([], [4], "A", True)["sufficient"])


if __name__ == "__main__":
    unittest.main()
