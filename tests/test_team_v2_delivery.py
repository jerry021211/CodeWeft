"""V2 source/artifact manifests through candidate, integration and local delivery."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import test_team_managed_integration as fixtures


class V2DeliveryTests(unittest.TestCase):
    setUp = fixtures.ManagedIntegrationTests.setUp
    tearDown = fixtures.ManagedIntegrationTests.tearDown
    git = fixtures.ManagedIntegrationTests.git
    start = fixtures.ManagedIntegrationTests.start
    claim = fixtures.ManagedIntegrationTests.claim
    candidate = fixtures.ManagedIntegrationTests.candidate
    integrate = fixtures.ManagedIntegrationTests.integrate

    def start_v2(self, *, checks=None, commands=None):
        create_task, create_team = self.repo.create_task, self.repo.create_team_run

        def task(*args, **kwargs):
            kwargs["metadata"] = {**kwargs["metadata"], "artifact_paths": ["test-output/**"]}
            if commands:
                kwargs["metadata"]["validation_commands"] = commands
            return create_task(*args, **kwargs)

        def team(*args, **kwargs):
            kwargs["metadata"] = {**kwargs["metadata"], "kernel_version": 2}
            return create_team(*args, **kwargs)

        with patch.object(self.repo, "create_task", side_effect=task), patch.object(self.repo, "create_team_run", side_effect=team):
            self.start(checks=checks)

    def test_declared_test_output_never_freezes_or_enters_delivery(self):
        self.start_v2(checks=[{"id": "generic-check", "stage": "integration", "requires_tasks": [],
                "steps": [{"argv": [sys.executable, "-c", "from pathlib import Path; p=Path('test-output'); p.mkdir(exist_ok=True); (p/'result.json').write_text('{}')"]}]}])
        index = self.localgit.index_hash()
        candidate = self.candidate("A", {"library.py": "VALUE = 42\n", "test-output/worker.json": "{}"})
        publication = self.integrate()
        self.assertEqual(publication["status"], "published", publication)
        self.assertEqual(self.integrate()["status"], "delivered")
        self.assertEqual((self.source / "library.py").read_text(), "VALUE = 42\n")
        self.assertFalse((self.source / "test-output").exists())
        self.assertNotIn("test-output", self.git("ls-tree", "-r", "--name-only", candidate.commit_hash))
        self.assertEqual(self.git("rev-parse", "HEAD"), self.head)
        self.assertEqual(self.localgit.index_hash(), index)

    def test_validation_failure_returns_to_lead_then_repaired_snapshot_is_delivered(self):
        from codeagent.legacy.teams.candidates import CandidateService
        from codeagent.legacy.teams.tools import TeamCandidateContextTool
        import json
        self.start_v2(commands=["python -c \"from pathlib import Path; assert Path('library.py').read_text().strip() == 'VALUE = 42'\""])
        failed = self.candidate("A", {"library.py": "VALUE = 1\n"})
        self.assertEqual(failed.status.value, "validation_failed")
        self.assertEqual(self.repo.get_team_run(self.team.id).integration_revision, 0)
        self.assertEqual(self.repo.list_coordination_issues(self.team.id)[0]["owner"], "lead")
        context = json.loads(TeamCandidateContextTool(self.repo, self.manager, self.team.id).run(failed.id))
        self.assertTrue(any("AssertionError" in run.get("output_excerpt", "") for run in context["validation_runs"]))
        service = CandidateService(self.repo, self.manager)
        service.review(failed.id, decision="rework", reviewed_by=self.team.lead_agent_id, reason="修正数值并重新验证", command_id="rework-bad")
        binding = self.repo.get_attempt_worktree_binding(failed.attempt_id)
        (Path(binding.path) / "library.py").write_bytes(b"VALUE = 42\n")
        fixed = service.submit(failed.attempt_id, summary="corrected library")
        self.assertNotEqual(failed.id, fixed.id)
        service.review(fixed.id, decision="accept", reviewed_by=self.team.lead_agent_id, reason="改动与失败证据一致", command_id="accept-fixed")
        self.assertEqual(service.validate_and_commit(fixed.id).status.value, "committed")
        self.assertFalse(any(i["status"] == "open" for i in self.repo.list_coordination_issues(self.team.id)))
        publication = self.integrate()
        self.assertEqual(publication["status"], "published", publication)
        self.assertEqual(self.integrate()["status"], "delivered")
        self.assertEqual((self.source / "library.py").read_text(), "VALUE = 42\n")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.head)

    def test_integration_failure_has_owner_and_a_bounded_retry_path(self):
        self.start_v2(checks=[{"id": "failing-check", "stage": "integration", "steps": [
            {"argv": [sys.executable, "-c", "raise SystemExit(1)"]}]}])
        self.candidate("A", {"library.py": "VALUE = 42\n"})
        for retry in range(3):
            failed = self.integrate()
            self.assertEqual(failed["status"], "validation_failed")
            open_issues = [i for i in self.repo.list_coordination_issues(self.team.id) if i["status"] == "open"]
            self.assertEqual(len(open_issues), 1)
            self.assertEqual(open_issues[0]["owner"], "user" if retry == 2 else "lead")
            if retry < 2:
                self.repo.retry_team_integration(failed["id"], actor=self.team.lead_agent_id,
                    reason="环境已经检查，有限重新验证")
                self.assertFalse(any(i["status"] == "open" for i in self.repo.list_coordination_issues(self.team.id)))
            else:
                with self.assertRaisesRegex(Exception, "自动修复次数上限"):
                    self.repo.retry_team_integration(failed["id"], actor=self.team.lead_agent_id, reason="不应无限重试")
        self.assertEqual(self.repo.get_team_run(self.team.id).integration_revision, 0)
        self.assertFalse((self.source / "library.py").exists())


if __name__ == "__main__":
    unittest.main()
