from __future__ import annotations

import subprocess
import tempfile
import unittest
import threading
import time
import os
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from codeagent.teams.candidates import CandidateService
from codeagent.teams.managed_integration import IntegrationService
from codeagent.web.storage import SQLiteRepository, StorageConflictError
from codeagent.worktrees import WorktreeManager
from codeagent.worktrees.snapshots import LocalGit, exclusive_file


class ManagedIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.git("init")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        self.git("config", "core.autocrlf", "false")
        self.base_text = "first\n" + "".join(f"line-{n}\n" for n in range(2, 12)) + "last\n"
        (self.source / "base.txt").write_bytes(self.base_text.encode("utf-8"))
        (self.source / ".gitignore").write_text("ignored/\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-m", "base")
        self.head = self.git("rev-parse", "HEAD")
        self.repo = SQLiteRepository(self.root / "state.db", recover_incomplete=False)
        self.conversation = self.repo.create_conversation(workspace=str(self.source))
        self.run = self.repo.create_run(self.conversation.id, status="running")
        self.list_id = self.conversation.active_task_list_id
        self.manager = WorktreeManager(self.repo, self.source, self.root / "managed")
        self.localgit = LocalGit(self.source, self.root / "managed")
        self.service = IntegrationService(self.repo, self.manager)

    def tearDown(self):
        self.repo.close()
        self.temp.cleanup()

    def git(self, *args, cwd=None):
        result = subprocess.run(["git", "-C", str(cwd or self.source), *args], capture_output=True, check=True)
        return result.stdout.decode("utf-8", "replace").strip()

    def start(self, names=("A",), dependencies=None, commands=None):
        self.tasks = {}
        for name in names:
            self.tasks[name] = self.repo.create_task(self.list_id, subject=name, description="Implement " + name,
                blocked_by=[self.tasks[n].task.id for n in (dependencies or {}).get(name, [])],
                metadata={"kind": "code", "write_scopes": [], "risk_level": "low"})
        snapshot = self.manager.snapshot_local()
        self.team = self.repo.create_team_run(conversation_id=self.conversation.id, root_run_id=self.run.id,
            task_list_id=self.list_id, base_commit=snapshot["commit"], integration_mode="managed", metadata=snapshot)
        plan = self.repo.create_team_plan_revision(self.team.id, plan={"tasks": [
            {"task_id": r.task.id, **r.task.metadata} for r in self.tasks.values()],
            "integration_validation_commands": commands or []}, created_by=self.team.lead_agent_id, command_id="plan")
        self.repo.submit_team_plan_revision(self.team.id, plan.revision, command_id="submit")
        self.manager.ensure_baseline_ready(self.team.id)
        self.repo.decide_team_plan_revision(self.team.id, plan.revision, decision="approve", decided_by="user", reason="test", command_id="approve")

    def claim(self, name):
        member = self.repo.create_team_agent(self.team.id, name=name + uuid4().hex)
        session = self.repo.create_agent_session(self.team.id, member.id)
        task = self.repo.get_task_resource(self.list_id, self.tasks[name].task.id)
        attempt = self.repo.claim_task_attempt(self.team.id, task_id=task.task.id, agent_id=member.id,
            session_id=session.id, expected_task_revision=task.revision, command_id="claim" + uuid4().hex)
        return attempt, self.manager.create_for_attempt(attempt)

    def candidate(self, name, changes):
        attempt, binding = self.claim(name)
        for filename, content in changes.items():
            path = Path(binding.path) / filename
            if content is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        service = CandidateService(self.repo, self.manager)
        candidate = service.submit(attempt.id, summary="Implement " + name)
        service.review(candidate.id, decision="accept", reviewed_by=self.team.lead_agent_id, reason="reviewed", command_id="review" + candidate.id)
        return service.validate_and_commit(candidate.id)

    def integrate(self):
        return self.service.process_team(self.team.id)

    def test_snapshot_contains_dirty_staged_untracked_and_deleted_files_without_changing_index(self):
        (self.source / "base.txt").write_text("staged\n", encoding="utf-8")
        self.git("add", "base.txt")
        index = self.localgit.index_hash()
        (self.source / "base.txt").write_text("unstaged\n", encoding="utf-8")
        (self.source / "new.txt").write_text("new\n", encoding="utf-8")
        (self.source / ".gitignore").unlink()
        self.start()
        attempt, binding = self.claim("A")
        self.assertEqual((Path(binding.path) / "base.txt").read_text(), "unstaged\n")
        self.assertTrue((Path(binding.path) / "new.txt").exists())
        self.assertFalse((Path(binding.path) / ".gitignore").exists())
        self.assertEqual(self.localgit.index_hash(), index)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.head)
        self.assertEqual(attempt.attempt_base_commit, self.team.base_commit)

    def test_code_dependency_waits_for_integration_and_reads_upstream(self):
        self.start(("A", "B"), {"B": ["A"]})
        candidate = self.candidate("A", {"upstream.txt": "A\n"})
        with self.assertRaisesRegex(StorageConflictError, "candidate_not_integrated"):
            self.claim("B")
        op = self.integrate()
        self.assertEqual(op["status"], "published", op)
        attempt, binding = self.claim("B")
        self.assertEqual(attempt.attempt_base_commit, op["trial_commit"])
        self.assertEqual((Path(binding.path) / "upstream.txt").read_text(), "A\n")
        self.assertIsNone(self.repo.get_candidate(candidate.id).integrated_at)
        self.assertEqual(self.repo.get_candidate(candidate.id).team_integrated_revision, 1)
        self.assertFalse((self.source / "upstream.txt").exists())

    def test_snapshot_includes_explicitly_staged_ignored_file(self):
        (self.source / "ignored").mkdir()
        (self.source / "ignored" / "tracked.txt").write_text("staged intentionally")
        (self.source / "ignored" / "cache.txt").write_text("ignored cache")
        self.git("add", "-f", "ignored/tracked.txt")
        index = self.localgit.index_hash()
        self.start()
        _, binding = self.claim("A")
        self.assertEqual((Path(binding.path) / "ignored" / "tracked.txt").read_text(), "staged intentionally")
        self.assertFalse((Path(binding.path) / "ignored" / "cache.txt").exists())
        self.assertEqual(self.localgit.index_hash(), index)

    def test_parallel_old_base_candidates_publish_once_without_losing_changes(self):
        self.start(("A", "D", "B"), {"B": ["A", "D"]})
        a = self.candidate("A", {"a.txt": "A\n"})
        d = self.candidate("D", {"d.txt": "D\n"})
        self.assertEqual(a.base_commit, d.base_commit)
        self.assertEqual(self.integrate()["status"], "published")
        self.assertEqual(self.integrate()["status"], "published")
        self.assertIsNone(self.integrate())
        _, binding = self.claim("B")
        self.assertTrue((Path(binding.path) / "a.txt").exists())
        self.assertTrue((Path(binding.path) / "d.txt").exists())
        self.assertEqual(self.repo.get_team_run(self.team.id).integration_revision, 2)

    def test_conflict_preserves_head_and_lead_can_request_replacement(self):
        self.start(("A", "D"))
        self.candidate("A", {"base.txt": "A\n"})
        old = self.candidate("D", {"base.txt": "D\n"})
        first = self.integrate()
        conflict = self.integrate()
        self.assertEqual(conflict["status"], "conflicted", conflict)
        self.assertEqual(self.repo.get_team_run(self.team.id).integration_head, first["trial_commit"])
        self.repo.retry_team_integration(conflict["id"], actor=self.team.lead_agent_id, reason="reconcile both changes", repair=True)
        repaired = self.candidate("D", {"base.txt": "A and D\n"})
        self.assertEqual(repaired.base_commit, first["trial_commit"])
        self.assertTrue(self.repo.get_candidate(old.id).superseded_at)
        self.assertEqual(self.integrate()["status"], "published")

    def test_combined_validation_failure_does_not_publish(self):
        self.start(commands=['python -c "raise SystemExit(1)"'])
        self.candidate("A", {"a.txt": "A\n"})
        failed = self.integrate()
        self.assertEqual(failed["status"], "validation_failed", failed)
        self.assertEqual(self.repo.get_team_run(self.team.id).integration_head, self.team.base_commit)
        self.assertFalse((self.source / "a.txt").exists())
        self.assertIsNone(self.integrate())

    def test_validation_mutation_cannot_be_published(self):
        self.start(commands=['python -c "from pathlib import Path; Path(\'a.txt\').write_text(\'changed\')"'])
        self.candidate("A", {"a.txt": "A\n"})
        failed = self.integrate()
        self.assertEqual(failed["status"], "validation_failed", failed)
        self.assertEqual(self.repo.get_team_run(self.team.id).integration_revision, 0)

    def test_local_delivery_preserves_user_edits_head_and_staging(self):
        (self.source / "user.txt").write_text("staged user\n", encoding="utf-8")
        self.git("add", "user.txt")
        (self.source / "user.txt").write_text("unstaged user\n", encoding="utf-8")
        index = self.localgit.index_hash()
        self.start()
        self.candidate("A", {"base.txt": self.base_text.replace("first", "team first"), "binary.dat": b"\0\1\2"})
        self.assertEqual(self.integrate()["status"], "published")
        (self.source / "base.txt").write_bytes(self.base_text.replace("last", "user last").encode("utf-8"))
        delivery = self.integrate()
        self.assertEqual(delivery["status"], "delivered", delivery)
        self.assertEqual((self.source / "base.txt").read_text(), self.base_text.replace("first", "team first").replace("last", "user last"))
        self.assertEqual((self.source / "user.txt").read_text(), "unstaged user\n")
        self.assertEqual((self.source / "binary.dat").read_bytes(), b"\0\1\2")
        self.assertEqual(self.localgit.index_hash(), index)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.head)
        self.assertEqual(self.repo.get_team_run(self.team.id).state.value, "completed")
        self.assertTrue(any(m.payload.get("status") == "delivered" for m in self.repo.list_team_messages(self.team.id)))
        self.assertIsNone(self.integrate())

    def test_delivery_conflict_leaves_source_untouched(self):
        self.start()
        self.candidate("A", {"base.txt": "team\n"})
        self.integrate()
        (self.source / "base.txt").write_text("user\n", encoding="utf-8")
        index = self.localgit.index_hash()
        result = self.integrate()
        self.assertEqual(result["status"], "conflicted", result)
        self.assertEqual((self.source / "base.txt").read_text(), "user\n")
        self.assertEqual(self.localgit.index_hash(), index)
        self.assertEqual(self.repo.get_team_run(self.team.id).state.value, "running")

    def test_git_publication_before_database_crash_is_reconciled(self):
        self.start()
        self.candidate("A", {"a.txt": "A\n"})
        with patch.object(self.repo, "publish_team_integration", side_effect=RuntimeError("database unavailable")):
            pending = self.integrate()
        self.assertEqual(pending["status"], "publishing")
        self.assertEqual(self.repo.get_team_run(self.team.id).integration_revision, 0)
        self.repo.close()
        self.repo = SQLiteRepository(self.root / "state.db")
        self.manager = WorktreeManager(self.repo, self.source, self.root / "managed")
        self.service = IntegrationService(self.repo, self.manager)
        published = self.integrate()
        self.assertEqual(published["status"], "published", published)
        self.assertEqual(published["trial_commit"], pending["trial_commit"])
        self.assertEqual(self.repo.get_team_run(self.team.id).integration_revision, 1)

    def test_old_attempt_can_resume_after_head_advances(self):
        self.start(("A", "B"))
        self.candidate("A", {"a.txt": "A\n"})
        attempt, _ = self.claim("B")
        self.repo.mark_attempt_suspect(attempt.id, reason="model_call_timeout")
        self.repo.pause_attempt_after_worker_exit(attempt.id, reason="model_call_timeout")
        self.integrate()
        from codeagent.runtime import TeamSupervisor
        supervisor = TeamSupervisor(self.repo, lambda *_: None, enabled=True, worktree_manager=self.manager)
        try:
            resumed = supervisor.resume_attempt(attempt.id, resumed_by="user", reason="checked", command_id="resume")
            self.assertEqual(resumed.attempt_base_commit, self.team.base_commit)
        finally:
            supervisor.stop()

    def test_read_view_uses_published_files_and_external_writes_are_detected(self):
        self.start()
        self.candidate("A", {"a.txt": "A\n"})
        result = self.integrate()
        view = Path(self.manager.read_view(result["trial_commit"]))
        self.assertEqual((view / "a.txt").read_text(), "A\n")
        (view / "a.txt").write_text("tampered")
        with self.assertRaisesRegex(Exception, "snapshot was modified"):
            self.manager.read_view(result["trial_commit"])

    def test_process_lock_does_not_allow_a_second_writer(self):
        self.start()
        self.candidate("A", {"a.txt": "A\n"})
        with exclusive_file(self.manager.managed_root / self.team.id / "integration.lock"):
            with self.assertRaisesRegex(Exception, "Another runtime"):
                self.integrate()
        self.assertEqual(self.integrate()["status"], "published")

    def test_partial_delivery_resumes_without_duplicate_writes(self):
        self.start()
        self.candidate("A", {"a.txt": "A\n", "b.txt": "B\n", "base.txt": None})
        self.integrate()
        replace = os.replace
        def interrupted(source, target):
            if Path(target).name == "b.txt":
                raise OSError("simulated process interruption")
            return replace(source, target)
        with patch("codeagent.teams.managed_integration.os.replace", side_effect=interrupted):
            result = self.integrate()
        self.assertEqual(result["status"], "recovery_required", result)
        self.assertFalse((self.source / "base.txt").exists())
        self.assertTrue((self.source / "a.txt").exists())
        self.assertFalse((self.source / "b.txt").exists())
        self.assertIsNone(self.integrate())
        resumed = self.service.resume_delivery(self.team.id, result["id"])
        self.assertEqual(resumed["status"], "delivered", resumed)
        self.assertEqual((self.source / "b.txt").read_text(), "B\n")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.head)

    def test_delivery_recovery_refuses_new_user_edits(self):
        self.start()
        self.candidate("A", {"a.txt": "A\n"})
        self.integrate()
        with patch.object(self.service, "_apply_files", side_effect=OSError("interrupted")):
            result = self.integrate()
        (self.source / "a.txt").write_text("user edit")
        resumed = self.service.resume_delivery(self.team.id, result["id"])
        self.assertEqual(resumed["status"], "recovery_required", resumed)
        self.assertEqual((self.source / "a.txt").read_text(), "user edit")

    def test_source_change_during_delivery_validation_is_not_overwritten(self):
        self.start()
        self.candidate("A", {"a.txt": "A\n"})
        self.integrate()
        validate = self.service._validate
        def changed(*args):
            result = validate(*args)
            (self.source / "user.txt").write_text("concurrent user edit")
            return result
        with patch.object(self.service, "_validate", side_effect=changed):
            result = self.integrate()
        self.assertEqual(result["status"], "interrupted", result)
        self.assertFalse((self.source / "a.txt").exists())
        self.assertEqual((self.source / "user.txt").read_text(), "concurrent user edit")

    def test_stopping_during_validation_cannot_publish(self):
        self.start()
        self.candidate("A", {"a.txt": "A\n"})
        stopping = threading.Event()
        self.service.stop_event = stopping
        validate = CandidateService._run_validation
        def stopped(runner, *args):
            result = validate(runner, *args)
            stopping.set()
            return result
        with patch.object(CandidateService, "_run_validation", new=stopped):
            result = self.integrate()
        self.assertEqual(result["status"], "interrupted", result)
        self.assertEqual(self.repo.get_team_run(self.team.id).integration_revision, 0)

    def test_supervisor_automatically_integrates_and_delivers_with_one_slot(self):
        from codeagent.runtime import TeamSupervisor
        self.start()
        self.candidate("A", {"a.txt": "A\n"})
        supervisor = TeamSupervisor(self.repo, lambda *_: self.fail("No model call needed"),
            enabled=True, write_enabled=True, max_workers=1, worktree_manager=self.manager)
        try:
            deadline = time.monotonic() + 40
            while self.repo.get_team_run(self.team.id).state.value != "completed" and time.monotonic() < deadline:
                supervisor.dispatch_once(self.team.id)
                time.sleep(0.05)
            self.assertEqual(self.repo.get_team_run(self.team.id).state.value, "completed")
            self.assertEqual([o["status"] for o in self.repo.list_team_integrations(self.team.id)], ["published", "delivered"])
            self.assertEqual((self.source / "a.txt").read_text(), "A\n")
        finally:
            supervisor.stop()

    def test_attempt_from_published_version_resumes_after_later_publication(self):
        from codeagent.runtime import TeamSupervisor
        self.start(("A", "B", "D"), {"B": ["A"]})
        self.candidate("A", {"a.txt": "A\n"})
        first = self.integrate()
        self.candidate("D", {"d.txt": "D\n"})
        attempt, _ = self.claim("B")
        self.assertEqual(attempt.base_integration_revision, 1)
        self.repo.mark_attempt_suspect(attempt.id, reason="model_call_timeout")
        self.repo.pause_attempt_after_worker_exit(attempt.id, reason="model_call_timeout")
        self.integrate()
        supervisor = TeamSupervisor(self.repo, lambda *_: None, enabled=True, worktree_manager=self.manager)
        try:
            resumed = supervisor.resume_attempt(attempt.id, resumed_by="user", reason="checked", command_id="resume")
            self.assertEqual(resumed.attempt_base_commit, first["trial_commit"])
        finally:
            supervisor.stop()

    def test_validation_timeout_retains_output_and_requires_explicit_recovery(self):
        self.start(commands=['python -c "import time; print(123, flush=True); time.sleep(2)"'])
        self.candidate("A", {"a.txt": "A\n"})
        self.service.validation_timeout = 0.5
        result = self.integrate()
        self.assertEqual(result["status"], "recovery_required", result)
        output = Path(result["validations"][-1]["output_ref"]).read_text()
        self.assertIn("123", output)
        self.assertIn("Timed out", output)
        if "cleanup confirmed=False" in output:
            # Restricted Windows runners may deny taskkill; the operation must
            # remain frozen. Let this bounded test child exit before temp cleanup.
            time.sleep(2.1)
        self.assertIsNone(self.integrate())
        resumed = self.service.resume_delivery(self.team.id, result["id"])
        self.assertEqual(resumed["status"], "interrupted")


if __name__ == "__main__":
    unittest.main()
