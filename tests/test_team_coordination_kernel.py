import json
import unittest
import sys
import threading
import time
from pathlib import Path

from tests import test_team_worktrees as fixtures
from codeagent.legacy.teams import CandidateService, TeamToolExecutionGate
from codeagent.tools import ToolRegistry, WorkspaceGuard, WriteFileTool
from codeagent.web.storage import SQLiteRepository
from codeagent.legacy.teams.tools import TeamResumeWorkTool
from codeagent.worktrees.snapshots import LocalGit


class CoordinationKernelTests(unittest.TestCase):
    # Reuse the real Git/SQLite fixture, not its legacy behavior tests.
    setUp = fixtures.TeamWorktreeTests.setUp
    tearDown = fixtures.TeamWorktreeTests.tearDown
    _git = fixtures.TeamWorktreeTests._git
    _attempt = fixtures.TeamWorktreeTests._attempt
    _confirm = fixtures.TeamWorktreeTests._confirm

    def v2(self, **policy):
        with self.repository._transaction(immediate=True) as db:
            db.execute("UPDATE team_runs SET metadata_json=? WHERE id=?",
                       (json.dumps({"kernel_version": 2, **policy}), self.team.id))
        self._confirm()

    def gate(self, attempt):
        binding = self.manager.create_for_attempt(attempt)
        registry = ToolRegistry()
        registry.register(WriteFileTool(workspace_guard=WorkspaceGuard(Path(binding.path))))
        return binding, TeamToolExecutionGate(self.repository, self.manager, attempt.id).wrap(registry)

    def test_responsibility_change_is_reviewed_and_committed_without_extra_user_gate(self):
        self.v2()
        attempt = self._attempt("soft", metadata={"artifact_paths": ["test-results/**"]})
        binding, gate = self.gate(attempt)
        self.assertIn("Wrote", gate.execute("write_file", {"file_path": "outside.txt", "content": "needed change\n"}))
        output = Path(binding.path) / "test-results"
        output.mkdir()
        (output / "run.json").write_text("first", encoding="utf-8")
        service = CandidateService(self.repository, self.manager)
        candidate = service.submit(attempt.id, summary="changed a shared source file")
        self.assertNotIn("test-results/run.json", candidate.untracked_files)
        self.assertTrue(any("outside.txt" in risk for risk in candidate.known_risks))
        (output / "run.json").write_text("second", encoding="utf-8")
        service.review(candidate.id, decision="accept", reviewed_by="agent_lead", reason="shared file reviewed", command_id="review-soft")
        committed = service.validate_and_commit(candidate.id)
        self.assertIsNotNone(committed.commit_hash)
        self.assertFalse(committed.user_approval_required)
        self.assertEqual(self._git("show", f"{committed.commit_hash}:outside.txt"), "needed change")
        self.assertNotIn("test-results", self._git("ls-tree", "-r", "--name-only", committed.commit_hash))
        self.assertEqual(self._git("rev-parse", "HEAD"), self.base_commit)

    def test_explicit_user_boundary_is_not_a_soft_responsibility(self):
        self.v2(authorized_write_scopes=["allowed/**"])
        attempt = self._attempt("hard")
        binding, gate = self.gate(attempt)
        self.assertIn("blocked", gate.execute("write_file", {"file_path": "outside.txt", "content": "forbidden"}))
        self.assertEqual((Path(binding.path) / "outside.txt").read_text(), "outside\n")

    def test_dispatch_error_is_durable_deduplicated_and_closes_on_success(self):
        self.v2()
        self.repository.record_dispatch_issue(self.team.id, "pending", "Git unavailable")
        self.repository.record_dispatch_issue(self.team.id, "pending", "Git unavailable")
        issue = self.repository.list_coordination_issues(self.team.id)[0]
        self.assertEqual((issue["owner"], issue["status"], issue["version"]), ("user", "open", 1))
        self.assertIn("Git unavailable", issue["evidence"]["error"])
        self.repository.record_dispatch_issue(self.team.id, "pending")
        self.assertEqual(self.repository.list_coordination_issues(self.team.id)[0]["status"], "resolved")

    def test_changed_candidate_remains_readable_but_cannot_be_accepted(self):
        from codeagent.legacy.teams.tools import TeamCandidateContextTool
        self.v2()
        attempt = self._attempt("diagnostic")
        binding, gate = self.gate(attempt)
        gate.execute("write_file", {"file_path": "outside.txt", "content": "candidate\n"})
        service = CandidateService(self.repository, self.manager)
        candidate = service.submit(attempt.id, summary="candidate")
        (Path(binding.path) / "outside.txt").write_text("changed by test\n")
        evidence = json.loads(TeamCandidateContextTool(self.repository, self.manager, self.team.id).run(candidate.id))
        self.assertFalse(evidence["snapshot_matches"])
        self.assertIn("candidate", evidence["patch"])
        with self.assertRaisesRegex(Exception, "changed after submission"):
            service.review(candidate.id, decision="accept", reviewed_by="agent_lead", reason="accept", command_id="stale")
        service.review(candidate.id, decision="rework", reviewed_by="agent_lead", reason="Inspect test mutations", command_id="repair")
        self.assertTrue(self.repository.get_task_attempt(attempt.id).write_enabled)

    def test_artifact_classification_does_not_hide_tracked_source(self):
        self.v2()
        attempt = self._attempt("tracked", metadata={"artifact_paths": ["outside.txt"]})
        binding, gate = self.gate(attempt)
        gate.execute("write_file", {"file_path": "outside.txt", "content": "tracked\n"})
        snapshot = self.manager.capture_candidate(binding.id, candidate_id="tracked-snapshot")
        self.assertIn("outside.txt", snapshot.changed_files)
        git = LocalGit(self.source, self.root / "snapshot-git")
        self.assertNotEqual(git.tree(Path(binding.path), artifact_paths=["outside.txt"]), self._git("rev-parse", "HEAD^{tree}"))

    def test_generic_yield_preserves_blocker_and_issue_closes_only_on_domain_recovery(self):
        self.v2()
        attempt = self._attempt("reason")
        self.manager.create_for_attempt(attempt)
        self.repository.pause_attempt_after_worker_exit(attempt.id, reason="protocol_incomplete")
        self.repository.save_agent_session_checkpoint(attempt.session_id, messages=[], context={}, safe_boundary="test", target_state="waiting", waiting_reason="waiting")
        self.assertEqual(self.repository.get_agent_session(attempt.session_id).waiting_reason, "protocol_incomplete")
        issues = self.repository.list_coordination_issues(self.team.id)
        self.assertEqual(issues[0]["owner"], "lead")
        self.assertEqual(issues[0]["status"], "open")
        tool = TeamResumeWorkTool(self.repository, self.manager, self.team.id, "agent_lead")
        tool.run(attempt.id, "检查最近的提交错误，修正后补交成果，不重写已完成代码")
        self.assertEqual(self.repository.list_coordination_issues(self.team.id)[0]["status"], "resolved")
        self.assertTrue(self.repository.get_task_attempt(attempt.id).write_enabled)

    def test_independent_worktrees_do_not_lease_the_same_virtual_path(self):
        self.v2()
        first = self._attempt("first")
        second = self._attempt("second")
        self.assertNotEqual(self.manager.create_for_attempt(first).path, self.manager.create_for_attempt(second).path)

    def test_correction_budget_survives_repeated_stops(self):
        self.v2()
        attempt = self._attempt("bounded")
        self.manager.create_for_attempt(attempt)
        tool = TeamResumeWorkTool(self.repository, self.manager, self.team.id, "agent_lead")
        for _ in range(2):
            self.repository.pause_attempt_after_worker_exit(attempt.id, reason="protocol_incomplete")
            tool.run(attempt.id, "核对失败日志并补交")
        self.repository.pause_attempt_after_worker_exit(attempt.id, reason="protocol_incomplete")
        with self.assertRaisesRegex(Exception, "budget exhausted"):
            tool.run(attempt.id, "再次尝试")
        self.assertEqual(self.repository.list_coordination_issues(self.team.id)[0]["owner"], "user")

    def test_v2_restart_never_automatically_resumes_writes(self):
        self.v2()
        attempt = self._attempt("restart")
        self.manager.create_for_attempt(attempt)
        self.repository.close()
        self.repository = SQLiteRepository(self.root / "state.db")
        current = self.repository.get_task_attempt(attempt.id)
        self.assertFalse(current.write_enabled)
        self.assertFalse(current.error["auto_recoverable"])
        self.assertEqual(self.repository.get_team_run(self.team.id).state.value, "paused")

    def test_plan_revision_preserves_an_unaffected_attempt_and_allows_recovery(self):
        self.v2()
        attempt = self._attempt("old-contract")
        self.manager.create_for_attempt(attempt)
        task = self.repository.get_task_resource(attempt.task_list_id, attempt.task_id)
        revision = self.repository.create_team_plan_revision(self.team.id,
            plan={"tasks": [{"task_id": attempt.task_id, **task.task.metadata}], "shared_context": "补充协调说明"},
            created_by="agent_lead", command_id="revision2")
        self.repository.submit_team_plan_revision(self.team.id, revision.revision, command_id="submit2")
        self.assertEqual(self.repository.get_team_run(self.team.id).active_plan_revision, 1)
        self.repository.decide_team_plan_revision(self.team.id, revision.revision, decision="approve",
            decided_by="user", reason="补充说明", command_id="approve2")
        self.repository.pause_attempt_after_worker_exit(attempt.id, reason="protocol_incomplete")
        TeamResumeWorkTool(self.repository, self.manager, self.team.id, "agent_lead").run(attempt.id, "按已固定约定补交")
        self.assertEqual(self.repository.get_task_attempt(attempt.id).team_plan_revision, 1)
        self.assertTrue(self.repository.get_task_attempt(attempt.id).write_enabled)

    def test_revision_cannot_discard_an_existing_attempt(self):
        self.v2()
        self._attempt("keep")
        revision = self.repository.create_team_plan_revision(self.team.id, plan={"tasks": []},
            created_by="agent_lead", command_id="remove")
        self.repository.submit_team_plan_revision(self.team.id, revision.revision, command_id="submit-remove")
        with self.assertRaisesRegex(Exception, "修正任务"):
            self.repository.decide_team_plan_revision(self.team.id, revision.revision, decision="approve",
                decided_by="user", reason="remove", command_id="approve-remove")
        self.assertEqual(self.repository.get_team_run(self.team.id).active_plan_revision, 1)

    def test_explicit_revision_continuation_resolves_plan_wait_without_expanding_contract(self):
        self.v2()
        attempt = self._attempt("plan-wait")
        self.manager.create_for_attempt(attempt)
        self.repository.pause_attempt_after_worker_exit(attempt.id, reason="team_plan_change_required")
        task = self.repository.get_task_resource(attempt.task_list_id, attempt.task_id)
        revision = self.repository.create_team_plan_revision(self.team.id,
            plan={"tasks": [{"task_id": attempt.task_id, **task.task.metadata}],
                  "continue_attempts": [attempt.id], "shared_context": "已确认原任务范围足以继续"},
            created_by="agent_lead", command_id="continue-plan")
        self.repository.submit_team_plan_revision(self.team.id, revision.revision, command_id="submit-continue")
        self.repository.decide_team_plan_revision(self.team.id, revision.revision, decision="approve",
            decided_by="user", reason="继续原约定", command_id="approve-continue")
        self.assertFalse(self.repository.get_task_attempt(attempt.id).write_enabled)
        self.assertEqual(self.repository.get_attempt_contract(attempt.id), task.task.metadata)
        TeamResumeWorkTool(self.repository, self.manager, self.team.id, "agent_lead").run(attempt.id, "按确认的原约定继续")
        self.assertTrue(self.repository.get_task_attempt(attempt.id).write_enabled)

    def test_claimed_contract_cannot_be_mutated_through_task_update(self):
        self.v2()
        attempt = self._attempt("immutable")
        with self.assertRaisesRegex(Exception, "执行约定已固定"):
            self.repository.update_task(attempt.task_list_id, attempt.task_id,
                changes={"metadata": {"strict_write_scopes": ["**"]}}, human_override=True)

    def test_pause_stops_a_running_validation_process(self):
        self.v2()
        runner = CandidateService(self.repository, self.manager, validation_timeout=30)
        runner.validation_team_id = self.team.id
        sentinel = self.root / "validator-started"
        results = []
        worker = threading.Thread(target=lambda: results.append(runner._run_validation(self.root,
            {"argv": [sys.executable, "-c", "from pathlib import Path; import time; Path('validator-started').write_text('started'); time.sleep(30)"]})))
        worker.start()
        until = time.monotonic() + 5
        while not sentinel.exists() and time.monotonic() < until:
            time.sleep(0.02)
        self.assertTrue(sentinel.exists(), "Validation process did not start")
        self.repository.request_team_pause(self.team.id, reason="test", command_id="pause-validator")
        worker.join(8)
        self.assertFalse(worker.is_alive(), "Pause must not wait for the original validation timeout")
        self.assertNotEqual(results[0][0], "passed")
        self.assertIn("cleanup confirmed=", results[0][2])

    def validation_approval(self, *, pause):
        from codeagent.web.scheduler import RunScheduler
        self.v2()
        scheduler = RunScheduler(self.repository, object(), approval_timeout=5)
        runner = CandidateService(self.repository, self.manager, permission_check=scheduler.authorize_team_validation)
        runner.validation_team_id = self.team.id
        results = []
        # The ordinary policy recognizes this phrase. If approved, this command
        # only prints text; no destructive command is ever executed by the test.
        command = {"argv": [sys.executable, "-c", "print('rm marker')"]}
        worker = threading.Thread(target=lambda: results.append(runner._run_validation(self.root, command)))
        worker.start()
        approval = None
        try:
            until = time.monotonic() + 3
            while time.monotonic() < until and approval is None:
                pending = scheduler.list_pending_approvals(self.team.conversation_id)
                approval = pending[0] if pending else None
                time.sleep(0.01)
            self.assertIsNotNone(approval, "Runtime validation approval must use the normal conversation channel")
            self.assertEqual(approval.metadata["team_run_id"], self.team.id)
            if pause:
                self.repository.request_team_pause(self.team.id, reason="pause approval", command_id="pause-approval")
            else:
                scheduler.resolve_approval(self.team.root_run_id, approval.id, "allow")
            worker.join(5)
            self.assertFalse(worker.is_alive())
            if pause:
                self.assertTrue(runner.validation_interrupted)
                self.assertIn("command not executed", results[0][2])
                self.assertFalse(scheduler.list_pending_approvals(self.team.conversation_id))
            else:
                self.assertEqual(results[0][0], "passed", results[0])
                self.assertIn("rm marker", results[0][2])
        finally:
            if worker.is_alive():
                self.repository.request_team_pause(self.team.id, reason="test cleanup", command_id="approval-cleanup")
                worker.join(6)

    def test_validation_uses_the_same_live_approval_channel(self):
        self.validation_approval(pause=False)

    def test_pause_abandons_validation_approval_without_executing_command(self):
        self.validation_approval(pause=True)


if __name__ == "__main__":
    unittest.main()
