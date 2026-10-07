from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover - optional dependency in core-only installs
    TestClient = None  # type: ignore[assignment,misc]

from codeagent.web.api import create_app
from codeagent.web.storage import SQLiteRepository


class FakeScheduler:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


class FakeTeamSupervisor:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self.cancelled: list[str] = []
        self.resumed: list[tuple[str, dict[str, object]]] = []

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def cancel_attempt(self, attempt_id: str, **_kwargs: object) -> None:
        self.cancelled.append(attempt_id)

    def resume_attempt(self, attempt_id: str, **kwargs: object) -> None:
        self.resumed.append((attempt_id, kwargs))

    def pause_team(self, team_id, **kwargs):
        self.repository.request_team_pause(team_id, **kwargs)
        self.repository.finish_team_pause(team_id)

    def resume_team(self, team_id, **kwargs):
        self.repository.resume_team_run(team_id, fingerprints={}, **kwargs)

    def cancel_team(self, team_id, **kwargs):
        self.repository.cancel_team_run(team_id, cancelled_by="user", **kwargs)


@unittest.skipIf(TestClient is None, "FastAPI test dependencies are not installed")
class TeamApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "source"
        self.workspace.mkdir()
        self._git("init")
        self._git("config", "user.email", "tests@example.invalid")
        self._git("config", "user.name", "CodeAgent Tests")
        (self.workspace / "codeagent").mkdir()
        (self.workspace / "codeagent" / "base.py").write_text(
            "BASE = True\n", encoding="utf-8"
        )
        self._git("add", ".")
        self._git("commit", "-m", "base")
        self.base_commit = self._git("rev-parse", "HEAD")

        self.repository = SQLiteRepository(
            self.root / "state.db", recover_incomplete=False
        )
        self.conversation = self.repository.create_conversation(
            title="Team API", workspace=str(self.workspace.resolve())
        )
        self.run = self.repository.create_run(
            self.conversation.id, status="running"
        )
        assert self.conversation.active_task_list_id is not None
        self.task_list_id = self.conversation.active_task_list_id
        self.task = self.repository.create_task(
            self.task_list_id,
            subject="Implement bounded change",
            description="Only write codeagent/.",
            metadata={"kind": "code", "write_scopes": ["codeagent/"],
                      "validation_commands": ["python -m compileall -q codeagent"]},
        )
        self.scheduler = FakeScheduler()
        self.supervisor = FakeTeamSupervisor()
        self.supervisor.repository = self.repository
        env = SimpleNamespace(
            model_id="test-model",
            max_tokens=4096,
            max_iterations=12,
            team_runtime_enabled=True,
            team_write_enabled=True,
            team_worktree_root=self.root / "managed-worktrees",
        )
        self.app = create_app(
            repository=self.repository,
            scheduler=self.scheduler,
            workspace=self.workspace,
            env=env,
            static_dir=self.root / "missing-dist",
            team_supervisor=self.supervisor,
        )
        self.client_context = TestClient(self.app)
        self.client = self.client_context.__enter__()
        self.assertEqual(self.client.get("/api/health").status_code, 200)

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.repository.close()
        self.temp_dir.cleanup()

    def test_observation_pages_and_details_are_read_only(self) -> None:
        snapshot = self._create_team()
        team_id = snapshot["team"]["id"]
        url = f"/api/teams/{team_id}/changes"
        response = self.client.get(url, params={"limit": 2})
        self.assertEqual(response.status_code, 200, response.text)
        page = response.json()
        self.assertEqual(len(page["items"]), 2)
        self.assertTrue(page["has_more"])
        sequence = page["items"][0]["seq"]
        detail = self.client.get(f"{url}/{sequence}")
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertIn("after", detail.json())
        self.assertEqual(self.client.get(url, params={"limit": 201}).status_code, 422)
        self.assertEqual(self.client.get(url, params={"after": -1}).status_code, 422)
        self.assertEqual(self.client.get(f"{url}/999999999").status_code, 404)
        self.assertEqual(self.client.get("/api/teams/missing/changes").status_code, 404)
        self.assertEqual(self.repository.list_task_attempts(team_id), [])
        self.assertEqual(self.client.get(url, params={"limit": 2}).json(), page)

    def test_pause_resume_and_legacy_recovery_endpoints_return_current_snapshot(self):
        snapshot = self._create_team()
        team_id = snapshot["team"]["id"]
        self.repository.decide_team_plan_revision(team_id, 1, decision="approve", decided_by="user",
            reason="approved", command_id="approve")
        for index, (action, state) in enumerate([("pause", "paused"), ("resume", "running"), ("cancel", "cancelled"), ("resume", "running")]):
            response = self.client.post(f"/api/teams/{team_id}/{action}", json={"reason": "test", "commandId": f"lifecycle-{index}"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["team"]["state"], state)

    def test_invalid_analysis_plan_does_not_create_a_team(self) -> None:
        self.repository.update_task(
            self.task_list_id, self.task.task.id,
            changes={"metadata": {"kind": "analysis", "write_scopes": ["docs/"]}},
        )
        response = self.client.post("/api/teams", json={
            "rootRunId": self.run.id, "taskListId": self.task_list_id,
            "baseCommit": self.base_commit, "teammateCount": 1, "maxTeammates": 2,
            "plan": {"tasks": [{"task_id": self.task.task.id, "write_scopes": ["docs/"]}]},
        })
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("analysis Tasks cannot", response.text)
        self.assertEqual(self.repository.list_team_runs(), [])

    def test_managed_plan_rejects_missing_or_malformed_validation_before_snapshot(self) -> None:
        from unittest.mock import patch
        from codeagent.worktrees import WorktreeManager

        for commands in (None, [], "python -m unittest", [None], [12], [" "]):
            with self.subTest(commands=commands):
                metadata = {"kind": "code", "write_scopes": ["codeagent/"],
                            "validation_commands": commands}
                self.repository.update_task(
                    self.task_list_id, self.task.task.id, changes={"metadata": metadata},
                )
                with patch.object(WorktreeManager, "snapshot_local", side_effect=AssertionError("Invalid plan reached snapshot creation")) as snapshot:
                    response = self.client.post("/api/teams", json={
                        "rootRunId": self.run.id, "taskListId": self.task_list_id,
                        "baseCommit": self.base_commit, "teammateCount": 1,
                        "plan": {"tasks": [{"task_id": self.task.task.id}]},
                    })
                    snapshot.assert_not_called()
                self.assertEqual(response.status_code, 422, response.text)
                self.assertIn("validation", response.text)
                self.assertEqual(self.repository.list_team_runs(), [])

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.workspace), *args],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return result.stdout.strip()

    def _create_team(self, integration_mode="managed") -> dict[str, object]:
        response = self.client.post(
            "/api/teams",
            json={
                "rootRunId": self.run.id,
                "taskListId": self.task_list_id,
                "baseCommit": self.base_commit,
                "integrationMode": integration_mode,
                "teammateCount": 1,
                "maxTeammates": 2,
                "plan": {
                    "tasks": [
                        {
                            "task_id": self.task.task.id,
                            "write_scopes": ["codeagent/"],
                        }
                    ]
                },
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_explicit_manual_team_keeps_existing_integration_flow(self):
        snapshot = self._create_team("manual")
        self.assertEqual(snapshot["team"]["integration_mode"], "manual")
        self.assertEqual(snapshot["team"]["base_commit"], self.base_commit)
        self.assertTrue(snapshot["manual_integration"]["required"])
        self.assertFalse(snapshot["manual_integration"]["automatic_merge"])
        self.assertEqual(snapshot["integrations"], [])

    def test_managed_team_does_not_accept_manual_integration_verification(self):
        snapshot = self._create_team()
        response = self.client.post(f"/api/teams/{snapshot['team']['id']}/manual-integration",
            json={"targetRef": "HEAD", "commandId": "manual-check"})
        self.assertEqual(response.status_code, 409, response.text)

    def test_cumulative_diff_uses_frozen_team_version(self):
        snapshot = self._create_team()
        team_id = snapshot["team"]["id"]
        (self.workspace / "codeagent" / "base.py").write_text("new user edit")
        result = self.client.get(f"/api/teams/{team_id}/integration-diff")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["diff"], "")
        self.assertEqual(result.json()["integration_head"], snapshot["team"]["base_commit"])
        self.assertFalse(result.json()["truncated"])

    def test_create_reject_revise_and_approve_team_plan(self) -> None:
        snapshot = self._create_team()
        team = snapshot["team"]
        team_id = team["id"]
        self.assertEqual(team["state"], "waiting_approval")
        self.assertEqual(len(snapshot["agents"]), 1)
        self.assertEqual(len(snapshot["sessions"]), 1)
        lead_sessions = [
            session
            for session in snapshot["sessions"]
            if session["agent_id"] == team["lead_agent_id"]
        ]
        self.assertEqual(len(lead_sessions), 1)
        self.assertEqual(lead_sessions[0]["state"], "idle")
        self.assertEqual(snapshot["manual_integration"]["automatic_merge"], True)
        self.assertEqual(team["integration_mode"], "managed")
        self.assertEqual(self.repository.list_task_attempts(team_id), [])

        rejection = {
            "decision": "reject",
            "reason": "Narrow the write scope",
            "commandId": "reject-r1",
        }
        first = self.client.post(
            f"/api/teams/{team_id}/plans/1/decision", json=rejection
        )
        repeated = self.client.post(
            f"/api/teams/{team_id}/plans/1/decision", json=rejection
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(repeated.json()["team"]["state"], "planning")
        self.assertIsNone(repeated.json()["team"]["active_plan_revision"])
        self.assertEqual(self.repository.list_task_attempts(team_id), [])

        stale_approval = self.client.post(
            f"/api/teams/{team_id}/plans/1/decision",
            json={
                "decision": "approve",
                "reason": "Try to revive r1",
                "commandId": "approve-rejected-r1",
            },
        )
        self.assertEqual(stale_approval.status_code, 409)

        # The revised approval must describe the actual Task's execution scope.
        self.repository.update_task(
            self.task_list_id, self.task.task.id,
            changes={"metadata": {"kind": "code", "write_scopes": ["codeagent/web/"]}},
        )
        revision = self.client.post(
            f"/api/teams/{team_id}/plans",
            json={
                "plan": {
                    "tasks": [
                        {
                            "task_id": self.task.task.id,
                            "write_scopes": ["codeagent/web/"],
                        }
                    ]
                },
                "commandId": "create-r2",
            },
        )
        self.assertEqual(revision.status_code, 201, revision.text)
        self.assertEqual(revision.json()["plan"]["revision"], 2)
        self.assertEqual(revision.json()["plan"]["status"], "pending_user_approval")
        self.assertEqual(self.repository.list_task_attempts(team_id), [])

        approved = self.client.post(
            f"/api/teams/{team_id}/plans/2/decision",
            json={
                "decision": "approve",
                "reason": "Scope is acceptable",
                "commandId": "approve-r2",
            },
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["team"]["state"], "running")
        self.assertEqual(approved.json()["team"]["active_plan_revision"], 2)

        fetched = self.client.get(f"/api/teams/{team_id}")
        self.assertEqual(fetched.status_code, 200)
        self.assertIn("scheduling", fetched.json())
        self.assertIn("usage", fetched.json())

    def test_team_supervisor_follows_application_lifecycle(self) -> None:
        self.assertTrue(self.scheduler.started)
        self.assertTrue(self.supervisor.started)
        self.client_context.__exit__(None, None, None)
        self.assertTrue(self.scheduler.stopped)
        self.assertTrue(self.supervisor.stopped)
        self.client_context = TestClient(self.app)
        self.client = self.client_context.__enter__()

    def test_user_api_cannot_impersonate_root_lead_reviews(self) -> None:
        snapshot = self._create_team()
        team_id = snapshot["team"]["id"]

        attempt_decision = self.client.post(
            f"/api/teams/{team_id}/attempts/attempt_fake/plan-decision",
            json={
                "revision": 1,
                "decision": "approve",
                "reason": "User must not act as Lead",
                "commandId": "forbidden-user-attempt-review",
            },
        )
        candidate_review = self.client.post(
            f"/api/teams/{team_id}/candidates/candidate_fake/review",
            json={
                "decision": "accept",
                "reason": "User must not act as Lead",
                "commandId": "forbidden-user-candidate-review",
            },
        )

        self.assertEqual(attempt_decision.status_code, 409)
        self.assertIn("Root/Lead", attempt_decision.json()["detail"])
        self.assertEqual(candidate_review.status_code, 409)
        self.assertIn("Root/Lead", candidate_review.json()["detail"])

    def test_snapshot_exposes_recovery_and_resume_endpoint_uses_supervisor(self) -> None:
        snapshot = self._create_team()
        team_id = snapshot["team"]["id"]
        approved = self.client.post(
            f"/api/teams/{team_id}/plans/1/decision",
            json={
                "decision": "approve",
                "reason": "Proceed with the bounded task",
                "commandId": "approve-recovery-api-plan",
            },
        )
        self.assertEqual(approved.status_code, 200, approved.text)

        agent = self.repository.create_team_agent(team_id, name="Teammate 1")
        session = self.repository.create_agent_session(team_id, agent.id)
        task = self.repository.get_task_resource(self.task_list_id, self.task.task.id)
        attempt = self.repository.claim_task_attempt(
            team_id,
            task_id=task.task.id,
            agent_id=agent.id,
            session_id=session.id,
            expected_task_revision=task.revision,
            command_id="claim-recovery-api-task",
        )
        execution = self.repository.begin_tool_execution(
            attempt.id, tool_call_id="failed-submission", tool_name="team_submit_candidate",
            risk="low", is_write=False, input={}, worktree_id=None,
        )
        self.repository.finish_tool_execution(
            execution.id, status="failed",
            error='StorageConflictError: Candidate contains files outside the approved write scope: '
                  '{"outside_paths":["web/app.ts"],"approved_write_scopes":["server/**"]}',
        )
        self.repository.pause_attempt_after_worker_exit(
            attempt.id,
            reason="protocol_incomplete",
        )

        paused = self.client.get(f"/api/teams/{team_id}")
        self.assertEqual(paused.status_code, 200, paused.text)
        recoveries = paused.json()["recoveries"]
        self.assertEqual(len(recoveries), 1)
        self.assertEqual(recoveries[0]["attempt_id"], attempt.id)
        self.assertEqual(recoveries[0]["reason_code"], "protocol_incomplete")
        self.assertTrue(recoveries[0]["recoverable"])
        self.assertIn("代码成果提交失败", recoveries[0]["summary"])
        self.assertEqual(recoveries[0]["tool_status"], "failed")
        self.assertIn("web/app.ts", recoveries[0]["tool_error"])
        self.assertEqual(recoveries[0]["outside_paths"], ["web/app.ts"])

        resumed = self.client.post(
            f"/api/teams/{team_id}/attempts/{attempt.id}/resume",
            json={
                "reason": "I inspected the preserved attempt state",
                "acknowledgeUnknownResult": False,
                "commandId": "resume-recovery-api-task",
            },
        )
        self.assertEqual(resumed.status_code, 200, resumed.text)
        self.assertEqual(self.supervisor.resumed[0][0], attempt.id)
        self.assertEqual(
            self.supervisor.resumed[0][1],
            {
                "reason": "I inspected the preserved attempt state",
                "acknowledge_unknown_result": False,
                "command_id": "resume-recovery-api-task",
                "resumed_by": "user",
            },
        )


if __name__ == "__main__":
    unittest.main()
