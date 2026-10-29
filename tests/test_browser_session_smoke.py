"""Opt-in local Chrome test for conversation-owned sessions (no account/model)."""
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from codeagent.mcp.browser import BrowserSessions
from codeagent.mcp.config import McpServerConfig
from codeagent.mcp.router import McpRouter
from codeagent.tools import ToolRegistry
from tests.test_browser_smoke import PageHandler


@unittest.skipUnless(os.environ.get('CODEAGENT_BROWSER_SMOKE') == '1', 'opt-in local browser test')
class BrowserSessionSmokeTests(unittest.TestCase):
    def test_real_browser_retains_storage_between_turns_and_isolates_chats(self):
        http = ThreadingHTTPServer(('127.0.0.1', 0), PageHandler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{http.server_port}/'
        original_transport = McpRouter._transport
        async def test_transport(router, server, stack):
            # The SDK intentionally filters inherited env. Forward only test
            # cache/temp paths so offline sandbox runs use the installed package.
            env = {key: os.environ[key] for key in ('npm_config_cache', 'npm_config_offline', 'TEMP', 'TMP') if key in os.environ}
            env['PLAYWRIGHT_BROWSERS_PATH'] = str(router.workspace_root / 'browser-runtime')
            env['LOCALAPPDATA'] = str(router.workspace_root / 'browser-cache')
            return await original_transport(router, replace(server, env=env), stack)
        transport_patch = patch.object(McpRouter, '_transport', test_transport)
        transport_patch.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                config = McpServerConfig('browser', 'stdio', command='npx',
                    args=('-y', '@playwright/mcp@0.0.83', '--isolated', '--browser',
                          os.environ.get('CODEAGENT_BROWSER_CHANNEL', 'chrome'), '--headless'),
                    lifecycle='execution', startup_timeout=120)
                parent = McpRouter(workspace_root=Path(directory), servers=[config], trust_checker=lambda _: True)
                manager = BrowserSessions()
                registries = []
                def registry(chat):
                    tools = ToolRegistry()
                    parent.register_tools(tools, adapt=lambda tool: manager.adapt(tool, chat))
                    registries.append(tools)
                    return tools
                def call(tools, name, **args):
                    result = tools.execute('mcp__browser__' + name, args)
                    self.assertEqual(result.status, 'success', str(result))
                    return str(result)
                try:
                    first = registry('one')
                    call(first, 'browser_navigate', url=url)
                    call(first, 'browser_evaluate', function="() => localStorage.setItem('last', 'retained-session')")
                    first.close_workspace_tools()
                    second = registry('one')
                    call(second, 'browser_navigate', url=url)
                    snapshot = call(second, 'browser_snapshot')
                    self.assertIn('retained-session', snapshot)
                    state = manager.list('one')[0]
                    self.assertEqual(state['url'], url)
                    self.assertEqual(state['title'], 'Browser acceptance', snapshot)
                    other = registry('two')
                    call(other, 'browser_navigate', url=url)
                    self.assertIn('Fresh session', call(other, 'browser_snapshot'))
                    manager.close_conversation('one')
                    old = next(s for s in manager.sessions.values() if s.conversation == 'one')
                    self.assertEqual(old.view()['control'], 'closed', repr((old.router.states(), old.router._transport_closed, old.router._startup_error)))
                    third = registry('one')
                    call(third, 'browser_navigate', url=url)
                    self.assertIn('Fresh session', call(third, 'browser_snapshot'))
                    self.assertEqual(manager.list('two')[0]['control'], 'agent')
                finally:
                    for tools in registries:
                        tools.close_workspace_tools()
                    manager.close()
                    parent.close()
        finally:
            transport_patch.stop()
            http.shutdown()
            http.server_close()
            thread.join(2)
