"""Opt-in direct Playwright acceptance on local pages; no MCP or personal profile."""
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from codeagent.browser.tools import BrowserSessions
from codeagent.runtime.cancellation import CancellationToken
from codeagent.tools import ToolRegistry


PAGE = b'''<!doctype html><title>Native browser acceptance</title>
<label>Name <input id="name"></label><button id="submit">Submit</button>
<button>Duplicate</button><button>Duplicate</button>
<p id="result" role="status"></p><p id="storage"></p>
<iframe src="/frame" title="Embedded form"></iframe>
<script>
storage.textContent = localStorage.getItem('last') || 'Fresh session';
submit.onclick = () => {
  const value = document.querySelector('#name').value;
  result.textContent = 'Hello ' + value;
  localStorage.setItem('last', value);
};
</script>'''


class PageHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        payload = b'<label>Inside <input></label><button>Frame action</button>' if self.path == '/frame' else PAGE
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@unittest.skipUnless(os.environ.get('CODEAGENT_BROWSER_SMOKE') == '1', 'opt-in local browser test')
class BuiltinBrowserSmokeTests(unittest.TestCase):
    def test_forms_frames_tabs_stale_targets_and_session_lifetime(self):
        http = ThreadingHTTPServer(('127.0.0.1', 0), PageHandler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        manager = BrowserSessions(headless=True, channel=os.environ.get('CODEAGENT_BROWSER_CHANNEL', 'chrome'))
        url = f'http://127.0.0.1:{http.server_port}/'
        def registry(chat):
            tools = ToolRegistry()
            manager.register_tools(tools, chat, Path.cwd())
            tools.bind_runtime(CancellationToken().raise_if_cancelled, lambda: 30)
            return tools
        def call(tools, name, **args):
            result = tools.execute(name, args)
            self.assertEqual(result.status, 'success', str(result))
            return json.loads(result) if result.startswith('{') else str(result)
        def target(snapshot, role, name, frame=0):
            return dict(snapshot_id=snapshot['snapshot_id'], frame_id=snapshot['frames'][frame]['frame_id'],
                        target=dict(role=role, name=name))
        try:
            first = registry('one')
            call(first, 'browser_navigate', url=url)
            snapshot = call(first, 'browser_snapshot')
            self.assertIn('Fresh session', str(snapshot))
            self.assertEqual(len(snapshot['frames']), 2)
            duplicate = first.execute('browser_click', target(snapshot, 'button', 'Duplicate'))
            self.assertEqual(duplicate.status, 'error')
            self.assertIn('ambiguous', duplicate)
            call(first, 'browser_fill', **target(snapshot, 'textbox', 'Name'), value='CodeWeft')
            stale = first.execute('browser_click', target(snapshot, 'button', 'Submit'))
            self.assertEqual(stale.status, 'error')
            self.assertIn('stale', stale)
            snapshot = call(first, 'browser_snapshot')
            call(first, 'browser_click', **target(snapshot, 'button', 'Submit'))
            call(first, 'browser_wait_for', text='Hello CodeWeft')
            snapshot = call(first, 'browser_snapshot')
            self.assertIn('Hello CodeWeft', str(snapshot))
            call(first, 'browser_fill', **target(snapshot, 'textbox', 'Inside', frame=1), value='Frame text')
            snapshot = call(first, 'browser_snapshot')
            call(first, 'browser_press_key', **target(snapshot, 'textbox', 'Inside', frame=1), key='Tab')
            call(first, 'browser_navigate', url=url, new_tab=True)
            tabs = call(first, 'browser_tabs', action='list')['tabs']
            self.assertEqual(len(tabs), 2)
            call(first, 'browser_tabs', action='select', tab_id=tabs[0]['tab_id'])
            call(first, 'browser_tabs', action='close', tab_id=tabs[1]['tab_id'])
            self.assertEqual(len(call(first, 'browser_tabs', action='list')['tabs']), 1)
            first.close_workspace_tools()
            again = registry('one')
            call(again, 'browser_navigate', url=url)
            self.assertIn('CodeWeft', str(call(again, 'browser_snapshot')))
            other = registry('two')
            call(other, 'browser_navigate', url=url)
            self.assertIn('Fresh session', str(call(other, 'browser_snapshot')))
            call(again, 'browser_close')
            self.assertEqual(manager.list('one')[0]['control'], 'closed')
            fresh = registry('one')
            call(fresh, 'browser_navigate', url=url)
            self.assertIn('Fresh session', str(call(fresh, 'browser_snapshot')))
            rejected = fresh.execute('browser_navigate', {'url': 'file:///private'})
            self.assertEqual(rejected.status, 'error')
            self.assertNotEqual(rejected.outcome, 'unknown')
        finally:
            manager.close()
            http.shutdown()
            http.server_close()
            thread.join(2)
