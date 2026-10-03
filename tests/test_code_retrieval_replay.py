"""Replay measurement semantics, independent of the historical answer key."""
import hashlib
from pathlib import Path
import tempfile
import unittest

from evals.code_retrieval.replay import score, write


class ReplayScoringTests(unittest.TestCase):
    def test_function_identity_is_separate_from_required_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "target").mkdir()
            raw = b'def example():\n    return 42\n'
            (root / "target/a.py").write_bytes(raw)
            doc = {"_id": "a.py::example@1", "path": "a.py", "symbol": "example",
                   "start_line": 1, "end_line": 2, "text": raw.decode()}
            import json
            (root / "corpus.jsonl").write_text(json.dumps(doc) + "\n", encoding="utf-8")
            write(root / "gold.json", {"cases": [
                {"id": "P", "category": "exact", "groups": [{"id": "g1", "members": [
                    {"doc_id": doc["_id"], "evidence_lines": [2]}]}]},
                {"id": "Z", "category": "absent", "groups": []}]})
            snippet = {"path": "a.py", "symbol": "example", "definition_start_line": 1,
                       "line": 1, "end_line": 1, "quote": "def example():",
                       "content_hash": hashlib.sha256(raw).hexdigest()}
            jobs = [{"id": name, "case_id": case, "stratum": "audit"}
                    for name, case in [("identity", "P"), ("evidence", "P"), ("invalid", "P"), ("absent", "Z")]]
            rows = [{"id": "identity", "payload": {"results": [snippet]}},
                    {"id": "evidence", "payload": {"results": [dict(snippet, quote=raw.decode().rstrip(), end_line=2)]}},
                    {"id": "invalid", "error": "invalid arguments"},
                    {"id": "absent", "payload": {"results": [snippet]}}]
            report = score(rows, jobs, root)
            first, second, failed, absent = report["rows"]
            self.assertTrue(first["identity_hit1"])
            self.assertEqual(first["hit1"], 0)
            self.assertEqual(first["source_valid"], 1)
            self.assertEqual(second["hit1"], 1)
            self.assertFalse(failed["identity_hit1"])
            self.assertNotIn("false_positive", absent)  # Retrieval candidates are not an Agent's assertion.
            summary = report["summaries"]["audit"]
            self.assertEqual(summary["positive_calls"], 3)  # Failed call remains in the denominator.
            self.assertEqual(summary["identity_hit1"], 2)
            self.assertEqual(summary["hit1"], 1)
            self.assertEqual(summary["errors"], 1)


if __name__ == "__main__":
    unittest.main()
