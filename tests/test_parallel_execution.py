"""Concurrency contracts: synchronization proves overlap without timing guesses."""
from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from codeagent import Agent, AgentConfig, ToolDefinition, ToolRegistry, ModelResponse, HookManager
from codeagent.context import ContextConfig, ContextManager
from codeagent.events import EventEmitter, CallbackEventSink, TokenUsage, RunEvent
from codeagent.hooks.loop_guard import LoopGuardConfig
from codeagent.messages import validate_tool_history
from codeagent.permissions.broker import WaitingPermissionBroker, permission_execution
from codeagent.runtime import CancellationToken, CancelledError
from codeagent.runtime.activity import ExecutionActivity
from codeagent.runtime.execution import RunBudget
from codeagent.runtime.parallel import Admission, ParallelConfig
from codeagent.web.storage import SQLiteRepository, RecordNotFoundError


def final(text="done"):
    return ModelResponse("end_turn", [{"type": "text", "text": text}])


def batch(*calls):
    return ModelResponse("tool_use", [dict(type="tool_use", id=f"t{i}", name=name, input=args)
                                      for i, (name, args) in enumerate(calls)])


class Client:
    def __init__(self, responses=(), child=None):
        self.responses = list(responses)
        self.child = child
        self.calls = []

    def create_message(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        return response(kwargs) if callable(response) else response

    def fork(self, **kwargs):
        return self.child(kwargs) if self.child else self


class ParallelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.events = []

    def agent(self, client, tools=None, **kwargs):
        config = kwargs.pop("config", AgentConfig("fake"))
        return Agent(client=client, config=config, tools=tools or ToolRegistry(),
            context=ContextManager(config=ContextConfig(mode="off", transcript_dir=self.root / "history",
                tool_output_dir=self.root / "outputs")),
            event_emitter=EventEmitter(CallbackEventSink(self.events.append)), **kwargs)

    def reader(self, callback):
        tools = ToolRegistry()
        tools.register_handler(ToolDefinition("probe", "", {"type": "object"}, effect="read", reentrant=True), callback)
        return tools

    def test_readers_overlap_and_history_retains_request_order(self):
        barrier = threading.Barrier(2)
        def read(number):
            barrier.wait(timeout=3)
            return str(number)
        agent = self.agent(Client([batch(("probe", {"number": 1}), ("probe", {"number": 2})), final()]), self.reader(read))
        result = agent.run("read both")
        validate_tool_history(result.messages)
        outputs = next(m["content"] for m in result.messages if isinstance(m["content"], list) and
                       m["content"] and m["content"][0].get("type") == "tool_result")
        self.assertEqual([r["content"] for r in outputs], ["1", "2"])
        self.assertTrue(all(not r.get("is_error") for r in outputs))

    def test_exclusive_barrier_preserves_read_write_read_order(self):
        barrier = threading.Barrier(2)
        completed = set()
        def read(number):
            if number < 2:
                barrier.wait(timeout=3)
                completed.add(number)
            else:
                self.assertIn("write", completed)
            return "ok"
        tools = self.reader(read)
        def write():
            self.assertEqual(completed, {0, 1})
            completed.add("write")
            return "saved"
        tools.register_handler(ToolDefinition("write", "", {}), write)
        agent = self.agent(Client([batch(("probe", {"number": 0}), ("probe", {"number": 1}),
            ("write", {}), ("probe", {"number": 2})), final()]), tools)
        self.assertEqual(agent.run("go").final_text, "done")

    def test_disable_parallel_and_unknown_tools_remain_serial(self):
        calls = []
        tools = self.reader(lambda number: calls.append(number) or "ok")
        agent = self.agent(Client([batch(("probe", {"number": 1}), ("probe", {"number": 2})), final()]), tools,
                           config=AgentConfig("fake", parallel=ParallelConfig(enabled=False)))
        agent.run("go")
        self.assertEqual(calls, [1, 2])
        ordinary = ToolRegistry()
        ordinary.register_handler(ToolDefinition("custom", "", {}), lambda: "ok")
        self.assertFalse(ordinary.parallel_safe("custom"))

    def test_parallel_child_contexts_tools_and_usage_are_isolated(self):
        barrier = threading.Barrier(2)
        trackers = []
        def child(options):
            trackers.append(options["usage_tracker"])
            def response(params):
                barrier.wait(timeout=3)
                names = {item["name"] for item in params["tools"]}
                self.assertNotIn("write", names)
                self.assertNotIn("subagent", names)
                self.assertNotIn("subagent_cancel", names)
                options["usage_tracker"].record(TokenUsage(input_tokens=7))
                return final(params["messages"][-1]["content"])
            return Client([response])
        tools = self.reader(lambda: "ok")
        tools.register_handler(ToolDefinition("write", "", {}), lambda: "saved")
        agent = self.agent(Client([batch(("subagent", {"description": "A", "access": "read_only"}),
            ("subagent", {"description": "B", "access": "read_only"})), final()], child), tools)
        result = agent.run("parallel research")
        self.assertEqual(result.final_text, "done")
        self.assertEqual([t.snapshot().input_tokens for t in trackers], [7, 7])
        self.assertEqual(agent.usage_tracker.snapshot().input_tokens, 14)
        self.assertEqual(len(list((self.root / "outputs" / "subagent-results").glob("*.txt"))), 2)

    def test_background_continues_parent_and_notifies_once_before_final(self):
        release = threading.Event()
        child_entered = threading.Event()
        def child(options):
            def response(_):
                child_entered.set()
                if not release.wait(3):
                    raise AssertionError("parent did not continue")
                return final("child report")
            return Client([response])
        def parent_continues(_):
            self.assertTrue(child_entered.wait(3))
            release.set()
            return final("provisional")
        agent = self.agent(Client([batch(("subagent", {"description": "research", "access": "read_only", "run_in_background": True})),
                                   parent_continues, final("integrated")], child))
        result = agent.run("go")
        self.assertEqual(result.final_text, "integrated")
        validate_tool_history(result.messages)
        notices = [m for m in result.messages if isinstance(m["content"], str) and m["content"].startswith("[自动子任务完成通知")]
        self.assertEqual(len(notices), 1)
        self.assertIn("child report", notices[0]["content"])
        self.assertEqual(len([e for e in self.events if e.type == "subagent.delivered"]), 1)

    def test_single_child_cancellation_keeps_sibling_and_parent_alive(self):
        release = threading.Event()
        entered = threading.Event()
        def child(options):
            def response(_):
                entered.set()
                release.wait(3)
                return final("child")
            return Client([response])
        agent = self.agent(Client(child=child))
        first = agent._start_readonly_subagent("one", background=True)
        second = agent._start_readonly_subagent("two", background=True)
        self.assertTrue(entered.wait(3))
        agent._subagent_runtime.cancel(first.id)
        self.assertFalse(agent.cancellation.is_cancelled)
        self.assertFalse(second.token.is_cancelled)
        release.set()
        agent._subagent_runtime.wait(first)
        agent._subagent_runtime.wait(second)
        self.assertEqual(first.status, "cancelled")
        self.assertEqual(second.status, "completed")
        agent._subagent_runtime.close()

    def test_local_and_global_capacity_and_bounded_queue(self):
        release = threading.Event()
        entered = threading.Event()
        active = []
        def child(options):
            def response(_):
                active.append(1)
                entered.set()
                release.wait(3)
                return final()
            return Client([response])
        agent = self.agent(Client(child=child), config=AgentConfig("fake",
            parallel=ParallelConfig(max_subagents=1, max_pending_subagents=2)))
        first = agent._start_readonly_subagent("one")
        self.assertTrue(entered.wait(3))
        second = agent._start_readonly_subagent("two")
        self.assertEqual(second.status, "queued")
        with self.assertRaisesRegex(ValueError, "queue is full"):
            agent._start_readonly_subagent("three")
        self.assertEqual(len(active), 1)
        release.set()
        agent._subagent_runtime.wait(first)
        agent._subagent_runtime.wait(second)
        agent._subagent_runtime.close()

    def test_background_writer_is_rejected(self):
        agent = self.agent(Client())
        result = agent._spawn_subagent("write files", run_in_background=True)
        self.assertTrue(result.startswith("Error:"))
        self.assertIsNone(agent._subagent_runtime)

    def test_custom_sdk_hooks_require_a_factory_for_background(self):
        hooks = HookManager()
        hooks.register("PreToolUse", lambda tool: None)
        agent = self.agent(Client(child=lambda _: Client([final()])), hooks=hooks)
        result = agent._spawn_subagent("read", access="read_only", run_in_background=True)
        self.assertIn("independent subagent_environment_factory", result)
        self.assertIsNone(agent._subagent_runtime)

    def test_event_failure_during_close_still_interrupts_and_drains_child(self):
        entered, release = threading.Event(), threading.Event()
        def child(options):
            def response(_):
                entered.set()
                release.wait(5)
                return final()
            return Client([response])
        class Sink:
            durable = True
            def emit(self, event):
                if event.type == "subagent.cancelling":
                    raise OSError("journal unavailable")
        agent = self.agent(Client(child=child))
        agent.event_emitter = EventEmitter(Sink())
        with patch.object(ExecutionActivity, "interrupt_request", side_effect=release.set):
            record = agent._start_readonly_subagent("one")
            self.assertTrue(entered.wait(3))
            with self.assertRaisesRegex(RuntimeError, "persist run event"):
                agent._subagent_runtime.close()
            self.assertTrue(release.is_set())
            self.assertTrue(record.future.done())

    def test_process_admission_cancelled_waiter_does_not_leak_slot(self):
        gate = Admission(1)
        token = CancellationToken()
        entered = threading.Event()
        errors = []
        def waiter():
            try:
                entered.set()
                with gate.enter(token.raise_if_cancelled):
                    raise AssertionError("cancelled waiter entered")
            except BaseException as exc:
                errors.append(exc)
        with gate.enter():
            worker = threading.Thread(target=waiter)
            worker.start()
            self.assertTrue(entered.wait(3))
            token.cancel()
            worker.join(3)
            self.assertFalse(worker.is_alive())
        self.assertIsInstance(errors[0], CancelledError)
        with gate.enter():
            pass

    def test_queued_child_cancel_never_calls_its_model(self):
        release, entered = threading.Event(), threading.Event()
        count = []
        def child(options):
            def response(_):
                count.append(1)
                entered.set()
                release.wait(3)
                return final()
            return Client([response])
        agent = self.agent(Client(child=child), config=AgentConfig("fake", parallel=ParallelConfig(max_subagents=1)))
        first = agent._start_readonly_subagent("one")
        self.assertTrue(entered.wait(3))
        queued = agent._start_readonly_subagent("two")
        agent._subagent_runtime.cancel(queued.id)
        release.set()
        agent._subagent_runtime.wait(first)
        agent._subagent_runtime.wait(queued)
        self.assertEqual(count, [1])
        self.assertEqual(queued.status, "cancelled")
        agent._subagent_runtime.close()

    def test_large_child_report_is_bounded_but_full_artifact_survives(self):
        report = "完整报告" * 5000
        agent = self.agent(Client(child=lambda _: Client([final(report)])))
        record = agent._start_readonly_subagent("research")
        agent._subagent_runtime.wait(record)
        self.assertEqual(len(record.snapshot()["result"]), 4000)
        self.assertEqual(Path(record.output_handle).read_text(encoding="utf-8"), report)
        self.assertTrue(all("subagent_id" in e.payload for e in self.events if e.type.startswith("subagent.")))
        agent._subagent_runtime.close()

    def test_parent_cancel_interrupts_foreground_children_before_join(self):
        entered, release = threading.Event(), threading.Event()
        errors = []
        def child(options):
            def response(_):
                entered.set()
                release.wait(5)
                return final()
            return Client([response])
        agent = self.agent(Client([batch(("subagent", {"description": "one", "access": "read_only"}))], child))
        def execute():
            try:
                agent.run("go")
            except BaseException as exc:
                errors.append(exc)
        with patch.object(ExecutionActivity, "interrupt_request", side_effect=release.set):
            worker = threading.Thread(target=execute)
            worker.start()
            self.assertTrue(entered.wait(3))
            agent.cancellation.cancel()
            worker.join(3)
            release.set()
            self.assertFalse(worker.is_alive())
        self.assertIsInstance(errors[0], CancelledError)
        validate_tool_history(agent.messages)

    def test_parallel_failure_and_post_hook_error_preserve_sibling_result(self):
        barrier = threading.Barrier(2)
        def read(number):
            barrier.wait(3)
            if number == 0:
                raise ValueError("cannot read")
            return "sibling saved"
        hooks = HookManager()
        def after(tool, output):
            if tool.input["number"] == 0:
                raise RuntimeError("post hook failed")
        hooks.register("PostToolUse", after)
        agent = self.agent(Client([batch(("probe", {"number": 0}), ("probe", {"number": 1}))]),
                           self.reader(read), hooks=hooks)
        with self.assertRaisesRegex(RuntimeError, "post hook failed"):
            agent.run("read")
        results = agent.messages[-1]["content"]
        self.assertIn("cannot read", results[0]["content"])
        self.assertEqual(results[1]["content"], "sibling saved")
        self.assertFalse(results[1].get("is_error"))
        validate_tool_history(agent.messages)

    def test_readonly_scope_rejects_unknown_child_ids(self):
        agent = self.agent(Client(child=lambda _: Client([final()])))
        record = agent._start_readonly_subagent("one")
        with self.assertRaises(ValueError):
            agent._subagent_runtime.result("another-run")
        agent._subagent_runtime.wait(record)
        agent._subagent_runtime.close()

    def test_budget_pause_is_per_execution_branch(self):
        now = [0.0]
        budget = RunBudget(LoopGuardConfig(), clock=lambda: now[0])
        active, release = threading.Event(), threading.Event()
        def sibling():
            with budget.running():
                active.set()
                release.wait(3)
        with budget.running():
            with budget.paused():
                worker = threading.Thread(target=sibling)
                worker.start()
                self.assertTrue(active.wait(3))
                now[0] = 5
                self.assertEqual(budget.snapshot()["active_seconds"], 5)
                release.set()
                worker.join(3)
                now[0] = 10
                self.assertEqual(budget.snapshot()["active_seconds"], 5)

    def test_permission_binding_uses_child_identity_and_cancellation(self):
        parent, child = CancellationToken(), CancellationToken()
        broker = WaitingPermissionBroker(on_request=lambda request: child.cancel())
        with permission_execution(ExecutionActivity(child), child, "child"):
            with self.assertRaises(CancelledError):
                broker.request("probe", {}, "check", cancellation=parent)
        self.assertFalse(parent.is_cancelled)
        self.assertFalse(broker.pending)

    def test_subagent_persistence_is_atomic_scoped_and_restart_safe(self):
        path = self.root / "state.db"
        repo = SQLiteRepository(path, recover_incomplete=False)
        conv = repo.create_conversation(title="test")
        run = repo.create_run(conv.id)
        event = RunEvent(type="subagent.queued", run_id=run.id, agent_id="child",
            payload={"subagent_id": "child", "status": "queued", "description": "test"})
        repo.append_event(event)
        repo.append_event(event)
        self.assertEqual(len(repo.list_subagent_runs(run.id)), 1)
        with self.assertRaises(RecordNotFoundError):
            repo.get_subagent_run("other-run", "child")
        repo.close()
        repo = SQLiteRepository(path)
        self.addCleanup(repo.close)
        self.assertEqual(repo.get_subagent_run(run.id, "child")["status"], "interrupted")
        events = repo.list_events(run.id)
        self.assertEqual(len({e.seq for e in events}), len(events))
        self.assertTrue(any(e.type == "subagent.interrupted" for e in events))

    def test_web_background_result_cancel_and_parent_join(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError:
            self.skipTest("Web dependencies unavailable")
        from types import SimpleNamespace
        from codeagent.web.api import create_app
        from codeagent.web.scheduler import RunScheduler
        release, entered = threading.Event(), threading.Event()
        root = self.root
        def child(options):
            def response(_):
                entered.set()
                release.wait(5)
                return final("finished child")
            return Client([response])
        class Factory:
            def for_workspace(self, workspace):
                return self
            def create(self, *, event_emitter, cancellation, permission_broker, **kwargs):
                return Agent(client=Client([
                    batch(("subagent", {"description": "investigate", "access": "read_only", "run_in_background": True})),
                    final("provisional"), final("integrated")], child),
                    tools=ToolRegistry(), config=AgentConfig("fake"),
                    event_emitter=event_emitter, cancellation=cancellation, permission_broker=permission_broker,
                    context=ContextManager(config=ContextConfig(mode="off", transcript_dir=root / "web-history",
                        tool_output_dir=root / "web-outputs")))
        repo = SQLiteRepository(root / "web.db", recover_incomplete=False)
        self.addCleanup(repo.close)
        scheduler = RunScheduler(repo, Factory())
        app = create_app(repository=repo, scheduler=scheduler, workspace=root,
            env=SimpleNamespace(model_id="fake", max_tokens=8000, max_iterations=50), static_dir=root / "no-dist")
        self.addCleanup(release.set)
        with TestClient(app) as client:
            client.get("/api/health")
            conv = repo.create_conversation(title="test", workspace=str(root))
            run = scheduler.submit(conv.id, "research")
            self.assertTrue(entered.wait(3))
            response = client.get(f"/api/runs/{run.id}/subagents")
            self.assertEqual(response.status_code, 200)
            record = response.json()[0]
            child_id = record["subagent_id"]
            self.assertEqual(record["status"], "running")
            self.assertEqual(client.get(f"/api/runs/wrong/subagents/{child_id}/result").status_code, 404)
            cancelled = client.post(f"/api/runs/{run.id}/subagents/{child_id}/cancel")
            self.assertEqual(cancelled.status_code, 200)
            self.assertEqual(cancelled.json()["status"], "cancelling")
            self.assertEqual(repo.get_run(run.id).status, "running")
            release.set()
            deadline = time.monotonic() + 5
            while repo.get_run(run.id).status in {"queued", "running"} and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(repo.get_run(run.id).status, "completed")
            result = client.get(f"/api/runs/{run.id}/subagents/{child_id}/result").json()
            self.assertEqual(result["status"], "cancelled")
            self.assertIn("Subagent cancelled", result["output"])
            self.assertTrue(result["delivered"])
            events = repo.list_events(run.id)
            self.assertLess(next(e.seq for e in events if e.type == "subagent.cancelled"),
                            next(e.seq for e in events if e.type == "run.completed"))


if __name__ == "__main__":
    unittest.main()
