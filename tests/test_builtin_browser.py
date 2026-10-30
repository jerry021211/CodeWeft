"""Native browser dispatch, permission and ownership contracts without a browser."""
import asyncio
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from codeagent.browser.playwright import PlaywrightBackend
from codeagent.browser.tools import BrowserSessions, NAMES
from codeagent.config import EnvironmentConfig
from codeagent.permissions.policy import PermissionPolicy
from codeagent.permissions.read_only import read_only_tool_guard
from codeagent.messages import ToolUse
from codeagent.runtime.cancellation import CancellationToken, CancelledError
from codeagent.runtime.execution import ExecutionStopped
from codeagent.tools import ToolRegistry


class Page:
    url = 'https://local.test/'
    def is_closed(self):
        return False
    async def title(self):
        return 'Fake page'


class Runtime(PlaywrightBackend):
    async def _ensure_browser(self):
        self.started = True
        self._page = Page()

    async def _dispatch(self, name, args):
        self.dispatch_thread = threading.get_ident()
        if name == 'browser_wait_for':
            self.entered.set()
            await asyncio.sleep(args['seconds'])
        return {'completed': True}

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.entered = threading.Event()


class NativeBrowserTests(unittest.TestCase):
    def setUp(self):
        self.manager = BrowserSessions(backend_factory=Runtime)
        self.token = CancellationToken()

    def tearDown(self):
        self.manager.close()

    def registry(self, conversation='one', token=None, remaining=5):
        tools = ToolRegistry()
        self.manager.register_tools(tools, conversation, Path.cwd())
        tools.bind_runtime((token or self.token).raise_if_cancelled, lambda: remaining)
        return tools

    def session(self, conversation='one'):
        return next(s for s in self.manager.sessions.values() if s.conversation == conversation)

    def test_native_tools_are_lazy_and_rounds_reuse_only_their_own_session(self):
        first = self.registry()
        self.assertEqual({s['name'] for s in first.schemas()}, NAMES)
        self.assertIsNone(self.session().backend._thread)
        self.assertEqual(first.execute('browser_snapshot').status, 'success')
        backend = self.session().backend
        self.assertNotEqual(backend.dispatch_thread, threading.get_ident())
        first.close_workspace_tools()
        second = self.registry()
        self.assertEqual(second.execute('browser_snapshot').status, 'success')
        self.assertIs(self.session().backend, backend)
        self.registry('two')
        self.assertIsNot(self.session('two').backend, backend)
        self.manager.close_conversation('one')
        self.assertTrue(backend.is_closed())
        self.assertEqual(second.execute('browser_snapshot').status, 'blocked')

    def test_handoff_drains_and_requires_fresh_agent_snapshot(self):
        tools = self.registry()
        tools.execute('browser_snapshot')
        session = self.session()
        with ThreadPoolExecutor(1) as pool:
            call = pool.submit(tools.execute, 'browser_wait_for', {'seconds': .2})
            self.assertTrue(session.backend.entered.wait(1))
            self.assertEqual(session.takeover()['control'], 'pausing')
            self.assertEqual(tools.execute('browser_snapshot').status, 'blocked')
            self.assertEqual(call.result(2).status, 'success')
        self.assertEqual(session.view()['control'], 'human')
        self.assertEqual(session.handback()['control'], 'agent')
        self.assertEqual(tools.execute('browser_navigate', {'url': 'https://example.com'}).status, 'blocked')
        self.assertEqual(tools.execute('browser_snapshot').status, 'success')

    def test_timeout_never_grants_human_control_or_replays(self):
        tools = self.registry(remaining=.15)
        tools.execute('browser_snapshot')
        session = self.session()
        with ThreadPoolExecutor(1) as pool:
            call = pool.submit(tools.execute, 'browser_wait_for', {'seconds': 5})
            self.assertTrue(session.backend.entered.wait(1))
            session.takeover()
            result = call.result(2)
        self.assertEqual(result.outcome, 'unknown')
        self.assertFalse(result.retry_safe)
        self.assertEqual(session.view()['control'], 'error')
        self.assertEqual(session.close()['control'], 'closed')

    def test_cancellation_and_budget_stop_close_inflight_browser(self):
        for error in (CancelledError, ExecutionStopped):
            with self.subTest(error=error):
                tools = self.registry(str(error))
                stop = threading.Event()
                def check():
                    if stop.is_set():
                        raise error('stopped')
                tools.bind_runtime(check, lambda: 5)
                with ThreadPoolExecutor(1) as pool:
                    call = pool.submit(tools.execute, 'browser_wait_for', {'seconds': 5})
                    session = self.session(str(error))
                    self.assertTrue(session.backend.entered.wait(1))
                    stop.set()
                    with self.assertRaises(error):
                        call.result(2)
                self.assertEqual(session.view()['control'], 'closed')
                tools.close_workspace_tools()

    def test_forked_agent_cannot_steal_browser_and_idle_closes_it(self):
        tools = self.registry()
        tools.execute('browser_snapshot')
        child = tools.fork_workspace_tools()
        child.bind_runtime(CancellationToken().raise_if_cancelled, lambda: 5)
        self.assertEqual(child.execute('browser_snapshot').status, 'blocked')
        child.close_workspace_tools()
        self.assertEqual(tools.execute('browser_snapshot').status, 'success')
        tools.close_workspace_tools()
        self.assertEqual(child.execute('browser_snapshot').status, 'success')
        session = self.session()
        self.manager.expire(session.last_used + 1801)
        self.assertEqual(session.view()['control'], 'closed')

    def test_close_tool_does_not_start_browser_and_next_round_is_fresh(self):
        tools = self.registry()
        old = self.session()
        self.assertEqual(tools.execute('browser_close').status, 'success')
        self.assertFalse(old.started)
        self.assertEqual(old.view()['control'], 'closed')
        self.registry()
        self.assertIsNot(old, self.session())

    def test_missing_dependency_is_actionable_without_mcp_fallback(self):
        runtime = PlaywrightBackend()
        try:
            with patch.object(runtime, '_ensure_browser', side_effect=ImportError('missing')):
                result = runtime.call('browser_snapshot', {}, lambda: None, lambda: 5)
            self.assertEqual(result.status, 'error')
            self.assertIn('.[browser]', result)
        finally:
            runtime.close()

    def test_close_during_inflight_wait_wins_over_takeover(self):
        tools = self.registry()
        tools.execute('browser_snapshot')
        session = self.session()
        with ThreadPoolExecutor(1) as pool:
            call = pool.submit(tools.execute, 'browser_wait_for', {'seconds': 5})
            self.assertTrue(session.backend.entered.wait(1))
            session.takeover()
            self.assertEqual(session.close()['control'], 'closed')
            call.result(2)
        self.assertEqual(session.view()['control'], 'closed')
        self.assertEqual(tools.execute('browser_snapshot').status, 'blocked')


class NativeBrowserPolicyTests(unittest.TestCase):
    def test_native_tools_use_existing_approval_modes_and_read_only_guard(self):
        readers = [('browser_snapshot', {}), ('browser_tabs', {'action': 'list'}),
                   ('browser_wait_for', {'text': 'ready'}), ('browser_navigate', {'url': 'https://example.com/'})]
        writers = [('browser_click', {}), ('browser_fill', {}), ('browser_close', {}),
                   ('browser_tabs', {'action': 'close', 'tab_id': 'one'}),
                   ('browser_navigate', {'url': 'https://example.com/delete?id=1'}),
                   ('browser_snapshot', {'filename': 'out.txt'})]
        for mode in (None, 'ask', 'auto', 'full'):
            ask = Mock(return_value=False)
            policy = PermissionPolicy(approval_mode=mode, ask=ask)
            for name, args in readers:
                self.assertEqual(policy.check(name, args).allowed, mode in {'auto', 'full'})
            for name, args in writers:
                self.assertEqual(policy.check(name, args).allowed, mode == 'full')
            for name in NAMES:
                self.assertIsNotNone(read_only_tool_guard(ToolUse('id', name, {})))

    def test_invalid_modes_are_rejected_and_defaults_are_native(self):
        env = EnvironmentConfig(model_id='test')
        self.assertEqual(env.browser_backend, 'builtin')
        with self.assertRaises(ValueError):
            replace(env, browser_backend='typo')
        with self.assertRaises(ValueError):
            replace(env, browser_channel='arbitrary.exe')

    def test_factory_filters_only_bundled_browser_and_keeps_generic_mcp(self):
        from codeagent.web.factory import WebAgentFactory
        from codeagent.web.storage import SQLiteRepository
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'mcp.json').write_text(json.dumps({'mcpServers': {
                'browser': {'command': 'npx', 'args': ['-y', '@playwright/mcp@0.0.83', '--browser', 'chrome', '--isolated']},
                'other': {'command': 'python', 'args': ['server.py']}}}), encoding='utf-8')
            repo = SQLiteRepository(root / 'db')
            env = EnvironmentConfig(model_id='test', data_dir=root / 'data')
            factory = WebAgentFactory(env, root, repo)
            legacy = WebAgentFactory(replace(env, browser_backend='mcp'), root, repo)
            try:
                self.assertEqual([s.name for s in factory._mcp_router().servers], ['other'])
                self.assertEqual({s.name for s in legacy._mcp_router().servers}, {'browser', 'other'})
                self.assertIs(factory.for_workspace(root).browser_sessions, factory.browser_sessions)
                with self.assertRaisesRegex(ValueError, '内置浏览器'):
                    factory.connect_mcp(root, 'browser')
            finally:
                factory.close()
                legacy.close()
                repo.close()

    def test_real_agent_uses_native_registry_and_retains_session_after_turn(self):
        from codeagent import ModelResponse
        from codeagent.events import EventEmitter, ExecutionContext
        from codeagent.memory import MemoryConfig
        from codeagent.permissions import WaitingPermissionBroker
        from codeagent.web.factory import WebAgentFactory
        from codeagent.web.storage import SQLiteRepository
        from tests.test_plan_mode import Client
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = SQLiteRepository(root / 'db', recover_incomplete=False)
            env = EnvironmentConfig(model_id='test', data_dir=root / 'data', enable_skills=False,
                                    memory_config=MemoryConfig(enabled=False))
            factory = WebAgentFactory(env, root, repo)
            factory.browser_sessions.backend_factory = Runtime
            conv = repo.create_conversation(workspace=root)
            try:
                backend = None
                for _ in range(2):
                    run = repo.create_run(conv.id, status='completed')
                    client = Client([ModelResponse('tool_use', [{'type': 'tool_use', 'id': 'b1',
                        'name': 'browser_snapshot', 'input': {}}]),
                        ModelResponse('end_turn', [{'type': 'text', 'text': 'done'}])])
                    with patch.object(EnvironmentConfig, 'create_model_client', return_value=client):
                        agent = factory.for_workspace(root).create(
                            event_emitter=EventEmitter(context=ExecutionContext(conversation_id=conv.id, run_id=run.id)),
                            cancellation=CancellationToken(), permission_broker=WaitingPermissionBroker(),
                            approval_mode='auto', native_collaboration=False)
                        agent.run('Read the current browser')
                    session = next(iter(factory.browser_sessions.sessions.values()))
                    self.assertTrue(session.started)
                    self.assertFalse(session.backend.is_closed())
                    self.assertIsNone(session.owner)
                    if backend is not None:
                        self.assertIs(session.backend, backend)
                    backend = session.backend
            finally:
                factory.close()
                repo.close()
