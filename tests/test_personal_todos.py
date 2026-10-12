from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codeagent import Agent, AgentConfig, EnvironmentConfig, ModelResponse
from codeagent.context import ContextConfig
from codeagent.events import EventEmitter, ExecutionContext, RecordingEventSink, RunEvent
from codeagent.memory import MemoryConfig
from codeagent.permissions import WaitingPermissionBroker
from codeagent.prompts import PromptMode
from codeagent.runtime import CancellationToken
from codeagent.tools import TodoStore, TodoWriteTool, create_default_registry
from codeagent.web.factory import WebAgentFactory
from codeagent.web.storage import SQLiteRepository


class Client:
    def create_message(self, **kwargs):
        return ModelResponse("end_turn", [{"type": "text", "text": "done"}])


class PersonalTodoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = SQLiteRepository(self.root / "state.db", recover_incomplete=False)
        self.conv = self.repo.create_conversation(workspace=self.root)
        self.run = self.repo.create_run(self.conv.id, status="running")
        self.env = EnvironmentConfig(model_id="fake", data_dir=self.root / "runtime",
            enable_skills=False, memory_config=MemoryConfig(enabled=False),
            context_config=ContextConfig(mode="off"), team_runtime_enabled=True)
        self.factory = WebAgentFactory(self.env, self.root, self.repo)

    def tearDown(self):
        self.factory.close()
        self.repo.close()
        self.tmp.cleanup()

    def agent(self, *, conversation=None, run=None, session=None, attempt=None, checkpoint=None,
              planner=False):
        emitter = EventEmitter(RecordingEventSink(self.repo), context=ExecutionContext(
            conversation_id=(conversation or self.conv).id, run_id=(run or self.run).id,
            agent_id=session.agent_id if session else "agent_root",
            parent_agent_id="lead" if session else None))
        with patch.object(EnvironmentConfig, "create_model_client", return_value=Client()):
            return self.factory.create(event_emitter=emitter, cancellation=CancellationToken(),
                permission_broker=WaitingPermissionBroker(), checkpoint=checkpoint,
                team_session=session, team_attempt=attempt,
                worktree_manager=object() if session else None,
                root_prompt_mode=PromptMode.TEAM_PLANNER if planner else None)

    def write(self, agent, text="Implement", status="in_progress"):
        result = agent.tools.execute("todo_write", {"todos": [{"content": text, "status": status}]})
        self.assertFalse(result.startswith("Error:"), result)

    def test_root_uses_personal_tool_and_restores_latest_durable_list_before_checkpoint(self):
        agent = self.agent()
        self.assertIn("todo_write", agent.tools)
        self.assertNotIn("TaskCreate", agent.tools)
        self.assertNotIn("TaskList", agent.tools)
        self.write(agent)
        checkpoint = SimpleNamespace(messages=[], context={}, todos=[{"content": "Old", "status": "pending"}], metadata={})
        restored = self.agent(checkpoint=checkpoint)
        self.assertEqual(restored.context.todo_store.todos, agent.context.todo_store.todos)
        self.assertIn("Implement", restored.context._task_state())
        self.assertEqual(self.repo.list_task_resources(self.conv.active_task_list_id), [])

    def test_clearing_survives_database_reopen_and_does_not_restore_stale_checkpoint(self):
        agent = self.agent()
        self.write(agent)
        agent.tools.execute("todo_write", {"todos": []})
        self.repo.close()
        self.repo = SQLiteRepository(self.root / "state.db", recover_incomplete=False)
        self.factory.task_service = self.repo
        checkpoint = SimpleNamespace(messages=[], context={}, todos=[{"content": "Old", "status": "pending"}], metadata={})
        self.assertEqual(self.agent(checkpoint=checkpoint).context.todo_store.todos, [])
        self.assertEqual(self.repo.get_personal_todos(self.conv.id, "agent_root")["revision"], 2)

    def test_conversations_with_same_root_id_are_isolated(self):
        first = self.agent()
        other = self.repo.create_conversation(workspace=self.root)
        other_run = self.repo.create_run(other.id, status="running")
        second = self.agent(conversation=other, run=other_run)
        self.write(first, "First")
        self.write(second, "Second")
        self.assertEqual(self.repo.get_personal_todos(self.conv.id, "agent_root")["todos"][0]["content"], "First")
        self.assertEqual(self.repo.get_personal_todos(other.id, "agent_root")["todos"][0]["content"], "Second")

    def test_failed_persistence_and_invalid_input_do_not_advance_in_memory(self):
        agent = self.agent()
        self.write(agent, "Before")
        with patch.object(self.repo, "append_event", side_effect=OSError("disk full")):
            result = agent.tools.execute("todo_write", {"todos": []})
        self.assertIn("Error:", result)
        self.assertEqual(agent.context.todo_store.todos[0]["content"], "Before")
        result = agent.tools.execute("todo_write", {"todos": [
            {"content": "A", "status": "in_progress"}, {"content": "B", "status": "in_progress"}]})
        self.assertIn("Error:", result)
        self.assertEqual(self.repo.get_personal_todos(self.conv.id, "agent_root")["revision"], 1)

    def test_event_projection_is_atomic_and_replayed_event_is_idempotent(self):
        event = RunEvent(type="todo.updated", run_id=self.run.id, conversation_id=self.conv.id,
                         payload={"todos": [{"content": "One", "status": "pending"}]})
        self.repo.append_event(event)
        self.repo.append_event(event)
        self.assertEqual(self.repo.get_personal_todos(self.conv.id, "agent_root")["revision"], 1)
        before = len(self.repo.list_events(self.run.id))
        with self.assertRaises(ValueError):
            self.repo.append_event(RunEvent(type="todo.updated", run_id=self.run.id, payload={"todos": "bad"}))
        self.assertEqual(len(self.repo.list_events(self.run.id)), before)

    def test_legacy_task_list_keeps_dependency_checks_and_original_tools(self):
        first = self.repo.create_task(self.conv.active_task_list_id, subject="First", description="First")
        second = self.repo.create_task(self.conv.active_task_list_id, subject="Second", description="Second", blocked_by=[first.task.id])
        agent = self.agent()
        self.assertIn("TaskUpdate", agent.tools)
        self.assertNotIn("todo_write", agent.tools)
        result = agent.tools.execute("TaskUpdate", {"taskId": second.task.id, "status": "in_progress"})
        self.assertIn("Error:", result)
        self.assertEqual(self.repo.get_task_resource(self.conv.active_task_list_id, second.task.id).task.status.value, "pending")

    def test_task_readers_can_coexist_but_mutators_cannot(self):
        registry = create_default_registry(planning_backend="tasks", task_service=self.repo,
                                           task_list_id=self.conv.active_task_list_id)
        registry.register(TodoWriteTool(store=TodoStore()))
        with self.assertRaisesRegex(ValueError, "cannot be registered together"):
            Agent(client=Client(), tools=registry, config=AgentConfig(model="fake"))
        readers = registry.copy_without({"TaskCreate", "TaskUpdate"})
        agent = Agent(client=Client(), tools=readers, config=AgentConfig(model="fake"), allow_subagents=False)
        self.assertIn("TaskGet", agent.tools)
        self.assertIn("todo_write", agent.tools)

    def test_team_members_have_separate_personal_lists_without_completing_shared_tasks(self):
        planner = self.agent(planner=True)
        self.assertIn("TaskCreate", planner.tools)
        self.assertNotIn("todo_write", planner.tools)
        team = self.repo.create_team_run(conversation_id=self.conv.id, root_run_id=self.run.id,
            task_list_id=self.conv.active_task_list_id, base_commit="b" * 40, lead_agent_id="lead")
        plan = self.repo.create_team_plan_revision(team.id, plan={"tasks": []}, created_by="lead", command_id="create")
        self.repo.submit_team_plan_revision(team.id, plan.revision, command_id="submit")
        self.repo.decide_team_plan_revision(team.id, plan.revision, decision="approve", decided_by="user", reason="approved", command_id="approve")
        members = []
        for suffix in ("a", "b"):
            task = self.repo.create_task(self.conv.active_task_list_id, subject=suffix, description=suffix,
                                        metadata={"kind": "analysis", "write_scopes": []})
            member = self.repo.create_team_agent(team.id, name=suffix)
            session = self.repo.create_agent_session(team.id, member.id)
            attempt = self.repo.claim_task_attempt(team.id, task_id=task.task.id, agent_id=member.id,
                session_id=session.id, expected_task_revision=task.revision, command_id=suffix)
            agent = self.agent(session=session, attempt=attempt)
            self.assertIn("TaskGet", agent.tools)
            self.assertNotIn("TaskUpdate", agent.tools)
            self.write(agent, suffix, "completed")
            event = [item for item in self.repo.list_events(self.run.id) if item.type == "todo.updated"][-1]
            self.assertEqual(event.payload["team_run_id"], team.id)
            self.assertEqual(event.payload["owner_id"], session.id)
            self.assertEqual(self.repo.get_task_resource(self.conv.active_task_list_id, task.task.id).task.status.value, "in_progress")
            members.append((agent, session, attempt))
        self.assertEqual(members[0][0].context.todo_store.todos[0]["content"], "a")
        restored = self.agent(session=members[0][1], attempt=members[0][2])
        self.assertEqual(restored.context.todo_store.todos[0]["content"], "a")
        self.assertIn("个人清单", restored.prompt_runtime.assemble(
            mode=PromptMode.TEAMMATE_ANALYSIS, tool_schemas=restored.tools.schemas()).system_prompt)


if __name__ == "__main__":
    unittest.main()
