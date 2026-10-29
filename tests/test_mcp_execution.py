"""Stateful MCP ownership, cleanup and cross-execution isolation."""
import asyncio
import hashlib
import json
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from codeagent.mcp import McpRouter
from codeagent.mcp.config import parse_mcp_server
from codeagent.mcp.trust import resolved_server, server_fingerprint
from codeagent.runtime.cancellation import CancellationToken, CancelledError
from codeagent.tools import ToolRegistry
from tests import test_mcp_runtime as runtime_fixture

FakeClient = runtime_fixture.FakeClient


class ExecutionLifecycleTests(unittest.TestCase):
    setUp = runtime_fixture.McpRuntimeTests.setUp
    tearDown = runtime_fixture.McpRuntimeTests.tearDown
    fake_transport = staticmethod(runtime_fixture.McpRuntimeTests.fake_transport)

    def router(self, **config):
        return runtime_fixture.McpRuntimeTests.router(self, lifecycle='execution', **config)

    def registries(self, router):
        first, second = ToolRegistry(), ToolRegistry()
        router.register_tools(first)
        router.register_tools(second)
        return first, second

    def test_exclusive_for_whole_execution_and_fresh_connection_after_close(self):
        router = self.router()
        first, second = self.registries(router)
        self.assertEqual(first.execute('mcp__healthy__first').status, 'success')
        self.assertEqual(first.execute('mcp__healthy__second').status, 'success')
        self.assertEqual(second.execute('mcp__healthy__first').status, 'blocked')
        second.close_workspace_tools()  # A losing task must not close the owner's browser.
        self.assertEqual(FakeClient.exited, [])
        first.close_workspace_tools()
        self.assertEqual(FakeClient.exited, ['healthy'])
        self.assertEqual(router.states()['healthy']['status'], 'disconnected')
        third = ToolRegistry()
        router.register_tools(third)  # Tool discovery remains available after cleanup.
        self.assertEqual(len(third.schemas()), 2)
        self.assertEqual(third.execute('mcp__healthy__first').status, 'success')
        self.assertEqual(FakeClient.entered, ['healthy', 'healthy'])
        third.close_workspace_tools()

    def test_forked_registry_uses_agent_identity_and_cannot_release_parent(self):
        class Runtime:
            def check(self):
                pass
        router = self.router()
        parent, _ = self.registries(router)
        child = parent.fork_workspace_tools()
        parent_runtime, child_runtime = Runtime(), Runtime()
        parent.bind_runtime(parent_runtime.check, lambda: 5)
        child.bind_runtime(child_runtime.check, lambda: 5)
        self.assertEqual(parent.execute('mcp__healthy__first').status, 'success')
        parent.bind_runtime(parent_runtime.check, lambda: 4)  # Rebinding a bound method preserves identity.
        self.assertEqual(parent.execute('mcp__healthy__second').status, 'success')
        self.assertEqual(child.execute('mcp__healthy__first').status, 'blocked')
        child.close_workspace_tools()
        self.assertEqual(FakeClient.exited, [])
        parent.close_workspace_tools()
        self.assertEqual(child.execute('mcp__healthy__second').status, 'success')
        self.assertEqual(child.execute('mcp__healthy__first').status, 'success')

    def test_timeout_is_unknown_and_blocks_further_calls_until_cleanup(self):
        router = self.router(call_timeout=.08)
        first, second = self.registries(router)
        output = first.execute('mcp__healthy__first', {'slow': True})
        self.assertEqual(output.outcome, 'unknown')
        self.assertFalse(output.retry_safe)
        self.assertEqual(first.execute('mcp__healthy__second').status, 'blocked')
        self.assertEqual(second.execute('mcp__healthy__first').status, 'blocked')
        self.assertEqual(len(FakeClient.calls), 1)
        first.close_workspace_tools()
        self.assertEqual(second.execute('mcp__healthy__first').status, 'success')

    def test_cancellation_reclaims_connection_without_replaying_operation(self):
        router = self.router()
        first, second = self.registries(router)
        token = CancellationToken()
        first.bind_runtime(token.raise_if_cancelled, lambda: 10)
        with ThreadPoolExecutor(1) as executor:
            pending = executor.submit(first.execute, 'mcp__healthy__first', {'slow': True})
            self.assertTrue(FakeClient.dispatched.wait(1))
            token.cancel('stop')
            with self.assertRaises(CancelledError):
                pending.result(timeout=2)
        first.close_workspace_tools()
        self.assertTrue(FakeClient.cancelled.is_set())
        self.assertEqual(second.execute('mcp__healthy__first').status, 'success')
        self.assertEqual(len(FakeClient.calls), 2)

    def test_shared_service_still_works_and_is_not_closed_with_execution(self):
        router = self.router()
        router.servers.append(replace(router.servers[0], name='shared', lifecycle='shared'))
        first, second = self.registries(router)
        first.execute('mcp__healthy__first')
        self.assertEqual(second.execute('mcp__shared__first').status, 'success')
        first.close_workspace_tools()
        self.assertNotIn('shared', FakeClient.exited)
        self.assertEqual(second.execute('mcp__shared__second').status, 'success')

    def test_another_router_cannot_acquire_while_old_transport_is_cleaning(self):
        entered, release = threading.Event(), threading.Event()
        original = FakeClient.__aexit__

        async def delayed_exit(client, *args):
            entered.set()
            while not release.is_set():
                await asyncio.sleep(.01)
            await original(client, *args)

        with patch.object(FakeClient, '__aexit__', delayed_exit), ThreadPoolExecutor(1) as executor:
            router = self.router()
            first, _ = self.registries(router)
            other = McpRouter(self.config, trust_checker=lambda _: True)
            self.routers.append(other)
            second, _ = self.registries(other)
            first.execute('mcp__healthy__first')
            cleanup = executor.submit(first.close_workspace_tools)
            try:
                self.assertTrue(entered.wait(1))
                self.assertEqual(second.execute('mcp__healthy__first').status, 'blocked')
            finally:
                release.set()
            cleanup.result(timeout=2)
        self.assertEqual(second.execute('mcp__healthy__first').status, 'success')

    def test_cleanup_failure_never_hands_uncertain_process_to_next_task(self):
        async def broken_exit(*args):
            raise RuntimeError('private failure details')

        with patch.object(FakeClient, '__aexit__', broken_exit):
            router = self.router()
            first, second = self.registries(router)
            first.execute('mcp__healthy__first')
            first.close_workspace_tools()
        self.assertEqual(second.execute('mcp__healthy__first').status, 'blocked')
        self.assertNotIn('private failure', str(router.states()))
        self.assertEqual(len(FakeClient.calls), 1)

    def test_agent_run_and_yield_use_existing_cleanup_on_success_and_failure(self):
        from codeagent import Agent, AgentConfig
        for method, failure in [('run', False), ('run', True), ('run_until_yield', False)]:
            with self.subTest(method=method, failure=failure):
                router = self.router()
                first, second = self.registries(router)
                agent = Agent(config=AgentConfig(model='test'), client=object(), tools=first)

                def execute(*args, **kwargs):
                    self.assertEqual(agent.tools.execute('mcp__healthy__first').status, 'success')
                    if failure:
                        raise RuntimeError('model failed')
                    return None

                with patch.object(Agent, '_guarded_run', execute):
                    if failure:
                        with self.assertRaisesRegex(RuntimeError, 'model failed'):
                            agent.run('test')
                    else:
                        getattr(agent, method)('test')
                self.assertEqual(second.execute('mcp__healthy__first').status, 'success')
                second.close_workspace_tools()
                router.close()


class ExecutionConfigTests(unittest.TestCase):
    def test_old_shared_approvals_keep_their_fingerprint_and_execution_changes_it(self):
        config = parse_mcp_server('test', {'command': 'python'}, Path('mcp.json'))
        legacy = asdict(resolved_server(config))
        legacy.pop('resolved')
        legacy.pop('lifecycle')
        expected = hashlib.sha256(json.dumps(legacy, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        self.assertEqual(server_fingerprint(config), expected)
        self.assertNotEqual(server_fingerprint(replace(config, lifecycle='execution')), expected)

    def test_remote_or_unknown_lifecycle_rejected(self):
        for raw in [{'url': 'https://example.invalid', 'lifecycle': 'execution'},
                    {'command': 'python', 'lifecycle': 'wrong'}]:
            with self.assertRaises(ValueError):
                parse_mcp_server('test', raw, Path('mcp.json'))


class ExecutionProcessTests(unittest.TestCase):
    def test_real_stdio_state_is_reset_between_tasks_and_close_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'server.py'
            script.write_text('''from mcp.server import MCPServer
server = MCPServer("state-test")
counter = 0
@server.tool()
def increment() -> str:
    global counter
    counter += 1
    return str(counter)
server.run()
''', encoding='utf-8')
            config = root / 'mcp.json'
            config.write_text(json.dumps({'mcpServers': {'state': {
                'command': sys.executable, 'args': [str(script)], 'lifecycle': 'execution'}}}))
            router = McpRouter(config, trust_checker=lambda _: True)
            try:
                first, second = ToolRegistry(), ToolRegistry()
                router.register_tools(first)
                router.register_tools(second)
                self.assertEqual(json.loads(first.execute('mcp__state__increment'))['structured_content']['result'], '1')
                self.assertEqual(json.loads(first.execute('mcp__state__increment'))['structured_content']['result'], '2')
                self.assertEqual(second.execute('mcp__state__increment').status, 'blocked')
                first.close_workspace_tools()
                first.close_workspace_tools()
                self.assertEqual(json.loads(second.execute('mcp__state__increment'))['structured_content']['result'], '1')
                second.close_workspace_tools()
            finally:
                router.close()
            self.assertFalse(router._thread.is_alive())
