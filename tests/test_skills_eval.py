from __future__ import annotations

import tempfile
import unittest
import json
from types import SimpleNamespace
from pathlib import Path

from codeagent.runtime.execution import ExecutionStopped
from codeagent import ModelResponse
from codeagent.events import TokenUsage
from evals.skills import BoundedClient, ScriptedClient, grade, run_checks, usage_from_evidence
from evals.skills_cases import CORRECT, cases


def event(tool, args=None, status="success", exit_code=None):
    return {"tool": tool, "args": args or {}, "status": status, "exit_code": exit_code}


class SkillEvaluationTests(unittest.TestCase):
    def prepare(self, case, root):
        for name, content in case.files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def test_correct_answer_cannot_substitute_for_loading_or_reading(self):
        case = next(c for c in cases() if c.id == "repository")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.prepare(case, root)
            score = grade(case, [], "main.py double unittest", root, "end_turn")
            self.assertTrue(score["checks"]["answer_facts"])
            self.assertFalse(score["checks"]["routing"])
            self.assertFalse(score["checks"]["required_reads"])
            failed_load = [event("load_skill", {"name": case.skill}, status="error")]
            self.assertFalse(grade(case, failed_load, "main.py double unittest", root, "end_turn")["passed"])

    def test_negative_case_rejects_unnecessary_skill_and_file_changes(self):
        case = next(c for c in cases() if c.id == "arithmetic")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertTrue(grade(case, [], "42", root, "end_turn")["passed"])
            trace = [event("load_skill", {"name": "bug-fix"})]
            self.assertFalse(grade(case, trace, "42", root, "end_turn")["passed"])
            (root / "unrequested.txt").write_text("extra")
            self.assertFalse(grade(case, [], "42", root, "end_turn")["checks"]["scope_preserved"])

    def test_batch_read_counts_only_successful_files_and_normalizes_absolute_paths(self):
        case = next(c for c in cases() if c.id == "repository")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.prepare(case, root)
            batch = event("read_file", {"file_paths": list(case.reads)})
            batch["read_entries"] = [{"path": str(root / path), "status": "success"} for path in case.reads]
            trace = [event("load_skill", {"name": case.skill}), batch]
            self.assertTrue(grade(case, trace, "main.py double unittest", root, "end_turn")["passed"])
            batch["read_entries"][1]["status"] = "error"
            self.assertFalse(grade(case, trace, "main.py double unittest", root, "end_turn")["checks"]["required_reads"])

    def test_repair_requires_before_after_evidence_and_independent_checks(self):
        case = next(c for c in cases() if c.id == "repair")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.prepare(case, root)
            self.assertNotEqual(run_checks(root).exit_code, 0)
            trace = [event("load_skill", {"name": case.skill})]
            trace += [event("read_file", {"file_path": path}) for path in case.reads]
            trace += [event("run_fixture_tests", status="error", exit_code=1),
                      event("write_file", {"file_path": "totals.py"}), event("run_fixture_tests", exit_code=0)]
            # A claimed passing tool record must not make a broken artifact pass.
            self.assertFalse(grade(case, trace, "fixed", root, "end_turn")["checks"]["independent_behavior_check"])
            (root / "totals.py").write_text(CORRECT, encoding="utf-8")
            self.assertTrue(grade(case, trace, "fixed", root, "end_turn")["passed"])
            no_baseline = [row for row in trace if row["exit_code"] != 1]
            self.assertFalse(grade(case, no_baseline, "fixed", root, "end_turn")["passed"])
            late_load = trace[1:] + trace[:1]
            self.assertFalse(grade(case, late_load, "fixed", root, "end_turn")["checks"]["loaded_before_write"])

    def test_missing_file_requires_observed_failure_and_honest_answer(self):
        case = next(c for c in cases() if c.id == "missing")
        trace = [event("load_skill", {"name": case.skill}),
                 event("read_file", {"file_path": case.missing}, status="error")]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertTrue(grade(case, trace, "文件不存在", root, "end_turn")["passed"])
            trace[1]["args"]["file_path"] = str(root / case.missing)
            self.assertTrue(grade(case, trace, "文件不存在", root, "end_turn")["passed"])
            self.assertFalse(grade(case, trace, "评测通过", root, "end_turn")["passed"])

    def test_usage_and_sdk_reasoning_are_handled_without_false_zero_or_private_trace(self):
        class ThinkingBlock:
            def model_dump(self, **kwargs):
                return {"type": "thinking", "thinking": "private-reasoning-sentinel", "signature": "private-signature"}

        response = ModelResponse("end_turn", [ThinkingBlock(), {"type": "text", "text": "42"}],
                                 usage=TokenUsage(input_tokens=10, output_tokens=5, cache_read_input_tokens=7))
        raw = SimpleNamespace(create_message=lambda **kwargs: response)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client = BoundedClient(raw, root)
            client.create_message(messages=[{"role": "assistant", "content": [ThinkingBlock()]}])
            self.assertEqual(client.usage_tracker.snapshot().total_tokens, 22)
            self.assertEqual(usage_from_evidence(root)["total_tokens"], 22)
            for name in ("requests.jsonl", "responses.jsonl"):
                self.assertNotIn("private-", (root / name).read_text(encoding="utf-8"))
            (root / "responses.jsonl").write_text(json.dumps({"usage": None}) + "\n")
            self.assertFalse(usage_from_evidence(root)["available"])
            self.assertEqual(usage_from_evidence(root)["unavailable_calls"], 1)

    def test_call_cap_blocks_before_ninth_request(self):
        case = next(c for c in cases() if c.id == "arithmetic")
        with tempfile.TemporaryDirectory() as temporary:
            raw = ScriptedClient(case)
            client = BoundedClient(raw, Path(temporary))
            for _ in range(8):
                client.create_message()
            with self.assertRaises(ExecutionStopped):
                client.create_message()
            self.assertEqual(raw.index, 8)


if __name__ == "__main__":
    unittest.main()
