"""Team accounting covers every persisted call, across Lead turns and retries."""
import tempfile
import unittest
from pathlib import Path

from codeagent.web.api import _team_snapshot
from codeagent.web.storage import SQLiteRepository


class TeamUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.db"
        self.repo = SQLiteRepository(self.path, recover_incomplete=False)
        self.conversation = self.repo.create_conversation(workspace=self.temp.name)
        self.root = self.repo.create_run(self.conversation.id, status="running")
        self.team = self.repo.create_team_run(
            conversation_id=self.conversation.id, root_run_id=self.root.id,
            task_list_id=self.conversation.active_task_list_id, base_commit="a" * 40,
        )
        self.member = self.repo.create_team_agent(self.team.id, name="Worker")
        self.repo.update_run_status(self.root.id, "completed")

    def tearDown(self):
        self.repo.close()
        self.temp.cleanup()

    def record(self, run, agent="agent_root", kind="main", *, status="completed", usage=None):
        return self.repo.record_model_call(
            run.id, model="test-model", agent_id=agent, call_kind=kind, status=status,
            usage=usage if usage is not None else {
                "input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 5,
            },
        )

    def test_snapshot_includes_workers_later_lead_turns_auxiliary_and_retry(self):
        self.record(self.root)  # Initial planner has the legacy generic agent ID.
        self.record(self.root, self.member.id)
        later = self.repo.create_run(self.conversation.id, status="running")
        self.record(later, self.team.lead_agent_id)
        self.record(later, self.team.lead_agent_id, "context_summary")
        self.record(self.root, self.member.id, "memory", status="failed")
        self.record(self.root, self.member.id, "memory")
        # Matching both agent membership and run metadata must not double count.
        self.repo.update_run_status(later.id, "completed", metadata={"team_run_id": self.team.id})
        snapshot = _team_snapshot(self.repo, self.team.id, allow_code=False)
        usage = snapshot["usage"]
        self.assertEqual(usage["model_calls"], 6)
        self.assertEqual(usage["total_tokens"], 630)
        self.assertEqual(usage["by_agent"][self.member.id]["model_calls"], 3)
        self.assertEqual(usage["by_agent"][self.team.lead_agent_id]["model_calls"], 2)
        self.assertEqual(usage["by_call_kind"]["context_summary"]["model_calls"], 1)
        self.assertEqual(usage["by_model"]["test-model"]["total_tokens"], 630)
        self.repo.close()
        self.repo = SQLiteRepository(self.path, recover_incomplete=False)
        self.assertEqual(self.repo.aggregate_usage(team_run_id=self.team.id), usage)

    def test_inflight_lead_and_metadata_only_legacy_calls_are_included(self):
        later = self.repo.create_run(self.conversation.id, status="running")
        self.record(later, self.team.lead_agent_id)
        legacy = self.repo.create_run(self.conversation.id, status="completed", metadata={"team_run_id": self.team.id})
        self.record(legacy, kind="memory")
        self.assertEqual(self.repo.aggregate_usage(team_run_id=self.team.id)["model_calls"], 2)

    def test_unrelated_runs_other_teams_and_conversations_are_excluded(self):
        self.record(self.root)
        unrelated = self.repo.create_run(self.conversation.id)
        self.record(unrelated)
        self.repo.cancel_team_run(self.team.id, reason="test", cancelled_by="user", command_id="cancel")
        other = self.repo.create_team_run(
            conversation_id=self.conversation.id, root_run_id=unrelated.id,
            task_list_id=self.conversation.active_task_list_id, base_commit="b" * 40,
        )
        self.record(unrelated, other.lead_agent_id)
        conversation = self.repo.create_conversation(workspace=self.temp.name)
        foreign = self.repo.create_run(conversation.id, metadata={"team_run_id": self.team.id})
        self.record(foreign, self.member.id)
        self.assertEqual(self.repo.aggregate_usage(team_run_id=self.team.id)["model_calls"], 1)
        self.assertEqual(self.repo.aggregate_usage(team_run_id="missing")["model_calls"], 0)

    def test_explicitly_linked_planning_run_is_included(self):
        planning = self.repo.create_run(self.conversation.id, status="completed")
        self.repo.set_planning_mode(self.conversation.id, "planning", "team")
        plan = self.repo.save_planning_revision(
            self.conversation.id, run_id=planning.id, title="Team plan", markdown="Steps",
        )
        self.repo.update_plan_execution(plan["id"], status="started", team_run_id=self.team.id)
        self.record(planning)
        self.assertEqual(self.repo.aggregate_usage(team_run_id=self.team.id)["model_calls"], 1)

    def test_weighted_ratio_missing_usage_and_completion_updates(self):
        self.record(self.root, usage={"input_tokens": 1, "cache_read_input_tokens": 9, "output_tokens": 2})
        self.record(self.root, self.member.id, usage={"input_tokens": 90, "cache_read_input_tokens": 10, "output_tokens": 3})
        pending = self.repo.create_model_call(self.root.id, model="other", agent_id=self.member.id)
        usage = self.repo.aggregate_usage(team_run_id=self.team.id)
        self.assertEqual(usage["model_calls"], 3)
        self.assertEqual(usage["unavailable_calls"], 1)
        self.assertEqual(usage["total_tokens"], 115)
        self.assertAlmostEqual(usage["cache_hit_ratio"], 19 / 110)
        self.repo.complete_model_call(pending.id, usage={"input_tokens": 4, "output_tokens": 1})
        usage = self.repo.aggregate_usage(team_run_id=self.team.id)
        self.assertEqual(usage["model_calls"], 3)
        self.assertEqual(usage["unavailable_calls"], 0)
        self.assertEqual(usage["total_tokens"], 120)


if __name__ == "__main__":
    unittest.main()
