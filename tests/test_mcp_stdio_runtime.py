"""Real SDK/process regression coverage; no model credentials or network required."""
import json
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from codeagent.mcp import McpRouter, load_mcp_servers
from codeagent.mcp.trust import McpTrustStore
from codeagent.runtime.cancellation import CancellationToken, CancelledError


class McpStdioRuntimeTests(unittest.TestCase):
    def test_configuration_reload_drains_real_request_then_reclaims_process_connection(self):
        from codeagent import EnvironmentConfig
        from codeagent.web.factory import WebAgentFactory
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker, release = root / 'entered', root / 'release'
            server = root / 'server.py'
            server.write_text('''import asyncio
from pathlib import Path
from mcp.server import MCPServer
server = MCPServer("drain-test")
@server.tool()
async def wait_for_release(marker: str, release: str) -> str:
    Path(marker).write_text("entered")
    while not Path(release).exists():
        await asyncio.sleep(.02)
    return "completed-once"
server.run()
''', encoding='utf-8')
            path = root / 'mcp.json'
            path.write_text(json.dumps({'mcpServers': {'local': {
                'command': sys.executable, 'args': [str(server)], 'call_timeout': 5}}}))
            env = EnvironmentConfig(model_id='test', data_dir=root / 'data')
            trust = McpTrustStore(root / 'data' / 'state' / 'mcp-trust.db')
            trust.approve(root, load_mcp_servers(path)[0])
            factory = WebAgentFactory(env, root, SimpleNamespace())
            router = factory._mcp_router()
            try:
                router.start()
                with ThreadPoolExecutor(1) as executor:
                    call = executor.submit(router.call_tool, 'local', 'wait_for_release',
                                           {'marker': str(marker), 'release': str(release)})
                    try:
                        deadline = time.monotonic() + 3
                        while not marker.exists() and time.monotonic() < deadline:
                            time.sleep(.02)
                        self.assertTrue(marker.exists())
                        path.write_text('{"mcpServers": {}}')
                        factory.reload_mcp(root)
                        self.assertTrue(router._thread.is_alive())
                        self.assertEqual(router.call_tool('local', 'wait_for_release', {}).status, 'blocked')
                        self.assertEqual(factory._mcp_router().list_tools(), [])
                    finally:
                        release.write_text('release')
                    self.assertIn('completed-once', call.result(timeout=3))
                router._thread.join(6)
                self.assertFalse(router._thread.is_alive())
            finally:
                factory.close()

    def test_broken_and_hanging_processes_do_not_remove_healthy_tools_and_calls_cancel(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = root / 'server.py'
            marker = root / 'entered'
            server.write_text('''import asyncio
from pathlib import Path
from mcp.server import MCPServer
server = MCPServer("runtime-test")
@server.tool()
async def slow(marker: str) -> str:
    Path(marker).write_text("entered")
    await asyncio.sleep(30)
    return "late"
@server.tool()
def ping() -> str:
    return "pong"
server.run()
''', encoding='utf-8')
            path = root / 'mcp.json'
            path.write_text(json.dumps({'mcpServers': {
                'good': {'command': sys.executable, 'args': [str(server)], 'call_timeout': 5},
                'bad': {'command': sys.executable, 'args': ['-c', 'raise SystemExit(1)']},
                'hang': {'command': sys.executable, 'args': ['-c', 'import time; time.sleep(30)'], 'startup_timeout': .3},
            }}), encoding='utf-8')
            trust = McpTrustStore(root / 'trust.db')
            for config in load_mcp_servers(path):
                trust.approve(root, config)
            router = McpRouter(path, trust_checker=lambda config: trust.is_trusted(root, config))
            try:
                started = time.monotonic()
                router.start()
                self.assertLess(time.monotonic() - started, 8)
                self.assertEqual(router.states()['good']['status'], 'connected', router.states())
                self.assertEqual(router.states()['bad']['status'], 'error')
                self.assertEqual(router.states()['hang']['status'], 'error')
                self.assertEqual(router.list_tools(), ['mcp__good__ping', 'mcp__good__slow'])
                token = CancellationToken()
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(router.call_tool, 'good', 'slow', {'marker': str(marker)},
                                             cancellation_check=token.raise_if_cancelled)
                    deadline = time.monotonic() + 3
                    while not marker.exists() and time.monotonic() < deadline:
                        time.sleep(.02)
                    self.assertTrue(marker.exists())
                    token.cancel('cancel stdio request')
                    with self.assertRaises(CancelledError):
                        future.result(timeout=2)
                self.assertIn('pong', router.call_tool('good', 'ping', {}))
                result = router.call_tool('good', 'slow', {'marker': str(marker)}, remaining_seconds=lambda: .1)
                self.assertEqual(result.outcome, 'unknown')
                self.assertFalse(result.retry_safe)
                self.assertIn('pong', router.call_tool('good', 'ping', {}))
            finally:
                router.close()
            self.assertFalse(router._thread.is_alive())
