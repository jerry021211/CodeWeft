"""Search time exemptions must survive the Agent, provider and supervisor layers."""
import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, MagicMock

import httpx

from codeagent import Agent, AgentConfig, ModelResponse, OpenAIModelClient, ToolRegistry
from codeagent.hooks.loop_guard import LoopGuardConfig
from codeagent.messages import ToolUse
from codeagent.runtime.activity import ExecutionActivity
from codeagent.runtime.cancellation import CancellationToken, CancelledError, ModelCallTimeout
from codeagent.runtime.execution import RunBudget
from codeagent.tools.search_code import SearchCodeTool
from codeagent.code_search.embedding import EmbeddingConfig
from codeagent.code_search.service import CodeSearch
from tests.test_anthropic_client import FakeSdkClient
from codeagent.anthropic_client import AnthropicModelClient


class SearchTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.clock = lambda: self.now
        self.token = CancellationToken()
        self.activity = ExecutionActivity(self.token, clock=self.clock)

    def test_search_can_take_hours_and_next_tool_still_times_out(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'repo'
            root.mkdir()
            (root / 'sample.py').write_text('def reserve():\n    return "seat"\n')
            test = self

            class Provider:
                provider, model, dimensions = 'fake', 'fake', 2
                calls = 0

                def embed(self, texts, *, check, timeout):
                    test.assertEqual(timeout, float('inf'))
                    self.calls += 1
                    test.now += 3600
                    check()
                    test.assertIsNone(test.activity.timeout_reason(120))
                    if self.calls == 1:
                        with closing(sqlite3.connect(Path(directory) / 'index' / 'vectors-v2.sqlite3')) as conn:
                            test.assertGreater(conn.execute('SELECT MIN(expires) FROM jobs').fetchone()[0], test.now)
                    return [[1., 0.] for _ in texts]

            class Client:
                calls = 0

                def create_message(self, **kwargs):
                    self.calls += 1
                    if self.calls == 1:
                        return ModelResponse('tool_use', [{'type': 'tool_use', 'id': 'search',
                            'name': 'search_code', 'input': {'query': '购买座位', 'keywords': ['seat']}}])
                    return ModelResponse('end_turn', [{'type': 'text', 'text': 'done'}])

            registry = ToolRegistry()
            registry.register(SearchCodeTool(root, Path(directory) / 'index', embedding_provider=Provider()))
            budget = RunBudget(LoopGuardConfig(max_active_seconds=10), clock=self.clock)
            agent = Agent(Client(), registry, AgentConfig(model='fake'), cancellation=self.token,
                          execution_activity=self.activity, execution_budget=budget)
            self.assertIsNone(agent._parallel_kind(ToolUse('s', 'search_code', {'query': 'q'})))
            with patch('codeagent.code_search.vector.time', SimpleNamespace(time=self.clock, monotonic=self.clock)):
                self.assertEqual(agent.run('find seats').final_text, 'done')
            self.assertGreater(self.now, 125)
            self.assertEqual(budget.state.active_seconds, 0)
            self.assertEqual(budget.state.model_calls, 2)
            self.assertFalse(self.token.is_cancelled)
            with self.assertRaisesRegex(CancelledError, 'worker_heartbeat_timeout'):
                with self.activity.operation('tool', self.now + 125):
                    self.now += 126

    def test_rewrite_inherits_exemption_but_next_model_does_not(self):
        with self.activity.operation('tool', float('inf'), unlimited_time=True):
            with self.activity.model_request():
                self.now += 7200
                self.assertEqual(self.activity.request_timeout(), float('inf'))
                self.assertIsNone(self.activity.timeout_reason(120))
        with self.assertRaises(ModelCallTimeout):
            with self.activity.model_request():
                self.now += 301

    def test_search_scope_still_honors_manual_cancellation(self):
        with self.assertRaisesRegex(CancelledError, 'user cancelled'):
            with self.activity.operation('tool', float('inf'), unlimited_time=True):
                self.now += 7200
                self.token.cancel('user cancelled')
                self.activity.check()

    def test_unbounded_embedding_disables_http_timeout_and_legacy_config_limit(self):
        provider = EmbeddingConfig(enabled=True, base_url='https://example.invalid', model='fake',
                                   dimensions=2, timeout_seconds=.001).create_provider()
        real_client = httpx.AsyncClient
        observed = []

        async def respond(request):
            observed.append(request.extensions['timeout'])
            await asyncio.sleep(.06)
            return httpx.Response(200, json={'data': [{'index': 0, 'embedding': [1., 0.]}]})

        with patch('codeagent.code_search.embedding.httpx.AsyncClient',
                   side_effect=lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw)):
            self.assertEqual(provider.embed(['seat'], check=lambda: None, timeout=float('inf')), [[1., 0.]])
            with self.assertRaises(TimeoutError):
                provider.embed(['seat'], check=lambda: None, timeout=1)
        self.assertTrue(all(value is None for value in observed[0].values()))

    def test_unbounded_embedding_cancels_inflight_request(self):
        provider = EmbeddingConfig(enabled=True, base_url='https://example.invalid', model='fake', dimensions=2).create_provider()
        real_client = httpx.AsyncClient
        closed = []

        async def waiting(request):
            self.token.cancel('user cancelled')
            try:
                await asyncio.Event().wait()
            finally:
                closed.append(True)

        with patch('codeagent.code_search.embedding.httpx.AsyncClient',
                   side_effect=lambda **kw: real_client(transport=httpx.MockTransport(waiting), **kw)):
            with self.assertRaisesRegex(CancelledError, 'user cancelled'):
                provider.embed(['seat'], check=self.token.raise_if_cancelled, timeout=float('inf'))
        self.assertEqual(closed, [True])

    def test_openai_rewrite_uses_http_no_deadline(self):
        observed = []

        def respond(request):
            observed.append(request.extensions['timeout'])
            self.now += 7200
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
                'message': {'content': '{"queries":["seat"]}'}}]})

        with tempfile.TemporaryDirectory() as directory, httpx.Client(transport=httpx.MockTransport(respond)) as http:
            client = OpenAIModelClient(base_url='https://example.invalid', sdk_client=http,
                stream=False, activity=self.activity, request_timeout=5)
            search = CodeSearch(directory, Path(directory) / 'index', client=client, model='fake')
            with self.activity.operation('tool', float('inf'), unlimited_time=True):
                self.assertEqual(search.rewrite('购买座位'), (['seat'], 'completed', 1))
            self.assertEqual(client.request_timeout, 5)
        self.assertTrue(all(value is None for value in observed[0].values()))

    def test_anthropic_rewrite_uses_sdk_no_deadline(self):
        sdk = FakeSdkClient(SimpleNamespace(stop_reason='end_turn', content=[]))
        client = AnthropicModelClient(sdk_client=sdk, activity=self.activity,
                                      request_timeout=float('inf'), stream=False)
        with self.activity.operation('tool', float('inf'), unlimited_time=True):
            client.create_message(model='fake', system='', messages=[], tools=[], max_tokens=768)
        self.assertIsNone(sdk.options[-1]['timeout'])

    def test_registry_exemption_is_private_and_preserves_other_tool_budgets(self):
        from codeagent.tools.base import ToolDefinition

        class Reader:
            definition = ToolDefinition('reader', 'reader', {})

            def run(self):
                return 'ok'

            def bind_runtime(self, **kwargs):
                self.remaining = kwargs['remaining_seconds']

        with tempfile.TemporaryDirectory() as directory:
            search = SearchCodeTool(directory, Path(directory) / 'index')
            reader = Reader()
            registry = ToolRegistry()
            registry.register(search)
            registry.register(reader)
            registry.bind_runtime(self.token.raise_if_cancelled, lambda: 17)
            self.assertEqual(search.service.context.remaining_seconds(), float('inf'))
            self.assertEqual(reader.remaining(), 17)
            self.assertNotIn('unlimited_time', registry.schemas()[0])
            self.assertIn('search_code', registry.read_only_copy())

    def test_index_cli_unlimited_default_and_explicit_deadline(self):
        from codeagent.code_search.__main__ import main

        for options in ([], ['--timeout', '1']):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as directory:
                index = MagicMock(coverage={}, incomplete=False, generation=0,
                                  backend='fake', notes=[], documents=[])

                def sync(check):
                    self.now += 3600
                    check()

                index.sync.side_effect = sync
                with patch('sys.argv', ['index', 'index', '--workspace', directory, *options]), \
                     patch('codeagent.code_search.__main__.CodeIndex', return_value=index), \
                     patch('codeagent.code_search.__main__.time', SimpleNamespace(monotonic=self.clock)), \
                     patch('builtins.print'):
                    if options:
                        from codeagent.runtime.execution import ExecutionStopped
                        with self.assertRaises(ExecutionStopped):
                            main()
                    else:
                        main()
                index.close_snapshot.assert_called_once()


if __name__ == '__main__':
    unittest.main()
