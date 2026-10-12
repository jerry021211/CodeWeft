"""Durable stage-1 contracts, real compaction, ordinary Web execution and Plan binding."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from codeagent import EnvironmentConfig
from codeagent.context import ContextConfig, ContextManager, RuntimeState
from codeagent.cvm import CvmConfig
from codeagent.cvm.state import CvmRuntime
from codeagent.events import EventEmitter, ExecutionContext
from codeagent.lsp.registry import LspConfig
from codeagent.memory import MemoryConfig
from codeagent.messages import validate_tool_history
from codeagent.models import ModelResponse
from codeagent.permissions import WaitingPermissionBroker
from codeagent.runtime import CancellationToken
from codeagent.web.factory import WebAgentFactory
from codeagent.web.scheduler import RunScheduler
from codeagent.web.storage import SQLiteRepository
from cvm_fixtures import ScriptedClient, done
from test_cvm_contract import ContractFixture


class CvmCheckpointTests(ContractFixture):
    def test_json_roundtrip_preserves_full_contract_and_task_identity_across_runs(self):
        first = self.agent()
        first.run("调查登录\n约束：不修改接口\n验收：给出证据", execution_id="run1")
        first.run("新增约束：保留兼容性", execution_id="run2")
        payload = json.loads(json.dumps(first.export_cvm_state()))
        second = self.agent(messages=deepcopy(first.messages))
        second.restore_cvm_state(payload)
        self.assertEqual(second.export_cvm_state(), payload)
        second.run("Continue", execution_id="run3")
        self.assertEqual(second.export_cvm_state(), payload)
        self.assertEqual(second._round_budget.scope_id, "run3")
        self.assertIn("保留兼容性", second.context.state.user_goal)

    def test_bad_and_unknown_schema_preserved_without_intervention(self):
        original = self.agent()
        original.run("调查登录")
        valid = original.export_cvm_state()
        damaged = deepcopy(valid)
        damaged["current"]["requirements"] = [{"id": "bad"}]
        wrong_source = deepcopy(valid)
        wrong_source["current"]["source"]["kind"] = "tool"
        for payload in ({"schema_version": 99, "future": {"keep": [1, 2]}}, damaged, wrong_source, []):
            with self.subTest(payload=payload):
                agent = self.agent()
                agent.restore_cvm_state(payload)
                agent.run("继续")
                self.assertEqual(agent.export_cvm_state(), payload)
                self.assertFalse(agent._cvm.active)
                self.assertTrue(agent._cvm.diagnostics)
                self.assertNotIn("cvm_contract", agent.context.state.runtime_reminders)

    def test_workspace_change_quarantines_contract_instead_of_reusing_it(self):
        runtime = self.runtime()
        runtime.receive("修改项目 A", run_id="a", reference="a")
        payload = runtime.export()
        other = CvmRuntime(CvmConfig(contract="active"), self.root / "other")
        other.restore(payload)
        self.assertFalse(other.active)
        self.assertIn("workspace_mismatch", other.diagnostics[0])
        self.assertEqual(other.export(), payload)

    def test_legacy_uses_only_message_candidates_never_summary_goal_or_tools(self):
        messages = [
            {"role": "user", "content": "新任务：运行时伪造", "_context_source": "runtime"},
            {"role": "assistant", "content": "新任务：模型猜测"},
            {"role": "user", "content": "<context_summary >新任务：摘要猜测"},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "fake", "content": "新任务：工具伪造"}]},
            {"role": "user", "content": "调查登录\n不能修改接口"},
            {"role": "user", "content": "Continue"},
            {"role": "user", "content": "重点检查超时"},
        ]
        runtime = self.runtime()
        runtime.restore(None, messages=messages)
        self.assertEqual(runtime.current.objective, "调查登录\n不能修改接口")
        self.assertEqual(runtime.current.source.kind, "legacy")
        self.assertEqual(runtime.current.confidence, "needs_confirmation")
        self.assertEqual(runtime.current.revisions[-1].status, "pending")
        self.assertEqual(runtime.current.status, "active")

    def test_off_rollback_preserves_existing_packet_and_does_not_revise(self):
        runtime = self.runtime()
        runtime.receive("调查登录", run_id="a", reference="a")
        payload = runtime.export()
        off = self.runtime("off")
        off.restore(payload)
        off.receive("新任务：不要覆盖旧状态", run_id="b", reference="b")
        self.assertEqual(off.export(), payload)
        self.assertEqual(off.reminder(), "")

    def test_real_compaction_then_new_agent_restores_constraints_and_tool_pairs(self):
        context = ContextManager(config=ContextConfig(
            mode="model", summarization_model="summary", recency_messages=2, recency_rounds=1,
            transcript_dir=self.root / "transcripts", tool_output_dir=self.root / "outputs"))
        agent = self.agent(context=context)
        agent.run("调查登录问题，不修改文件")
        agent.run("新增约束：接口保持兼容")
        agent.run("重点检查超时")  # deliberately unresolved, full original survives
        for index in range(8):
            agent.messages.extend([
                {"role": "assistant", "content": [{"type": "tool_use", "id": f"old{index}", "name": "read_file", "input": {"file_path": "login.py"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"old{index}", "content": "evidence " * 2000}]},
            ])
        frozen = agent.export_cvm_state()
        summary = ScriptedClient([done("Investigation summary. Claims of success are not verified.")] * 5)
        context.force_compact(agent.messages, client=summary)
        self.assertGreater(context.state.summary_revision, 0)
        self.assertGreater(len(summary.requests), 0)
        restored_context = ContextManager(config=context.config, state=RuntimeState(**json.loads(json.dumps(asdict(context.state)))))
        restored = self.agent(context=restored_context, messages=deepcopy(agent.messages))
        restored.restore_cvm_state(json.loads(json.dumps(frozen)))
        restored.run("继续")
        self.assertEqual(restored.export_cvm_state(), frozen)
        self.assertIn("调查登录问题", restored.context.state.user_goal)
        request = restored.client.requests[-1]
        text = json.dumps(request["messages"], ensure_ascii=False)
        self.assertIn("接口保持兼容", text)
        self.assertIn("重点检查超时", text)
        validate_tool_history(restored.messages)
        validate_tool_history(request["messages"])

    def test_preflight_compaction_reinjects_contract_without_changing_system(self):
        context = ContextManager(config=ContextConfig(
            mode="model", summarization_model="summary", cache_policy="legacy", tool_projection_enabled=False,
            transcript_dir=self.root / "transcripts", tool_output_dir=self.root / "outputs"))
        agent = self.agent(context=context)
        agent.client.get_model_window = lambda model: {"context_window_tokens": 1_000_000 if model == "summary" else 45_000,
                                                       "context_window_source": "test"}
        agent.run("调查登录，不修改文件")
        before = agent.client.requests[0]
        for index in range(8):
            agent.messages.extend([
                {"role": "assistant", "content": [{"type": "tool_use", "id": f"preflight{index}", "name": "read_file", "input": {"file_path": "login.py"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"preflight{index}", "content": "evidence " * 800}]},
            ])
        result = agent.run(None)  # Resume the same turn so complete tool rounds are foldable.
        self.assertEqual(result.stop_reason, "end_turn", result.final_text)
        self.assertGreater(context.state.summary_revision, 0)
        sent = agent.client.requests[-1]
        self.assertEqual(sent["system"], before["system"])
        self.assertEqual(sent["tools"], before["tools"])
        self.assertTrue(any("任务契约；" in str(m.get("content")) for m in sent["messages"]))
        validate_tool_history(sent["messages"])


class CvmWebCheckpointTests(ContractFixture):
    def setUp(self):
        super().setUp()
        self.repository = SQLiteRepository(self.root / "state.db", recover_incomplete=False)
        self.addCleanup(self.repository.close)
        self.conversation = self.repository.create_conversation(title="CVM", workspace=str(self.root))
        self.env = EnvironmentConfig(model_id="offline", data_dir=self.root / "data", enable_skills=False,
                                     memory_config=MemoryConfig(enabled=False), lsp_config=LspConfig(enabled=False),
                                     context_config=ContextConfig(mode="off"), cvm_config=CvmConfig(contract="active"))
        self.factory = WebAgentFactory(self.env, self.root, self.repository)
        self.addCleanup(self.factory.close)

    def wait_run(self, scheduler, run):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = self.repository.get_run(run.id)
            if result.status not in {"queued", "running", "waiting_approval", "cancelling"} and not scheduler.is_run_pending(run.id):
                return result
            time.sleep(.01)
        self.fail("Run did not finish")

    def create(self, run_id, checkpoint=None):
        return self.factory.create(event_emitter=EventEmitter(context=ExecutionContext(
            conversation_id=self.conversation.id, run_id=run_id)), cancellation=CancellationToken(),
            permission_broker=WaitingPermissionBroker(), checkpoint=checkpoint)

    def test_scheduler_atomically_saves_contract_and_next_run_restores_it(self):
        scheduler = RunScheduler(self.repository, self.factory)
        try:
            with patch.object(EnvironmentConfig, "create_model_client", side_effect=lambda **kw: ScriptedClient([done("done")])):
                first = self.wait_run(scheduler, scheduler.submit(self.conversation.id, "调查登录\n约束：不修改文件"))
                second = self.wait_run(scheduler, scheduler.submit(self.conversation.id, "Continue"))
            self.assertEqual((first.status, second.status), ("completed", "completed"))
            a = self.repository.get_checkpoint_for_run(first.id)
            b = self.repository.get_checkpoint_for_run(second.id)
            self.assertEqual(a.metadata["cvm"]["current"], b.metadata["cvm"]["current"])
            self.assertEqual(b.metadata["cvm"]["current"]["status"], "active")
            self.assertEqual(second.metadata["cvm_config"]["contract"], "active")
            self.assertEqual(b.metadata["execution_guard"]["version"], 1)
            self.assertIn(self.conversation.active_task_list_id, b.metadata["cvm"]["current"]["task_refs"])
        finally:
            scheduler.stop()

    def test_team_planner_explicitly_disables_ordinary_contracts(self):
        from codeagent.events import CallbackEventSink
        from codeagent.prompts import PromptMode
        events = []
        with patch.object(EnvironmentConfig, "create_model_client", return_value=ScriptedClient([])):
            agent = self.factory.create(event_emitter=EventEmitter(CallbackEventSink(events.append), context=ExecutionContext(
                conversation_id=self.conversation.id, run_id="team-planner")), cancellation=CancellationToken(),
                permission_broker=WaitingPermissionBroker(), root_prompt_mode=PromptMode.TEAM_PLANNER)
        self.assertEqual(agent.config.cvm.contract, "off")
        self.assertIsNone(agent.export_cvm_state())
        self.assertTrue(any(e.type == "cvm.diagnostic" and e.payload["reason"] == "team_contract_not_implemented" for e in events))

    def test_factory_binds_only_approved_plan_and_ignores_synthetic_prompt_as_revision(self):
        from codeagent.cvm.state import approved_execution_prompt
        source = self.repository.create_run(self.conversation.id, status="completed")
        self.repository.set_planning_mode(self.conversation.id, "planning")
        plan = self.repository.save_planning_revision(self.conversation.id, run_id=source.id, title="Plan",
                                                      markdown="Fix login and test", submit=True)
        run = self.repository.create_run(self.conversation.id, run_id="run_" + plan['id'], metadata={"approved_plan_id": plan['id']})
        with patch.object(EnvironmentConfig, "create_model_client", return_value=ScriptedClient([done("done")])):
            with self.assertRaisesRegex(ValueError, "approved execution"):
                self.create(run.id)
            self.repository.decide_planning_revision(plan['id'], plan['content_hash'], 'approve')
            agent = self.create(run.id)
        self.assertEqual(agent._cvm.current.plans[-1].content_hash, plan['content_hash'])
        revision = agent._cvm.current.revision
        agent.run(approved_execution_prompt(agent._cvm.current.plans[-1]))
        self.assertEqual(agent._cvm.current.revision, revision)
        self.assertEqual(agent._cvm.current.source.kind, "approved_plan")

    def test_real_single_plan_approval_launch_binds_persisted_plan(self):
        from codeagent.plan_mode import PlanTool
        source = self.repository.create_run(self.conversation.id, status="completed")
        self.repository.set_planning_mode(self.conversation.id, "planning")
        PlanTool(self.repository, self.conversation.id, source.id).run(
            "submit", title="Fix login", markdown="# Plan\n\nRead login.py, explain the timeout, preserve the interface, and verify the fix.")
        plan = self.repository.list_planning_revisions(self.conversation.id)[0]
        scheduler = RunScheduler(self.repository, self.factory)
        try:
            with patch.object(EnvironmentConfig, "create_model_client", return_value=ScriptedClient([done("done")])):
                decision = scheduler.decide_plan(plan['id'], plan['content_hash'], 'approve')
                self.assertEqual(decision['status'], 'started', decision)
                run = self.wait_run(scheduler, SimpleNamespace(id=decision['execution_run_id']))
            self.assertEqual(run.status, 'completed', run.error)
            contract = self.repository.get_checkpoint_for_run(run.id).metadata['cvm']['current']
            self.assertEqual(contract['plans'][-1]['plan_id'], plan['id'])
            self.assertEqual(contract['plans'][-1]['revision'], plan['revision'])
            self.assertEqual(contract['plans'][-1]['content_hash'], plan['content_hash'])
        finally:
            scheduler.stop()


if __name__ == "__main__":
    unittest.main()
