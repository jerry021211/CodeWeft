"""Browser ownership contracts, using the real router and an in-memory MCP peer."""
import asyncio
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codeagent.mcp.browser import BrowserSessions
from codeagent.mcp.config import McpServerConfig
from codeagent.mcp.router import McpRouter
from codeagent.runtime.cancellation import CancellationToken, CancelledError
from codeagent.runtime.execution import ExecutionStopped
from codeagent.tools import ToolRegistry
from tests import test_web_api as api_fixture


class BrowserPeer:
    peers = []
    entered = threading.Event()
    finish = threading.Event()

    def __init__(self, server, **kwargs):
        self.server = server
        self.url = 'about:blank'
        self.title = 'Fresh'
        self.calls = []
        self.closed = False
        self.peers.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def list_tools(self, cursor=None):
        return SimpleNamespace(next_cursor=None, tools=[SimpleNamespace(name=name, description='',
            input_schema={'type': 'object'}) for name in ('browser_navigate', 'browser_snapshot', 'browser_click', 'browser_close')])

    async def call_tool(self, name, arguments, **kwargs):
        self.calls.append((name, arguments))
        if arguments.get('slow'):
            self.entered.set()
            while not self.finish.is_set():
                await asyncio.sleep(.01)
        if name == 'browser_navigate':
            self.url = arguments['url']
        text = f'### Page\n- Page URL: {self.url}\n- Page Title: {self.title}\n### Snapshot\n- button [ref=e1]'
        return SimpleNamespace(content=[SimpleNamespace(text=text)], structured_content=None, is_error=False)


class BrowserSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.trusted = True
        self.server = McpServerConfig('browser', 'stdio', command='npx',
            args=('-y', '@playwright/mcp@0.0.83', '--browser', 'chrome', '--isolated'),
            lifecycle='execution', call_timeout=2)
        BrowserPeer.peers = []
        BrowserPeer.entered.clear()
        BrowserPeer.finish.clear()
        self.clients = patch('codeagent.mcp.router.Client', BrowserPeer)
        self.clients.start()
        async def transport(router, server, stack):
            return server
        self.transport = patch.object(McpRouter, '_transport', transport)
        self.transport.start()
        self.parent = McpRouter(workspace_root=self.root, servers=[self.server], trust_checker=lambda _: self.trusted)
        self.manager = BrowserSessions()

    def tearDown(self):
        self.manager.close()
        self.parent.close()
        self.transport.stop()
        self.clients.stop()
        self.temp.cleanup()

    def tools(self, conversation='one', token=None, budget=5):
        tools = ToolRegistry()
        self.parent.register_tools(tools, adapt=lambda tool: self.manager.adapt(tool, conversation))
        token = token or CancellationToken()
        tools.bind_runtime(token.raise_if_cancelled, lambda: budget)
        return tools

    def call(self, tools, name='browser_snapshot', **args):
        return tools.execute('mcp__browser__' + name, args)

    def session(self, conversation='one'):
        return next(s for s in self.manager.sessions.values() if s.conversation == conversation)

    def test_cross_turn_retention_and_conversation_isolation(self):
        first = self.tools()
        self.assertEqual(self.call(first, 'browser_navigate', url='https://one.test').status, 'success')
        peer = BrowserPeer.peers[-1]
        peer.title = 'Logged in'
        first.close_workspace_tools()
        self.assertFalse(peer.closed)
        second = self.tools()
        self.assertIn('Logged in', self.call(second))
        other = self.tools('two')
        self.assertIn('Fresh', self.call(other))
        self.assertNotIn('one.test', self.call(other))
        self.assertNotEqual(self.session().router, self.session('two').router)
        self.manager.close_conversation('one')
        self.assertTrue(peer.closed)
        self.assertEqual(self.call(second).status, 'blocked')
        self.assertEqual(self.call(other).status, 'success')
        reopened = self.tools()
        self.assertIn('Fresh', self.call(reopened))

    def test_web_factory_and_real_agent_cleanup_preserve_same_conversation_browser(self):
        from codeagent import ModelResponse
        from codeagent.config import EnvironmentConfig
        from codeagent.events import EventEmitter, ExecutionContext
        from codeagent.memory import MemoryConfig
        from codeagent.permissions import WaitingPermissionBroker
        from codeagent.web.factory import WebAgentFactory
        from codeagent.web.storage import SQLiteRepository
        from tests.test_plan_mode import Client
        repo = SQLiteRepository(self.root / 'db', recover_incomplete=False)
        env = EnvironmentConfig(model_id='test', browser_backend='mcp', enable_skills=False, data_dir=self.root / 'data',
                                memory_config=MemoryConfig(enabled=False))
        factory = WebAgentFactory(env, self.root, repo)
        conv = repo.create_conversation(workspace=self.root)
        try:
            first_peer = None
            for name, args in [('browser_navigate', {'url': 'https://retained.test/'}), ('browser_snapshot', {})]:
                run = repo.create_run(conv.id, status='completed')
                client = Client([ModelResponse('tool_use', [{'type': 'tool_use', 'id': 'b1',
                    'name': 'mcp__browser__' + name, 'input': args}]),
                    ModelResponse('end_turn', [{'type': 'text', 'text': 'done'}])])
                with patch.object(EnvironmentConfig, 'create_model_client', return_value=client), \
                     patch.object(WebAgentFactory, '_mcp_router', return_value=self.parent):
                    agent = factory.for_workspace(self.root).create(
                        event_emitter=EventEmitter(context=ExecutionContext(conversation_id=conv.id, run_id=run.id)),
                        cancellation=CancellationToken(), permission_broker=WaitingPermissionBroker(),
                        approval_mode='full', native_collaboration=False)
                    agent.run('Use the browser')
                current = BrowserPeer.peers[-1]
                if first_peer is not None:
                    self.assertIs(current, first_peer)
                    self.assertEqual(current.url, 'https://retained.test/')
                first_peer = current
                self.assertFalse(current.closed)
                self.assertEqual(factory.browser_sessions.list(conv.id)[0]['control'], 'agent')
        finally:
            factory.close()
            repo.close()

    def test_single_agent_owner_and_fork_cannot_close_parent_session(self):
        tools = self.tools()
        self.call(tools)
        child = tools.fork_workspace_tools()
        token = CancellationToken()
        child.bind_runtime(token.raise_if_cancelled, lambda: 5)
        self.assertEqual(self.call(child).status, 'blocked')
        child.close_workspace_tools()
        self.assertEqual(self.call(tools).status, 'success')
        tools.close_workspace_tools()
        self.assertEqual(self.call(child).status, 'success')

    def test_takeover_drains_then_handback_requires_fresh_agent_snapshot(self):
        tools = self.tools()
        self.call(tools)
        session = self.session()
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(self.call, tools, 'browser_click', slow=True)
            self.assertTrue(BrowserPeer.entered.wait(1))
            self.assertEqual(session.takeover()['control'], 'pausing')
            count = len(BrowserPeer.peers[-1].calls)
            self.assertEqual(self.call(tools).status, 'blocked')
            self.assertEqual(len(BrowserPeer.peers[-1].calls), count)
            BrowserPeer.finish.set()
            self.assertEqual(pending.result(2).status, 'success')
        self.assertEqual(session.view()['control'], 'human')
        BrowserPeer.peers[-1].title = 'Human logged in'
        self.assertEqual(self.call(tools, 'browser_click', ref='old').status, 'blocked')
        returned = session.handback()
        self.assertEqual(returned['control'], 'agent')
        self.assertEqual(returned['title'], 'Human logged in')
        self.assertEqual(self.call(tools, 'browser_click', ref='old').status, 'blocked')
        self.assertIn('Human logged in', self.call(tools))
        self.assertEqual(self.call(tools, 'browser_click', ref='e1').status, 'success')

    def test_unknown_inflight_result_never_grants_human_control(self):
        tools = self.tools(budget=.15)
        self.call(tools)
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(self.call, tools, 'browser_click', slow=True)
            self.assertTrue(BrowserPeer.entered.wait(1))
            self.session().takeover()
            self.assertEqual(pending.result(2).outcome, 'unknown')
        self.assertEqual(self.session().view()['control'], 'error')
        with self.assertRaises(ValueError):
            self.session().handback()
        self.assertEqual(self.session().close()['control'], 'closed')

    def test_cancel_inflight_closes_connection_and_does_not_replay(self):
        token = CancellationToken()
        tools = self.tools(token=token)
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(self.call, tools, 'browser_click', slow=True)
            self.assertTrue(BrowserPeer.entered.wait(1))
            token.cancel()
            with self.assertRaises(CancelledError):
                pending.result(2)
        self.assertEqual(self.session().view()['control'], 'closed')
        self.assertTrue(BrowserPeer.peers[-1].closed)

    def test_trust_revocation_and_parent_reload_invalidate_session(self):
        tools = self.tools()
        self.call(tools)
        self.trusted = False
        self.assertEqual(self.manager.list('one')[0]['control'], 'closed')
        self.assertEqual(self.call(tools).status, 'blocked')
        self.trusted = True
        new = self.tools()
        self.call(new)
        self.parent.retire()
        self.manager.retire(self.parent)
        self.assertEqual(self.call(new).status, 'blocked')

    def test_budget_stop_during_dispatch_cleans_up_without_granting_takeover(self):
        tools = self.tools()
        self.call(tools)
        stopped = threading.Event()
        def check():
            if stopped.is_set():
                raise ExecutionStopped('budget_exceeded:active_seconds')
        # Rebind before release so this test uses the same owner consistently.
        tools.close_workspace_tools()
        tools.bind_runtime(check, lambda: 5)
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(self.call, tools, 'browser_click', slow=True)
            self.assertTrue(BrowserPeer.entered.wait(1))
            self.session().takeover()
            stopped.set()
            with self.assertRaises(ExecutionStopped):
                pending.result(2)
        tools.close_workspace_tools()  # Cleanup must not rethrow a budget stop.
        self.assertEqual(self.session().view()['control'], 'closed')

    def test_idle_cleanup_and_headless_takeover_rejection(self):
        self.parent.servers[0] = replace(self.server, args=self.server.args + ('--headless',))
        tools = self.tools()
        self.call(tools)
        session = self.session()
        with self.assertRaisesRegex(ValueError, '后台'):
            session.takeover()
        self.manager.expire(session.last_used + 1801)
        self.assertEqual(session.view()['control'], 'closed')

    def test_unrecognized_config_and_shared_services_keep_original_tools(self):
        self.parent.servers[0] = replace(self.server, lifecycle='shared')
        tools = self.tools()
        self.assertEqual(self.manager.list('one'), [])
        self.assertEqual(self.call(tools).status, 'success')

    def test_shutdown_does_not_interrupt_already_started_transport_teardown(self):
        tools = self.tools()
        self.call(tools)
        async def delayed_exit(peer, *args):
            await asyncio.sleep(.05)
            peer.closed = True
        with patch.object(BrowserPeer, '__aexit__', delayed_exit):
            self.assertEqual(self.session().close()['control'], 'closed')
        self.assertTrue(BrowserPeer.peers[-1].closed)
        self.assertTrue(self.session().router._transport_closed['browser'])

    def test_closing_during_takeover_never_promotes_to_human_or_accepts_stale_wrapper(self):
        tools = self.tools()
        self.call(tools)
        session = self.session()
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(self.call, tools, 'browser_click', slow=True)
            self.assertTrue(BrowserPeer.entered.wait(1))
            session.takeover()
            session.close()
            pending.result(2)
        self.assertNotIn(session.view()['control'], {'agent', 'human'})
        self.assertEqual(session.view()['url'], '')
        self.assertEqual(self.call(tools).status, 'blocked')


class BrowserApiTests(unittest.TestCase):
    setUp = api_fixture.WebApiTests.setUp
    tearDown = api_fixture.WebApiTests.tearDown

    def test_routes_scope_control_and_cleanup_to_conversation(self):
        conv = self.repository.create_conversation(workspace=self.workspace)
        seen = []
        self.scheduler.browser_states = lambda cid: [{'id': cid}]
        self.scheduler.browser_action = lambda cid, sid, action: seen.append((cid, sid, action)) or {'control': 'human'}
        self.scheduler.close_browser_conversation = lambda cid: seen.append(('close', cid))
        url = f'/api/conversations/{conv.id}/browsers'
        self.assertEqual(self.client.get(url).json(), [{'id': conv.id}])
        self.assertEqual(self.client.post(url + '/browser-id/takeover').status_code, 200)
        self.assertEqual(seen[0], (conv.id, 'browser-id', 'takeover'))
        self.assertEqual(self.client.post(url + '/browser-id/arbitrary').status_code, 400)
        self.assertEqual(self.client.get('/api/conversations/missing/browsers').status_code, 404)
        self.assertEqual(self.client.delete(f'/api/conversations/{conv.id}').status_code, 204)
        self.assertEqual(seen[-1], ('close', conv.id))

    def test_cross_conversation_session_identifier_cannot_control_another_browser(self):
        manager = BrowserSessions()
        try:
            with self.assertRaises(ValueError):
                manager.action('different-conversation', 'browser-id', 'takeover')
        finally:
            manager.close()
