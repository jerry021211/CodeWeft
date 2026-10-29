import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from codeagent.mcp.approval import browser_read_allowed
from codeagent.mcp.config import McpServerConfig
from codeagent.mcp.router import McpRouter, McpTool
from codeagent.permissions.policy import PermissionPolicy
from codeagent.multiagent.context import merge_context
from codeagent.web.scheduler import RunScheduler
from codeagent.web.storage import SQLiteRepository, StorageConflictError
from tests.test_web_scheduler import _FakeFactory
from tests import test_web_api as fixture
from tests import test_native_adapter as native_fixture
from tests import test_message_actions as message_fixture
from tests import test_plan_mode as plan_fixture


class ApprovalPolicyTests(unittest.TestCase):
    def test_modes_and_hard_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            for mode in (None, "ask", "auto", "full"):
                ask = Mock(return_value=True)
                policy = PermissionPolicy(workspace=Path(directory), approval_mode=mode, ask=ask,
                                          mcp_read_only=lambda name, args: name == "mcp__known__snapshot" and not args)
                self.assertTrue(policy.check("mcp__known__snapshot", {}).allowed)
                self.assertEqual(ask.call_count, int(mode in {None, "ask"}))
                ask.reset_mock()
                self.assertTrue(policy.check("mcp__known__click", {"ref": "pay"}).allowed)
                self.assertEqual(ask.call_count, int(mode != "full"))
                ask.reset_mock()
                self.assertFalse(policy.check("bash", {"command": "rm -rf /"}).allowed)
                self.assertFalse(policy.check("edit_file", {"file_path": "../outside.txt"}).allowed)
                ask.assert_not_called()
                self.assertTrue(policy.check("write_file", {"file_path": "inside.txt"}).allowed)
            with self.assertRaises(ValueError):
                PermissionPolicy(approval_mode="typo")

    def test_unknown_commands_and_decision_logs(self):
        ask, log = Mock(return_value=False), Mock()
        policy = PermissionPolicy(approval_mode="auto", ask=ask, on_decision=log)
        self.assertTrue(policy.check("bash", {"command": "git status --short"}).allowed)
        ask.assert_not_called()
        for command in ("python arbitrary.py", "git status; upload secrets", "npm test", "Remove-Item source.py"):
            self.assertFalse(policy.check("bash", {"command": command}).allowed)
        self.assertFalse(policy.check("mcp__other__read", {"password": "secret-value"}).allowed)
        self.assertNotIn("secret-value", str(log.call_args_list))
        self.assertTrue(PermissionPolicy(ask=ask).check("bash", {"command": "npm test"}).allowed)
        ask.reset_mock()
        self.assertFalse(PermissionPolicy(approval_mode="ask", ask=ask).check("web_search", {"query": "hello"}).allowed)

    def test_browser_adapter_matches_configuration_and_parameters(self):
        server = McpServerConfig(name="any-alias", transport="stdio", command="npx",
            args=("-y", "@playwright/mcp@0.0.83", "--browser", "chrome", "--isolated"))
        self.assertTrue(browser_read_allowed(server, "browser_snapshot", {}))
        self.assertTrue(browser_read_allowed(server, "browser_tabs", {"action": "list"}))
        self.assertTrue(browser_read_allowed(server, "browser_wait_for", {"text": "loaded", "time": 1}))
        self.assertTrue(browser_read_allowed(server, "browser_navigate", {"url": "https://example.com/"}))
        for url in ("file:///secret", "javascript:alert(1)", "https://x/logout", "https://x/?token=secret",
                    "https://user:password@x/", "https://x/#delete", "https://x/\n"):
            self.assertFalse(browser_read_allowed(server, "browser_navigate", {"url": url}), url)
        for name, args in (("browser_click", {"element": "safe link", "ref": "x"}),
                           ("browser_tabs", {"action": "close"}), ("browser_snapshot", {"filename": "../secret"}),
                           ("browser_wait_for", {"unexpected": True})):
            self.assertFalse(browser_read_allowed(server, name, args))
        for changed in (replace(server, command="python"), replace(server, command="D:/fake/npx.cmd"),
                        replace(server, cwd=Path("custom")), replace(server, env={"NODE_OPTIONS": "--require evil"}),
                        replace(server, args=("@playwright/mcp@latest",)),
                        replace(server, args=server.args + ("--init-script", "evil.js"))):
            self.assertFalse(browser_read_allowed(changed, "browser_snapshot", {}))

    def test_router_requires_actual_binding_current_trust_and_live_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mcp.json"
            path.write_text(json.dumps({"mcpServers": {"renamed": {"command": "npx", "args": [
                "-y", "@playwright/mcp@0.0.83", "--browser", "chrome", "--isolated"]}}}), encoding="utf-8")
            trust = Mock(return_value=True)
            router = McpRouter(path, trust_checker=trust)
            tool = McpTool(router, "renamed", SimpleNamespace(name="browser_snapshot", description="", input_schema={}))
            router._tools.append(tool)
            self.assertTrue(router.read_only_approval(tool.definition.name, {}))
            ask = Mock(return_value=False)
            policy = PermissionPolicy(approval_mode="auto", ask=ask, mcp_read_only=router.read_only_approval)
            for arguments in ({"depth": 10}, {"depth": 12}, {"depth": 14},
                              {"target": 'iframe[id^="login"]', "depth": 10},
                              {"boxes": True}, {"target": "e1", "depth": 3, "boxes": False}):
                with self.subTest(arguments=arguments):
                    self.assertTrue(policy.check(tool.definition.name, arguments).allowed)
            ask.assert_not_called()
            for arguments in ({"filename": "snapshot.md", "depth": 10}, {"depth": "10"},
                              {"depth": True}, {"depth": float("inf")}, {"depth": -1},
                              {"target": {}}, {"boxes": "true"}, {"unknown": True}):
                with self.subTest(arguments=arguments):
                    self.assertFalse(policy.check(tool.definition.name, arguments).allowed)
            policy.approval_mode = "ask"
            self.assertFalse(policy.check(tool.definition.name, {"depth": 10}).allowed)
            self.assertFalse(router.read_only_approval("mcp__playwright__browser_snapshot", {}))
            trust.return_value = False
            self.assertFalse(router.read_only_approval(tool.definition.name, {}))
            trust.return_value = True
            router._retired = True
            self.assertFalse(router.read_only_approval(tool.definition.name, {}))
            router.close()

    def test_collaboration_resume_cannot_upgrade_original_mode(self):
        new = merge_context({}, {"approval_mode": "auto"})
        self.assertEqual(new["approval_mode"], "auto")
        self.assertEqual(merge_context(new, {"approval_mode": "full"})["approval_mode"], "auto")
        self.assertIsNone(merge_context({"requests": []}, {"approval_mode": "full"})["approval_mode"])


class ApprovalPersistenceTests(unittest.TestCase):
    def test_mode_persists_and_active_run_cannot_change_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "db.sqlite"
            repo = SQLiteRepository(path, recover_incomplete=False)
            conversation = repo.create_conversation(workspace=directory)
            self.assertEqual(conversation.approval_mode, "auto")
            repo.update_conversation(conversation.id, approval_mode="full")
            repo.close()
            repo = SQLiteRepository(path, recover_incomplete=False)
            self.assertEqual(repo.get_conversation(conversation.id).approval_mode, "full")
            self.assertEqual(repo.create_conversation(workspace=directory).approval_mode, "auto")
            repo.create_run(conversation.id)
            with self.assertRaises(StorageConflictError):
                repo.update_conversation(conversation.id, approval_mode="ask")
            repo.close()

    def test_scheduler_pins_mode_without_changing_legacy_submissions(self):
        class Factory(_FakeFactory):
            modes = []

            def create(self, *, approval_mode=None, **kwargs):
                self.modes.append(approval_mode)
                return super().create(**kwargs)

        with tempfile.TemporaryDirectory() as directory:
            repo = SQLiteRepository(Path(directory) / "db", recover_incomplete=False)
            factory = Factory()
            scheduler = RunScheduler(repo, factory)
            try:
                for mode in ("auto", "full", None):
                    conv = repo.create_conversation(workspace=directory)
                    run = scheduler.submit(conv.id, "hello", approval_mode=mode)
                    deadline = time.monotonic() + 5
                    while repo.get_run(run.id).status in {"queued", "running"} and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertEqual(repo.get_run(run.id).status, "completed")
                    self.assertEqual(repo.get_run(run.id).metadata["approval_mode"], mode)
                    self.assertEqual(repo.list_messages(conv.id)[0].metadata["approval_mode"], mode)
                self.assertEqual(factory.modes, ["auto", "full", None])
            finally:
                scheduler.stop()
                repo.close()

    def test_schema_upgrade_keeps_old_conversations_and_new_default(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.db"
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT NOT NULL, "
                                   "workspace TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, archived_at TEXT)")
                connection.execute("INSERT INTO conversations VALUES ('old','History',?,'2026-01-01','2026-01-01',NULL)", (directory,))
            connection.close()
            repo = SQLiteRepository(path, recover_incomplete=False)
            try:
                self.assertEqual(repo.get_conversation("old").approval_mode, "auto")
                self.assertEqual(repo.get_conversation("old").title, "History")
            finally:
                repo.close()


class ApprovalPlanTests(unittest.TestCase):
    setUp = plan_fixture.PlanModeTests.setUp
    tearDown = plan_fixture.PlanModeTests.tearDown
    plan = plan_fixture.PlanModeTests.plan

    def test_approved_plan_uses_source_run_mode_and_keeps_plan_guard(self):
        from codeagent.config import EnvironmentConfig
        self.repo.update_run_status(self.run.id, "completed", metadata={"approval_mode": "auto"})
        # A later conversation preference must not silently widen this plan.
        self.repo.update_conversation(self.conv.id, approval_mode="full")
        plan = self.plan()
        with patch.object(EnvironmentConfig, "create_model_client", return_value=plan_fixture.Client([])):
            result = self.scheduler.decide_plan(plan["id"], plan["content_hash"], "approve")
            self.assertEqual(result["status"], "started", result.get("error"))
            self.assertEqual(self.repo.get_run(result["execution_run_id"]).metadata["approval_mode"], "auto")
            self.scheduler.stop()

    def test_full_mode_does_not_enable_write_tools_during_planning(self):
        from codeagent.config import EnvironmentConfig
        from codeagent.events import EventEmitter, ExecutionContext
        from codeagent.permissions import WaitingPermissionBroker
        from codeagent.runtime import CancellationToken
        with patch.object(EnvironmentConfig, "create_model_client", return_value=plan_fixture.Client([])):
            agent = self.factory.create(event_emitter=EventEmitter(context=ExecutionContext(
                conversation_id=self.conv.id, run_id=self.run.id)), cancellation=CancellationToken(),
                permission_broker=WaitingPermissionBroker(), plan_mode=True, approval_mode="full")
        self.assertNotIn("write_file", agent.tools)
        self.assertNotIn("bash", agent.tools)
        self.assertEqual(agent.hooks.permission_snapshot()[0]["approval_mode"], "full")


class ApprovalBranchTests(unittest.TestCase):
    setUp = message_fixture.MessageActionTests.setUp
    tearDown = message_fixture.MessageActionTests.tearDown
    turn = message_fixture.MessageActionTests.turn

    def test_branch_defaults_auto_but_regeneration_keeps_original_mode(self):
        _, _, reply = self.turn("question", "answer", approval_mode="full")
        self.repo.update_conversation(self.source.id, approval_mode="ask")
        branch, _ = self.repo.fork_conversation(self.source.id, reply.id)
        self.assertEqual(branch.approval_mode, "auto")
        copied = self.repo.list_messages(branch.id)[-1]
        self.assertEqual(self.repo.get_run(copied.run_id).metadata["approval_mode"], "full")
        retry_branch, retry = self.repo.fork_conversation(branch.id, copied.id, regenerate=True)
        self.assertEqual(retry["approvalMode"], "full")
        self.assertEqual(retry_branch.approval_mode, "full")
        self.assertEqual(self.repo.get_conversation(self.source.id).approval_mode, "ask")


class ApprovalNativeInheritanceTests(unittest.TestCase):
    setUp = native_fixture.NativeAdapterTests.setUp
    tearDown = native_fixture.NativeAdapterTests.tearDown
    create_task = native_fixture.NativeAdapterTests.create_task
    agent = native_fixture.NativeAdapterTests.agent

    def test_native_worker_inherits_mode_and_keeps_read_only_guard(self):
        from codeagent.config import EnvironmentConfig
        from codeagent.web.factory import WebAgentFactory
        key = self.create_task()
        root = native_fixture.Client(native_fixture.tool_response("delegate_task", {"taskId": key}), native_fixture.text_response())
        child = native_fixture.Client(native_fixture.text_response())
        original = WebAgentFactory.create
        snapshots = []

        def create(factory, **kwargs):
            agent = original(factory, **kwargs)
            snapshots.append((kwargs.get("native_worker"), agent.read_only, agent.hooks.permission_snapshot()))
            return agent

        with patch.object(EnvironmentConfig, "create_model_client", side_effect=[root, child]), patch.object(WebAgentFactory, "create", new=create):
            self.agent(native_collaboration=True, approval_mode="full").run("Investigate using a worker")
        worker = next(item for item in snapshots if item[0])
        self.assertTrue(worker[1])
        self.assertEqual(worker[2][0]["approval_mode"], "full")

    def test_verification_commands_honor_mode(self):
        from codeagent.multiagent.verification import run_checks, CheckFailed
        from codeagent.runtime import CancellationToken
        from codeagent.permissions import CliPermissionBroker
        ask = Mock(return_value=False)
        broker = CliPermissionBroker(prompt=ask)
        with self.assertRaises(CheckFailed):
            run_checks(self.source, ["echo verify"], CancellationToken(), broker, lambda item: None, approval_mode="auto")
        ask.assert_called_once()
        ask.reset_mock()
        self.assertEqual(run_checks(self.source, ["echo verify"], CancellationToken(), broker, lambda item: None, approval_mode="full")[0]["status"], "passed")
        ask.assert_not_called()


@unittest.skipIf(fixture.TestClient is None, "FastAPI is not installed")
class ApprovalApiTests(unittest.TestCase):
    setUp = fixture.WebApiTests.setUp
    tearDown = fixture.WebApiTests.tearDown

    def test_saved_mode_roundtrip_request_validation_and_legacy_omission(self):
        conv = self.repository.create_conversation(workspace=self.workspace)
        path = f"/api/conversations/{conv.id}"
        self.assertEqual(self.client.get(path).json()["approval_mode"], "auto")
        result = self.client.patch(path, json={"approval_mode": "full"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.client.get(path).json()["approval_mode"], "full")
        self.assertEqual(self.client.patch(path, json={"approval_mode": "invalid"}).status_code, 422)
        self.assertEqual(self.client.post(path + "/runs", json={"content": "hello", "approvalMode": "invalid"}).status_code, 422)
        seen = []

        def submit(conversation_id, content, *, approval_mode=None, **kwargs):
            seen.append(approval_mode)
            return self.repository.create_run(conversation_id, status="completed")

        self.scheduler.submit = submit
        for body in ({"approvalMode": "auto"}, {"approvalMode": "full"}, {}):
            response = self.client.post(path + "/runs", json={"content": "hello", **body})
            self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(seen, ["auto", "full", None])
