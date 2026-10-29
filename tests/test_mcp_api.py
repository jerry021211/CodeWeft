import json
import sys
import unittest
from unittest.mock import Mock

from tests import test_web_api as fixture


@unittest.skipIf(fixture.TestClient is None, 'FastAPI test dependencies are not installed')
class McpApiTests(unittest.TestCase):
    # Reuse the established HTTP/CSRF fixture, not its unrelated test cases.
    setUp = fixture.WebApiTests.setUp
    tearDown = fixture.WebApiTests.tearDown
    def add_server(self):
        response = self.client.post('/api/mcp/servers', json={
            'workspace': str(self.workspace), 'name': 'docs', 'transport': 'http',
            'url': 'https://example.invalid/mcp', 'headers': {'Authorization': 'secret-token'},
            'startup_timeout': 12, 'call_timeout': 25,
        })
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()['servers'][0]

    def test_save_does_not_grant_trust_or_connect_and_secrets_are_hidden(self):
        self.scheduler.connect_mcp = Mock()
        server = self.add_server()
        self.assertFalse(server['trusted'])
        self.assertEqual(server['status'], 'awaiting_trust')
        self.assertEqual(server['call_timeout'], 25)
        self.assertNotIn('secret-token', json.dumps(server))
        self.scheduler.connect_mcp.assert_not_called()
        blocked = self.client.post('/api/mcp/servers/docs/connect', json={
            'workspace': str(self.workspace), 'fingerprint': server['fingerprint']})
        self.assertEqual(blocked.status_code, 403)
        self.scheduler.connect_mcp.assert_not_called()

    def test_approve_exact_snapshot_then_connect_and_revoke(self):
        server = self.add_server()
        body = {'workspace': str(self.workspace), 'fingerprint': server['fingerprint'], 'approved': True}
        approved = self.client.post('/api/mcp/servers/docs/trust', json=body)
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertTrue(approved.json()['servers'][0]['trusted'])
        self.assertEqual(approved.json()['servers'][0]['status'], 'not_connected')
        self.scheduler.connect_mcp = Mock()
        self.scheduler.mcp_states = lambda _: {'docs': {'fingerprint': server['fingerprint'], 'status': 'connected', 'tool_count': 3}}
        connected = self.client.post('/api/mcp/servers/docs/connect', json=body)
        self.assertEqual(connected.json()['servers'][0]['tool_count'], 3)
        self.scheduler.connect_mcp.assert_called_once_with(str(self.workspace), 'docs')
        revoked = self.client.post('/api/mcp/servers/docs/trust', json={**body, 'approved': False})
        self.assertEqual(revoked.json()['servers'][0]['status'], 'awaiting_trust')
        self.assertEqual(revoked.json()['servers'][0]['tool_count'], 0)

    def test_stale_approval_and_connect_cannot_authorize_changed_configuration(self):
        server = self.add_server()
        body = {'workspace': str(self.workspace), 'fingerprint': server['fingerprint'], 'approved': True}
        path = self.workspace / 'mcp.json'
        document = json.loads(path.read_text())
        document['mcpServers']['docs']['url'] = 'https://changed.invalid/mcp'
        path.write_text(json.dumps(document))
        for action in ('trust', 'connect'):
            response = self.client.post('/api/mcp/servers/docs/' + action, json=body)
            self.assertEqual(response.status_code, 409)
        current = self.client.get('/api/mcp/servers', params={'workspace': str(self.workspace)}).json()
        self.assertFalse(current['servers'][0]['trusted'])

    def test_config_cannot_self_approve_and_malformed_neighbor_is_isolated(self):
        path = self.workspace / 'mcp.json'
        path.write_text(json.dumps({'mcpServers': {
            'bad': 7, 'docs': {'url': 'https://example.invalid/mcp', 'trusted': True, 'approved': True}}}))
        response = self.client.get('/api/mcp/servers', params={'workspace': str(self.workspace)})
        self.assertEqual(response.status_code, 200, response.text)
        servers = {item['name']: item for item in response.json()['servers']}
        self.assertEqual(servers['bad']['status'], 'error')
        self.assertEqual(servers['docs']['status'], 'awaiting_trust')

    def test_timeouts_are_validated(self):
        for field, value in [('startup_timeout', 0), ('call_timeout', -1), ('startup_timeout', 301)]:
            result = self.client.post('/api/mcp/servers', json={
                'workspace': str(self.workspace), 'name': 'docs', 'transport': 'http',
                'url': 'https://example.invalid/mcp', field: value})
            self.assertEqual(result.status_code, 422)

    def test_execution_lifecycle_round_trips_and_legacy_edit_preserves_it(self):
        draft = {'workspace': str(self.workspace), 'name': 'browser', 'transport': 'stdio',
                 'command': 'npx', 'args': ['-y', '@playwright/mcp@0.0.83', '--isolated'],
                 'lifecycle': 'execution'}
        response = self.client.post('/api/mcp/servers', json=draft)
        self.assertEqual(response.status_code, 201, response.text)
        server = response.json()['servers'][0]
        self.assertEqual(server['lifecycle'], 'execution')
        self.assertFalse(server['trusted'])
        draft.pop('lifecycle')
        updated = self.client.post('/api/mcp/servers', json={**draft, 'fingerprint': server['fingerprint']})
        self.assertEqual(updated.status_code, 201, updated.text)
        self.assertEqual(updated.json()['servers'][0]['lifecycle'], 'execution')
        for value in ['execution', 'unknown']:
            response = self.client.post('/api/mcp/servers', json={
                'workspace': str(self.workspace), 'name': 'remote', 'transport': 'http',
                'url': 'https://example.invalid/mcp', 'lifecycle': value})
            self.assertIn(response.status_code, [400, 422])

    def test_edit_keeps_hidden_credentials_unless_explicitly_cleared_and_invalidates_trust(self):
        server = self.add_server()
        self.client.post('/api/mcp/servers/docs/trust', json={
            'workspace': str(self.workspace), 'fingerprint': server['fingerprint'], 'approved': True})
        draft = {'workspace': str(self.workspace), 'name': 'docs', 'transport': 'http',
                 'url': 'https://example.invalid/mcp', 'call_timeout': 90, 'fingerprint': server['fingerprint']}
        updated = self.client.post('/api/mcp/servers', json=draft)
        self.assertEqual(updated.status_code, 201, updated.text)
        self.assertFalse(updated.json()['servers'][0]['trusted'])
        document = json.loads((self.workspace / 'mcp.json').read_text())
        self.assertEqual(document['mcpServers']['docs']['headers']['Authorization'], 'secret-token')
        self.assertEqual(self.client.post('/api/mcp/servers', json=draft).status_code, 409)
        cleared = self.client.post('/api/mcp/servers', json={**draft, 'headers': {},
            'fingerprint': updated.json()['servers'][0]['fingerprint']})
        self.assertEqual(cleared.status_code, 201)
        self.assertEqual(cleared.json()['servers'][0]['header_keys'], [])

    def test_real_factory_uses_same_trust_store_and_reports_live_connection(self):
        from codeagent.config import EnvironmentConfig
        from codeagent.web.factory import WebAgentFactory
        script = self.workspace / 'local_server.py'
        script.write_text('from mcp.server import MCPServer\nserver=MCPServer("qa")\n'
                          '@server.tool()\ndef ping() -> str:\n    return "pong"\nserver.run()\n')
        env = EnvironmentConfig(model_id='test', data_dir=self.env.data_dir)
        factory = WebAgentFactory(env, self.workspace, self.repository)
        self.scheduler.connect_mcp = factory.connect_mcp
        self.scheduler.mcp_states = factory.mcp_states
        self.scheduler.reload_mcp = lambda workspace: (factory.reload_mcp(workspace) or True)
        try:
            response = self.client.post('/api/mcp/servers', json={
                'workspace': str(self.workspace), 'name': 'local', 'transport': 'stdio',
                'command': sys.executable, 'args': [str(script)]})
            server = response.json()['servers'][0]
            self.assertEqual(factory.mcp_states(self.workspace), {})
            body = {'workspace': str(self.workspace), 'fingerprint': server['fingerprint'], 'approved': True}
            self.assertEqual(self.client.post('/api/mcp/servers/local/trust', json=body).status_code, 200)
            connected = self.client.post('/api/mcp/servers/local/connect', json=body)
            self.assertEqual(connected.status_code, 200, connected.text)
            self.assertEqual(connected.json()['servers'][0]['status'], 'connected', connected.text)
            self.assertEqual(connected.json()['servers'][0]['tool_count'], 1)
            # Every settings mutation must reclaim the old real SDK/process,
            # while the next connection gets a fresh configuration generation.
            for mutation in ('edit', 'revoke', 'delete'):
                with self.subTest(mutation=mutation):
                    old_router = factory._mcp_router()
                    if mutation == 'edit':
                        changed = self.client.post('/api/mcp/servers', json={
                            'workspace': str(self.workspace), 'name': 'local', 'transport': 'stdio',
                            'command': sys.executable, 'args': [str(script)], 'call_timeout': 90,
                            'fingerprint': body['fingerprint']})
                        self.assertEqual(changed.status_code, 201, changed.text)
                        body['fingerprint'] = changed.json()['servers'][0]['fingerprint']
                    elif mutation == 'revoke':
                        changed = self.client.post('/api/mcp/servers/local/trust', json={**body, 'approved': False})
                        self.assertEqual(changed.status_code, 200, changed.text)
                    else:
                        changed = self.client.delete('/api/mcp/servers/local', params={'workspace': str(self.workspace)})
                        self.assertEqual(changed.status_code, 200, changed.text)
                    old_router._thread.join(6)
                    self.assertFalse(old_router._thread.is_alive(), 'Settings left a stale MCP transport running')
                    self.assertNotEqual(old_router.call_tool('local', 'ping', {}).status, 'success')
                    self.assertEqual(factory.mcp_states(self.workspace), {})
                    if mutation != 'delete':
                        self.assertEqual(self.client.post('/api/mcp/servers/local/trust', json=body).status_code, 200)
                        connected = self.client.post('/api/mcp/servers/local/connect', json=body)
                        self.assertEqual(connected.status_code, 200, connected.text)
                        self.assertEqual(connected.json()['servers'][0]['status'], 'connected')
                        self.assertIsNot(factory._mcp_router(), old_router)
        finally:
            factory.close()
