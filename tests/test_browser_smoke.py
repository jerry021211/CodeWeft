"""Opt-in local browser acceptance test; no model/API key or personal profile.

CODEAGENT_BROWSER_SMOKE=1 python -m unittest tests.test_browser_smoke -v
Requires Node.js 18+, npx and Chrome (or CODEAGENT_BROWSER_CHANNEL=msedge).
"""
import json
import os
import re
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from codeagent.mcp import McpRouter
from codeagent.runtime.cancellation import CancellationToken
from codeagent.tools import ToolRegistry


PAGE = b'''<!doctype html><title>Browser acceptance</title>
<label>Name <input id="name"></label><button id="submit">Submit</button>
<p id="result" role="status"></p><p id="storage"></p>
<script>
storage.textContent = localStorage.getItem('last') || 'Fresh session';
submit.onclick = () => setTimeout(() => {
  const value = document.querySelector('#name').value;
  result.textContent = 'Hello ' + value;
  localStorage.setItem('last', value);
}, 100);
</script>'''


class PageHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *args):
        pass


@unittest.skipUnless(os.environ.get('CODEAGENT_BROWSER_SMOKE') == '1', 'opt-in visible browser test')
class BrowserSmokeTests(unittest.TestCase):
    def test_visible_browser_form_result_close_reopen_and_storage_isolation(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), PageHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{server.server_port}'
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = root / 'mcp.json'
                config.write_text(json.dumps({'mcpServers': {'browser': {
                    'command': shutil.which('npx') or 'npx',
                    'args': ['-y', '@playwright/mcp@0.0.83', '--isolated', '--browser',
                             os.environ.get('CODEAGENT_BROWSER_CHANNEL', 'chrome'),
                             '--output-dir', str(root / 'output')],
                    'env': {**{key: os.environ[key] for key in ('npm_config_cache', 'TEMP', 'TMP') if key in os.environ},
                            'PLAYWRIGHT_BROWSERS_PATH': str(root / 'browsers')},
                    'startup_timeout': 120, 'call_timeout': 60, 'lifecycle': 'execution',
                }}}), encoding='utf-8')
                router = McpRouter(config, trust_checker=lambda _: True)
                registry, second = ToolRegistry(), ToolRegistry()
                third = ToolRegistry()
                try:
                    router.register_tools(registry)
                    router.register_tools(second)
                    token = CancellationToken()
                    second.bind_runtime(token.raise_if_cancelled, lambda: 60)
                    self.assertTrue(registry.schemas(), router.states())

                    def call(name, args=None, *, tools=registry):
                        output = tools.execute('mcp__browser__' + name, args or {})
                        self.assertEqual(output.status, 'success', str(output))
                        return str(output)

                    call('browser_navigate', {'url': url})
                    snapshot = call('browser_snapshot')
                    self.assertIn('Fresh session', snapshot)
                    self.assertEqual(second.execute('mcp__browser__browser_navigate', {'url': url}).status, 'blocked')
                    textbox = re.search(r'textbox "Name" \[ref=([^\]]+)\]', snapshot)
                    self.assertIsNotNone(textbox, snapshot)
                    schema = next(s for s in registry.schemas() if s['name'].endswith('__browser_type'))
                    target = 'target' if 'target' in schema['input_schema']['properties'] else 'ref'
                    snapshot = call('browser_type', {target: textbox[1], 'text': 'CodeWeft', 'element': 'Name'})
                    button = re.search(r'button "Submit" \[ref=([^\]]+)\]', snapshot)
                    if button is None:
                        snapshot = call('browser_snapshot')
                        button = re.search(r'button "Submit" \[ref=([^\]]+)\]', snapshot)
                    self.assertIsNotNone(button, snapshot)
                    call('browser_click', {target: button[1], 'element': 'Submit'})
                    call('browser_wait_for', {'text': 'Hello CodeWeft'})
                    self.assertIn('Hello CodeWeft', call('browser_snapshot'))
                    call('browser_close')  # A manually closed window can be reopened in the same run.
                    call('browser_navigate', {'url': url})
                    self.assertIn('Fresh session', call('browser_snapshot'))
                    # Write storage again, then rely on host teardown (not browser_close).
                    call('browser_evaluate', {'function': "() => localStorage.setItem('last', 'old-task')"})
                    registry.close_workspace_tools()
                    self.assertEqual(router.states()['browser']['status'], 'disconnected')
                    call('browser_navigate', {'url': url}, tools=second)
                    fresh = call('browser_snapshot', tools=second)
                    self.assertIn('Fresh session', fresh)
                    self.assertNotIn('old-task', fresh)
                    second.bind_runtime(token.raise_if_cancelled, lambda: .15)
                    timed_out = second.execute('mcp__browser__browser_wait_for', {'text': 'never appears'})
                    self.assertEqual(timed_out.outcome, 'unknown')
                    self.assertFalse(timed_out.retry_safe)
                    second.close_workspace_tools()
                    router.register_tools(third)
                    call('browser_navigate', {'url': url}, tools=third)
                    self.assertIn('Fresh session', call('browser_snapshot', tools=third))
                    third.close_workspace_tools()
                finally:
                    registry.close_workspace_tools()
                    second.close_workspace_tools()
                    third.close_workspace_tools()
                    router.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
