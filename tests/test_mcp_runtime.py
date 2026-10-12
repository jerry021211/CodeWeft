from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock

from codeagent.mcp import McpRouter, load_mcp_servers
from codeagent.mcp.router import _public_tool_name
from codeagent.mcp.trust import McpTrustStore, request_cli_trust, server_fingerprint
from codeagent.runtime.cancellation import CancellationToken, CancelledError
from codeagent.tools import ToolRegistry


class FakeClient:
    entered = []
    exited = []
    calls = []
    cancelled = threading.Event()
    dispatched = threading.Event()

    def __init__(self, server, **kwargs):
        self.server = server

    async def __aenter__(self):
        self.entered.append(self.server.name)
        if self.server.name == 'broken':
            raise ConnectionError('token=do-not-expose')
        if self.server.name == 'hanging':
            await asyncio.sleep(30)
        return self

    async def __aexit__(self, *args):
        self.exited.append(self.server.name)

    async def list_tools(self, cursor=None):
        schema = {'type': 'object', 'properties': {}, 'additionalProperties': False,
                  'oneOf': [{'required': ['a']}, {'required': ['b']}]}
        return SimpleNamespace(tools=[SimpleNamespace(name='second' if cursor else 'first',
                                   description='description', input_schema=schema)],
                               next_cursor=None if cursor else 'next')

    async def call_tool(self, name, arguments, read_timeout_seconds=None):
        self.calls.append((self.server.name, name, read_timeout_seconds))
        self.dispatched.set()
        if arguments.get('slow'):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        return SimpleNamespace(content=[SimpleNamespace(text='ok')], structured_content={'ok': True}, is_error=False)


class McpRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / 'mcp.json'
        FakeClient.entered, FakeClient.exited, FakeClient.calls = [], [], []
        FakeClient.cancelled.clear()
        FakeClient.dispatched.clear()
        self.clients = patch('codeagent.mcp.router.Client', FakeClient)
        self.clients.start()
        self.transport = patch.object(McpRouter, '_transport', self.fake_transport)
        self.transport.start()
        self.routers = []

    @staticmethod
    async def fake_transport(router, server, stack):
        return server

    def tearDown(self):
        for router in self.routers:
            router.close()
            if router._thread:
                self.assertFalse(router._thread.is_alive(), 'MCP thread leaked')
        self.transport.stop()
        self.clients.stop()
        self.temp.cleanup()

    def router(self, names=('healthy',), *, trusted=True, **config):
        self.config.write_text(json.dumps({'mcpServers': {
            name: {'command': 'fake', **config} for name in names}}), encoding='utf-8')
        router = McpRouter(self.config, trust_checker=(lambda _: True) if trusted else (lambda _: False))
        self.routers.append(router)
        return router

    def test_server_failure_isolated_and_pages_and_schema_preserved(self):
        router = self.router(('healthy', 'broken'))
        registry = ToolRegistry()
        router.register_tools(registry)
        self.assertEqual(router.list_tools(), ['mcp__healthy__first', 'mcp__healthy__second'])
        self.assertEqual(router.states()['broken']['status'], 'error')
        self.assertNotIn('do-not-expose', str(router.states()))
        self.assertNotIn('healthy', FakeClient.exited)
        self.assertFalse(registry.schemas()[0]['input_schema']['additionalProperties'])
        self.assertIn('oneOf', registry.schemas()[0]['input_schema'])
        output = registry.execute('mcp__healthy__first', {})
        self.assertEqual(json.loads(output)['structured_content'], {'ok': True})

    def test_startup_deadline_is_per_server_and_shutdown_is_bounded(self):
        router = self.router(('healthy', 'hanging'), startup_timeout=0.08)
        before = time.monotonic()
        router.start()
        self.assertLess(time.monotonic() - before, 1.5)
        self.assertEqual(router.states()['hanging']['status'], 'error')
        self.assertEqual(router.states()['healthy']['status'], 'connected')
        router.close()
        self.assertIn('healthy', FakeClient.exited)

    def test_untrusted_stdio_and_http_never_create_a_client(self):
        router = self.router(('local', 'remote'), trusted=False)
        router.servers[1] = replace(router.servers[1], transport='http', url='https://example.invalid/mcp')
        router.start()
        self.assertEqual(FakeClient.entered, [])
        self.assertEqual(router.list_tools(), [])
        self.assertTrue(all(s['status'] == 'awaiting_trust' for s in router.states().values()))

    def test_invalid_server_config_does_not_disable_healthy_server(self):
        router = self.router()
        self.config.write_text(json.dumps({'mcpServers': {'bad': {'args': 'wrong'}, 'healthy': {'command': 'fake'}}}))
        router.servers = load_mcp_servers(self.config)
        router.start()
        self.assertEqual(router.states()['bad']['status'], 'error')
        self.assertEqual(len(router.list_tools()), 2)

    def test_call_timeout_cancels_request_without_retry_or_closing_peer(self):
        router = self.router(call_timeout=0.08)
        router.start()
        output = router.call_tool('healthy', 'first', {'slow': True})
        self.assertEqual(output.outcome, 'unknown')
        self.assertFalse(output.retryable)
        self.assertTrue(FakeClient.cancelled.wait(1))
        self.assertEqual(len(FakeClient.calls), 1)
        self.assertEqual(router.call_tool('healthy', 'first', {}).status, 'success')

    def test_remaining_budget_caps_sdk_timeout_and_zero_never_dispatches(self):
        router = self.router(call_timeout=5)
        router.start()
        output = router.call_tool('healthy', 'first', {'slow': True}, remaining_seconds=lambda: .08)
        self.assertEqual(output.outcome, 'unknown')
        self.assertLessEqual(FakeClient.calls[0][2], .08)
        self.assertEqual(router.call_tool('healthy', 'first', {}, remaining_seconds=lambda: 0).status, 'blocked')
        self.assertEqual(len(FakeClient.calls), 1)

    def test_cancellation_and_budgets_are_not_shared_between_registries(self):
        router = self.router()
        first, second = ToolRegistry(), ToolRegistry()
        router.register_tools(first)
        router.register_tools(second)
        token = CancellationToken()
        first.bind_runtime(token.raise_if_cancelled, lambda: 10)
        second.bind_runtime(lambda: None, lambda: 5)
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(first.execute, 'mcp__healthy__first', {'slow': True})
            self.assertTrue(FakeClient.dispatched.wait(1))
            token.cancel('stop only first')
            with self.assertRaisesRegex(CancelledError, 'stop only first') as captured:
                pending.result(timeout=1)
            self.assertEqual(captured.exception.tool_output.outcome, 'unknown')
            self.assertTrue(FakeClient.cancelled.wait(1))
            self.assertEqual(second.execute('mcp__healthy__first', {}).status, 'success')
        before = len(FakeClient.calls)
        with self.assertRaises(CancelledError):
            first.execute('mcp__healthy__first', {})
        self.assertEqual(len(FakeClient.calls), before)

    def test_revoked_trust_blocks_existing_tool_before_dispatch(self):
        router = self.router()
        router.start()
        router._trust_checker = lambda _: False
        self.assertEqual(router.call_tool('healthy', 'first', {}).status, 'blocked')
        self.assertEqual(FakeClient.calls, [])

    def test_retiring_idle_router_closes_transport_and_blocks_stale_adapters(self):
        router = self.router()
        registry = ToolRegistry()
        router.register_tools(registry)
        router.retire()
        router._thread.join(2)
        self.assertFalse(router._thread.is_alive())
        self.assertEqual(FakeClient.exited, ['healthy'])
        output = registry.execute('mcp__healthy__first', {})
        self.assertEqual(output.status, 'blocked')
        self.assertIn('new turn', output)
        self.assertEqual(FakeClient.calls, [])
        with self.assertRaises(RuntimeError):
            router.start()

    def test_retirement_drains_all_accepted_calls_without_blocking_or_replaying(self):
        router = self.router()
        router.start()
        entered = [threading.Event(), threading.Event()]
        release = [threading.Event(), threading.Event()]

        async def controlled_call(client, name, arguments, **kwargs):
            index = arguments['index']
            entered[index].set()
            while not release[index].is_set():
                await asyncio.sleep(.01)
            return SimpleNamespace(content=[SimpleNamespace(text=f'done-{index}')],
                                   structured_content=None, is_error=False)

        with patch.object(FakeClient, 'call_tool', controlled_call), ThreadPoolExecutor(2) as executor:
            calls = [executor.submit(router.call_tool, 'healthy', 'first', {'index': i}) for i in range(2)]
            try:
                self.assertTrue(all(event.wait(2) for event in entered))
                before = time.monotonic()
                router.retire()
                router.retire()
                self.assertLess(time.monotonic() - before, .5)
                self.assertEqual(router.call_tool('healthy', 'first', {}).status, 'blocked')
                release[0].set()
                self.assertEqual(calls[0].result(2), 'done-0')
                self.assertNotIn('healthy', FakeClient.exited)
                release[1].set()
                self.assertEqual(calls[1].result(2), 'done-1')
                router._thread.join(2)
                self.assertFalse(router._thread.is_alive())
                self.assertEqual(FakeClient.exited, ['healthy'])
            finally:
                for event in release:
                    event.set()

    def test_retired_call_still_obeys_timeout_and_cancellation(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                FakeClient.dispatched.clear()
                FakeClient.cancelled.clear()
                router = self.router(call_timeout=.3)
                router.start()
                token = CancellationToken()
                with ThreadPoolExecutor(1) as executor:
                    call = executor.submit(router.call_tool, 'healthy', 'first', {'slow': True},
                                           cancellation_check=token.raise_if_cancelled)
                    self.assertTrue(FakeClient.dispatched.wait(2))
                    router.retire()
                    if cancel:
                        token.cancel()
                        with self.assertRaises(CancelledError):
                            call.result(2)
                    else:
                        self.assertEqual(call.result(2).outcome, 'unknown')
                router._thread.join(2)
                self.assertFalse(router._thread.is_alive())
                self.assertTrue(FakeClient.cancelled.wait(1))

    def test_retire_before_start_never_launches_and_during_start_cleans_up(self):
        router = self.router()
        router.retire()
        with self.assertRaises(RuntimeError):
            router.start()
        self.assertEqual(FakeClient.entered, [])
        router = self.router(('hanging',), startup_timeout=30)
        with ThreadPoolExecutor(1) as executor:
            startup = executor.submit(router.start)
            deadline = time.monotonic() + 2
            while 'hanging' not in FakeClient.entered and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertIn('hanging', FakeClient.entered)
            router.retire()
            with self.assertRaises(RuntimeError):
                startup.result(2)
        router.close()
        self.assertFalse(router._thread.is_alive())

    def test_default_router_denies_launch_even_if_config_claims_trust(self):
        from codeagent.runtime.data_paths import RuntimeDataPaths
        self.config.write_text(json.dumps({'mcpServers': {'demo': {
            'command': 'fake', 'trusted': True, 'approved': True}}}))
        with patch('codeagent.mcp.router.RuntimeDataPaths.default', return_value=RuntimeDataPaths(self.root / 'data')):
            router = McpRouter(self.config)
        self.routers.append(router)
        router.start()
        self.assertEqual(FakeClient.entered, [])
        self.assertEqual(router.states()['demo']['status'], 'awaiting_trust')

    def test_expanded_url_configuration_is_supported(self):
        self.config.write_text(json.dumps({'mcpServers': {'docs': {'url': '${MCP_TEST_URL}'}}}))
        with patch.dict('os.environ', {'MCP_TEST_URL': 'https://example.invalid/mcp'}):
            server = load_mcp_servers(self.config)[0]
            self.assertEqual(server.config_error, '')

    def test_retry_only_reconnects_failed_server_and_does_not_replay_calls(self):
        router = self.router(('healthy', 'broken'))
        router.start()
        initial = FakeClient.entered.count('healthy')
        router.connect_server('broken')
        self.assertEqual(FakeClient.entered.count('healthy'), initial)
        self.assertNotIn('healthy', FakeClient.exited)
        self.assertEqual(FakeClient.calls, [])

    def test_cancelling_startup_waiter_does_not_cancel_other_waiters(self):
        router = self.router(('healthy', 'hanging'), startup_timeout=.2)
        token = CancellationToken()
        with ThreadPoolExecutor(max_workers=2) as executor:
            cancelled = executor.submit(router.start, cancellation_check=token.raise_if_cancelled)
            other = executor.submit(router.list_tools)
            token.cancel()
            with self.assertRaises(CancelledError):
                cancelled.result(timeout=1)
            self.assertEqual(other.result(timeout=2), ['mcp__healthy__first', 'mcp__healthy__second'])

    def test_lossy_names_are_distinct_stable_and_within_provider_limit(self):
        names = [_public_tool_name('demo', tool) for tool in ('find.record', 'find_record', 'x'*80+'a', 'x'*80+'b')]
        self.assertEqual(len(set(names)), 4)
        self.assertTrue(all(len(name) <= 64 for name in names))
        self.assertEqual(names[0], _public_tool_name('demo', 'find.record'))
        self.assertNotEqual(_public_tool_name('a__b', 'c'), _public_tool_name('a', 'b__c'))


class McpTrustTests(unittest.TestCase):
    def test_exact_workspace_config_and_secret_changes_require_new_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            from codeagent.mcp.config import McpServerConfig
            server = McpServerConfig('demo', 'stdio', command='python', env={'TOKEN': 'secret-one'})
            store = McpTrustStore(root / 'outside-project' / 'trust.db')
            self.assertFalse(store.is_trusted(root / 'project', server))
            store.approve(root / 'project', server)
            self.assertTrue(store.is_trusted(root / 'project', server))
            self.assertFalse(store.is_trusted(root / 'other', server))
            for changed in (replace(server, command='other'), replace(server, args=('other.py',)),
                            replace(server, env={'TOKEN': 'secret-two'}), replace(server, cwd=root / 'other')):
                self.assertFalse(store.is_trusted(root / 'project', changed))
            self.assertNotIn(b'secret-one', store.path.read_bytes())
            store.revoke(root / 'project', server.name)
            self.assertFalse(store.is_trusted(root / 'project', server))

    def test_environment_expansion_changes_fingerprint(self):
        from codeagent.mcp.config import McpServerConfig
        server = McpServerConfig('demo', 'http', url='https://example.invalid', headers={'Authorization': '${MCP_TEST_TOKEN}'})
        with patch.dict('os.environ', {'MCP_TEST_TOKEN': 'first'}):
            first = server_fingerprint(server)
        with patch.dict('os.environ', {'MCP_TEST_TOKEN': 'second'}):
            self.assertNotEqual(first, server_fingerprint(server))

    def test_expansions_are_frozen_and_not_expanded_twice(self):
        from codeagent.mcp.config import McpServerConfig
        from codeagent.mcp.trust import resolved_server
        raw = McpServerConfig('demo', 'http', url='https://example.invalid', headers={'Authorization': '${MCP_TEST_TOKEN}'})
        with patch.dict('os.environ', {'MCP_TEST_TOKEN': '${NESTED_VALUE}', 'NESTED_VALUE': 'not-the-token'}):
            frozen = resolved_server(raw)
            self.assertEqual(frozen.headers['Authorization'], '${NESTED_VALUE}')
            self.assertEqual(server_fingerprint(raw), server_fingerprint(frozen))
            self.assertIs(resolved_server(frozen), frozen)

    def test_noninteractive_cli_cannot_implicitly_trust(self):
        from codeagent.mcp.config import McpServerConfig
        server = McpServerConfig('demo', 'stdio', command='python')
        store, broker = Mock(), Mock()
        store.is_trusted.return_value = False
        with patch('builtins.print'):
            request_cli_trust([server], store, Path('.'), broker, interactive=False)
        broker.request.assert_not_called()
        store.approve.assert_not_called()
        broker.request.return_value = True
        request_cli_trust([server], store, Path('.'), broker, interactive=True)
        store.approve.assert_called_once_with(Path('.'), server)
