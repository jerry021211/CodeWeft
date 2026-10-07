import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace as Record

from codeagent.web.api import _team_recoveries, _integration_view


class TeamRecoveryDiagnosticsTests(unittest.TestCase):
    def test_legacy_integration_log_is_bounded_and_exposes_cause(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "artifacts").mkdir()
            log = root / "artifacts" / "failure.log"
            log.write_text("x" * 20000 + "\nParserError InvalidEndOfLine", encoding="utf-8")
            record = {"worktree_path": str(root / "integration" / "op"), "result": {},
                      "validations": [{"status": "failed", "output_ref": str(log)}]}
            view = _integration_view(record)
            self.assertEqual(view["result"]["diagnostic"]["category"], "environment")
            self.assertLessEqual(len(view["validations"][0]["output_excerpt"]), 6000)
            self.assertNotIn("output_excerpt", record["validations"][0])
            self.assertEqual(record["result"], {})

    def test_integration_snapshot_does_not_read_files_outside_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "private.txt"
            log.write_text("PRIVATE_CONTENT", encoding="utf-8")
            view = _integration_view({"worktree_path": str(root / "integration" / "op"), "result": {},
                "validations": [{"status": "failed", "output_ref": str(log)}]})
            self.assertNotIn("PRIVATE_CONTENT", str(view))
            self.assertIn("无法读取", view["validations"][0]["output_excerpt"])

    def snapshot(self, *, reason="protocol_incomplete", error="scope rejected", tool_session="current"):
        attempt = Record(id="attempt", session_id="current", task_id="1", agent_id="agent",
                         result_unknown=False, error={"type": reason}, state=Record(value="waiting"))
        tool = Record(session_id=tool_session, result_unknown=False, status="failed",
                      tool_name="team_submit_candidate", error=error, tool_call_id="call")
        return _team_recoveries(
            Record(list_tool_executions=lambda _: [tool]), attempts=[attempt],
            sessions=[Record(id="current", waiting_reason=reason)], worktrees=[], messages=[],
        )[0]

    def test_restart_does_not_label_old_submission_failure_as_uncertain_write(self):
        result = self.snapshot(reason="service_restart")
        self.assertIsNone(result["tool_name"])
        self.assertIsNone(result["tool_error"])
        self.assertFalse(result["result_unknown"])

    def test_previous_session_failure_is_not_current_failure(self):
        result = self.snapshot(tool_session="old")
        self.assertIsNone(result["tool_name"])
        self.assertIn("尚未成功提交", result["summary"])

    def test_legacy_generic_submission_failure_does_not_invent_paths(self):
        error = "StorageConflictError: Candidate contains files outside the approved write scope"
        result = self.snapshot(error=error)
        self.assertIn("代码成果提交失败", result["summary"])
        self.assertEqual(result["tool_error"], error)
        self.assertEqual(result["outside_paths"], [])
        self.assertFalse(result["result_unknown"])
