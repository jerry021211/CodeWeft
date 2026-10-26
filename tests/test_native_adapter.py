import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from codeagent import ModelResponse
from codeagent.config import EnvironmentConfig
from codeagent.events import EventEmitter, ExecutionContext, CallbackEventSink, TokenUsage
from codeagent.memory import MemoryConfig
from codeagent.permissions import WaitingPermissionBroker
from codeagent.runtime import CancellationToken
from codeagent.multiagent.adapter import recover_root
from codeagent.multiagent.executions import ExecutionRepository
from codeagent.multiagent.tools import EXECUTION_TOOL_NAMES
from codeagent.tools.tasks import TASK_TOOL_NAMES
from codeagent.web.factory import WebAgentFactory, serialize_runtime_state
from codeagent.web.storage import SQLiteRepository
from codeagent.web.scheduler import RunScheduler


def text_response(text="done"):
    return ModelResponse("end_turn", [{"type": "text", "text": text}])


def tool_response(name, args, identifier="call"):
    return ModelResponse("tool_use", [{"type": "tool_use", "id": identifier, "name": name, "input": args}])


class Client:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def create_message(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0) if self.responses else text_response()


class NativeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.repo = SQLiteRepository(self.root / "web.db", recover_incomplete=False)
        self.conv = self.repo.create_conversation(workspace=self.source)
        self.run = self.repo.create_run(self.conv.id, status="completed")
        self.env = EnvironmentConfig(model_id="test", data_dir=self.root / "runtime", enable_skills=False,
                                     memory_config=MemoryConfig(enabled=False))
        self.factory = WebAgentFactory(self.env, self.source, self.repo)
        self.sink = SimpleNamespace(events=[])
        self.emitter = EventEmitter(CallbackEventSink(self.sink.events.append), context=ExecutionContext(conversation_id=self.conv.id, run_id=self.run.id))
        self.token = CancellationToken()

    def tearDown(self):
        self.factory.close()
        self.repo.close()
        self.temp.cleanup()

    def agent(self, **kwargs):
        return self.factory.create(event_emitter=self.emitter, cancellation=self.token,
                                   permission_broker=WaitingPermissionBroker(), **kwargs)

    def test_direct_turn_keeps_no_collaboration_storage_and_stable_schemas(self):
        client = Client(text_response(), text_response())
        with patch.object(EnvironmentConfig, "create_model_client", return_value=client):
            plain = self.agent()
            native = self.agent(native_collaboration=True)
            before = native.tools.schemas()
            native.run("hello")
            native.run("another simple question")
        self.assertEqual(before, native.tools.schemas())
        self.assertEqual([s for s in before if s["name"] not in EXECUTION_TOOL_NAMES | TASK_TOOL_NAMES],
                         [s for s in plain.tools.schemas() if s["name"] not in {"subagent", "subagent_result", "subagent_cancel"}])
        self.assertEqual(plain.tools.parallel_safe("read_file"), native.tools.parallel_safe("read_file"))
        self.assertFalse((self.factory.data_paths.workspace_dir(self.source) / "collaborations").exists())

    def create_task(self, subject='inspect', profile='research', **config):
        tl = self.repo.ensure_conversation_task_list(self.conv.id)
        return self.repo.create_task(tl.id, subject=subject, description=subject,
                     metadata={'execution': {'profile': profile, **config}}).task.id

    def test_model_delegates_research_without_team_or_git(self):
        key = self.create_task()
        root = Client(tool_response('delegate_task', {'taskId': key}), text_response('reported'))
        child = Client(text_response('inspection complete'))
        with patch.object(EnvironmentConfig, 'create_model_client', side_effect=[root, child]):
            result = self.agent(native_collaboration=True).run('Investigate using a worker')
        self.assertEqual(result.final_text, 'reported')
        snapshots = [e.payload for e in self.sink.events if e.type == 'collaboration.snapshot']
        self.assertEqual(snapshots[-1]['status'], 'completed', snapshots)
        self.assertFalse((self.source / '.git').exists())
        self.assertIsNone(self.repo.get_active_team_run_for_conversation(self.conv.id))
        self.assertFalse(any(t['name'] in EXECUTION_TOOL_NAMES | {'subagent', 'write_file', 'todo_write', 'TaskUpdate'} for t in child.calls[0]['tools']))

    def test_creating_task_does_not_switch_native_tools_or_cache_prefix(self):
        root = Client(tool_response('TaskCreate', dict(subject='inspect', description='Inspect config',
                      metadata={'execution': {'profile': 'research'}})),
                      tool_response('delegate_task', {'taskId': '1'}, 'delegate'), text_response())
        child = Client(text_response('inspected'))
        with patch.object(EnvironmentConfig, 'create_model_client', side_effect=[root, child, Client()]):
            agent = self.agent(native_collaboration=True)
            original = agent.tools.schemas()
            agent.run('Inspect with a worker')
            restored = self.agent(native_collaboration=True)
        self.assertEqual(original, restored.tools.schemas())
        self.assertTrue(all(call['tools'] == root.calls[0]['tools'] for call in root.calls))
        self.assertTrue(all(call['system'] == root.calls[0]['system'] for call in root.calls))
        snapshots = [e.payload for e in self.sink.events if e.type == 'collaboration.snapshot']
        self.assertEqual(snapshots[-1]['status'], 'completed')

    def test_worker_receives_original_constraints_not_just_task_description(self):
        key = self.create_task()
        root = Client(tool_response('delegate_task', {'taskId': key}), text_response())
        child = Client(text_response('inspected'))
        requirement = 'Only use Python standard library. Preserve public API. Report in Chinese.'
        with patch.object(EnvironmentConfig, 'create_model_client', side_effect=[root, child]):
            self.agent(native_collaboration=True).run(requirement)
        request = json.dumps(child.calls[0]['messages'], ensure_ascii=False)
        self.assertIn(requirement, request)
        record = ExecutionRepository(self.repo).list(self.conv.id, self.source)[0]
        self.assertEqual(record['context']['requests'][0]['text'], requirement)

    def test_native_worker_shares_budget_and_root_recovery_keeps_latest_usage(self):
        key = self.create_task()
        root = Client(tool_response('delegate_task', {'taskId': key}), text_response('reported'))
        child = Client(text_response('inspection complete'))
        for response in [*root.responses, *child.responses]:
            response.usage = TokenUsage(input_tokens=10, output_tokens=2)
        with patch.object(EnvironmentConfig, 'create_model_client', side_effect=[root, child, Client()]):
            agent = self.agent(native_collaboration=True)
            agent.run('Investigate using a worker')
            budget = agent._loop_guard.budget
            self.assertEqual(budget.state.model_calls, 3)
            self.assertEqual(budget.state.total_tokens, 36)
            repo = ExecutionRepository(self.repo)
            saved = repo.load_root(self.run.id, self.conv.id)
            self.assertEqual(saved['budget']['total_tokens'], 36)
            # The safe root message checkpoint can precede the latest child usage.
            saved['checkpoint']['metadata']['execution_guard']['budget']['total_tokens'] = 0
            repo.save_root(self.run.id, self.conv.id, saved['checkpoint'], ['delegate_task'])
            self.assertTrue(recover_root(self.factory, self.repo, self.run))
            checkpoint = self.repo.get_latest_checkpoint(self.conv.id)
            self.assertEqual(checkpoint.metadata['execution_guard']['budget']['total_tokens'], 36)
            restored = self.agent(native_collaboration=True, checkpoint=checkpoint)
            self.assertEqual(restored._loop_guard.budget.state.total_tokens, 36)

    def test_member_checkpoint_cannot_reset_shared_budget_or_tool_admission(self):
        with patch.object(EnvironmentConfig, 'create_model_client', return_value=Client()):
            root = self.agent(native_collaboration=True)
            root._loop_guard.budget.reserve('model')
            guard = deepcopy(root.export_execution_state())
            guard['budget']['model_calls'] = 999
            checkpoint = SimpleNamespace(messages=[], context={}, metadata={'execution_guard': guard})
            child = self.agent(native_worker=True, checkpoint=checkpoint,
                execution_budget=root._loop_guard.budget, tool_admission=root.tool_admission)
        self.assertIs(child._loop_guard.budget, root._loop_guard.budget)
        self.assertIs(child.tool_admission, root.tool_admission)
        child._loop_guard.begin('another-worker-scope')
        self.assertEqual(root._loop_guard.budget.state.model_calls, 1)

    def test_child_tokens_stop_the_root_before_another_model_call(self):
        self.factory.env = replace(self.env,
            loop_guard_config=replace(self.env.loop_guard_config, max_total_tokens=100))
        key = self.create_task()
        root_response = tool_response('delegate_task', {'taskId': key})
        root_response.usage = TokenUsage(input_tokens=60, output_tokens=0)
        child_response = text_response('inspected')
        child_response.usage = TokenUsage(input_tokens=60, output_tokens=0)
        root, child = Client(root_response, text_response('must not run')), Client(child_response)
        with patch.object(EnvironmentConfig, 'create_model_client', side_effect=[root, child]):
            agent = self.agent(native_collaboration=True)
            result = agent.run('Investigate')
        self.assertEqual(result.stop_reason, 'budget_exceeded:tokens')
        self.assertEqual(len(root.calls), 1)
        self.assertEqual(len(child.calls), 1)
        self.assertEqual(agent._loop_guard.budget.state.total_tokens, 120)
        saved = ExecutionRepository(self.repo).load_root(self.run.id, self.conv.id)
        self.assertEqual(saved['budget']['total_tokens'], 120)

    def test_plan_mode_tool_cannot_start_write_task(self):
        key = self.create_task('write', profile='implement')
        with patch.object(EnvironmentConfig, 'create_model_client', return_value=Client()):
            agent = self.agent(native_collaboration=True, plan_mode=True)
            output = agent.tools.execute('delegate_task', {'taskId': key})
        self.assertEqual(output.status, 'error')
        self.assertIn('read-only', str(output))
        self.assertFalse((self.source / '.git').exists())

    def test_real_worker_write_tool_and_delivery(self):
        key = self.create_task('Write a.txt with hello', profile='implement', files=['a.txt'],
                  checks=[{'argv': [sys.executable, '-c', "from pathlib import Path; assert Path('a.txt').read_text() == 'hello'"]}])
        root = Client(tool_response('execute_plan', {'taskIds': [key]}), text_response())
        child = Client(tool_response('write_file', {'file_path': 'a.txt', 'content': 'hello'}), text_response('created a.txt'))
        with patch.object(EnvironmentConfig, 'create_model_client', side_effect=[root, child]):
            self.agent(native_collaboration=True).run('Implement with a worker')
        snapshots = [e.payload for e in self.sink.events if e.type == 'collaboration.snapshot']
        self.assertEqual(snapshots[-1]['status'], 'completed', snapshots[-1])
        self.assertEqual((self.source / 'a.txt').read_text(), 'hello')

    def test_restart_recovery_only_accepts_safe_root_boundary(self):
        with patch.object(EnvironmentConfig, "create_model_client", return_value=Client()):
            agent = self.agent()
        checkpoint = dict(messages=[{"role": "user", "content": "goal"}], todos=[],
                          context=serialize_runtime_state(agent.context.state), metadata={"native_collaboration": True})
        native = ExecutionRepository(self.repo)
        native.save_root(self.run.id, self.conv.id, checkpoint, ["write_file", "execute_plan"])
        self.assertFalse(recover_root(self.factory, self.repo, self.run))
        native.save_root(self.run.id, self.conv.id, checkpoint, ["execute_plan"])
        self.assertTrue(recover_root(self.factory, self.repo, self.run))
        self.assertTrue(self.repo.get_latest_checkpoint(self.conv.id).metadata["native_collaboration"])

    def test_scheduler_pins_new_schema_and_preserves_legacy_checkpoint_schema(self):
        client = Client(text_response(), text_response(), text_response())
        scheduler = RunScheduler(self.repo, self.factory)
        def execute(conv):
            run = scheduler.submit(conv.id, "hello")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                current = self.repo.get_run(run.id)
                if current.status in {"completed", "failed", "cancelled"}:
                    self.assertEqual(current.status, "completed", current.error)
                    return
                time.sleep(.01)
            self.fail("scheduler did not finish")
        try:
            with patch.object(EnvironmentConfig, "create_model_client", return_value=client):
                execute(self.conv)
                execute(self.conv)
                old = self.repo.create_conversation(workspace=self.source)
                old_run = self.repo.create_run(old.id, status="completed")
                checkpoint = self.repo.get_latest_checkpoint(self.conv.id)
                self.repo.save_checkpoint(old_run.id, messages=[], todos=[], context={}, metadata={"tool_history_version": 1})
                execute(old)
            self.assertTrue(checkpoint.metadata["native_collaboration"])
            self.assertEqual(client.calls[0]["tools"], client.calls[1]["tools"])
            self.assertIn("execute_plan", {t["name"] for t in client.calls[0]["tools"]})
            self.assertNotIn("execute_plan", {t["name"] for t in client.calls[2]["tools"]})
        finally:
            scheduler.stop()


if __name__ == "__main__":
    unittest.main()
