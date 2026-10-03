from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import json
import asyncio
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import httpx

from codeagent import Agent, AgentConfig, EnvironmentConfig, ModelResponse
from codeagent.code_search.embedding import EmbeddingConfig
from codeagent.context import ContextConfig, ContextManager
from codeagent.lsp import LspConfig, LspService, ServerDefinition
from codeagent.lsp.rpc import RpcClient
from codeagent.runtime.cancellation import CancelledError
from codeagent.runtime.execution import ExecutionStopped
from codeagent.tools.defaults import create_default_registry
from codeagent.tools.lsp import LspTool
from codeagent.tools.search_code import SearchCodeTool
from codeagent.tools.workspace import WorkspaceGuard

FIXTURE = Path(__file__).parent / 'fixtures' / 'lsp_server.py'


def lsp_config(mode='push', log=None, timeout=2):
    command = (sys.executable, str(FIXTURE), mode, *([str(log)] if log else []))
    return LspConfig(servers=(ServerDefinition('mock-python', ('.py',), 'python', command, startup_seconds=timeout / 2 if mode == 'stall_init' else 45.),),
                     timeout_seconds=timeout, feedback_seconds=timeout)


class SemanticProvider:
    provider = 'mock-local'
    model = 'one'
    dimensions = 2

    def __init__(self):
        self.calls = []

    def embed(self, texts, *, check, timeout):
        check()
        self.calls.append(list(texts))
        return [[1., 0.] if 'discard_pending' in t or '清除等待中的操作' in t else [0., 1.] for t in texts]


class IntelligenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / 'repo'
        self.root.mkdir()
        self.index = self.base / 'index'
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True, capture_output=True)

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def tool(self, provider=None):
        return SearchCodeTool(self.root, self.index, embedding_provider=provider)

    def test_semantic_retrieval_exact_priority_and_cache(self):
        self.write('queue.py', 'def discard_pending():\n    pending.clear()\n')
        self.write('other.py', 'def exact_name():\n    return "other"\n')
        provider = SemanticProvider()
        tool = self.tool(provider)
        result = json.loads(tool.run('清除等待中的操作'))
        self.assertEqual(result['retrieval_backend'], 'hybrid')
        self.assertEqual(result['results'][0]['symbol'], 'discard_pending')
        self.assertEqual(result['query_intent'], 'behavior')
        result = json.loads(tool.run('exact_name 清除等待中的操作'))
        self.assertEqual(result['results'][0]['symbol'], 'exact_name')
        self.assertEqual(result['query_intent'], 'symbol')
        # A fresh instance reuses persisted document vectors; only query is embedded.
        provider.calls.clear()
        self.tool(provider).run('清除等待中的操作')
        self.assertEqual(provider.calls, [['清除等待中的操作']])

    def test_changes_model_dimensions_and_chunk_hash_invalidate(self):
        path = self.write('a.py', 'def discard_pending():\n    return 1\n')
        provider = SemanticProvider()
        tool = self.tool(provider)
        tool.run('清除等待中的操作')
        stat = path.stat()
        path.write_text('def discard_pending():\n    return 2\n', encoding='utf-8')
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        provider.calls.clear()
        result = json.loads(tool.run('清除等待中的操作'))
        self.assertIn('return 2', result['results'][0]['quote'])
        self.assertEqual(len(provider.calls), 2)
        self.write('b.py', 'def discard_pending_other():\n    return 3\n')
        path.unlink()
        result = json.loads(tool.run('清除等待中的操作'))
        self.assertEqual([hit['path'] for hit in result['results']], ['b.py'])
        provider.model = 'two'
        provider.calls.clear()
        tool.run('清除等待中的操作')
        self.assertEqual(len(provider.calls), 2)
        with closing(sqlite3.connect(self.index / 'vectors-v2.sqlite3')) as conn:
            rows = conn.execute('SELECT identity,path FROM vectors').fetchall()
        self.assertTrue(all(path == 'b.py' for _, path in rows))
        self.assertEqual({json.loads(identity)[2] for identity, _ in rows}, {'one', 'two'})
        provider.dimensions = 3
        self.assertEqual(json.loads(tool.run('discard_pending_other behavior'))['vector_status'], 'failed')

    def test_failure_offline_and_control_flow(self):
        self.write('x.py', 'def lookup():\n    return 1\n')
        for error in (RuntimeError('offline'), ValueError('bad vectors')):
            provider = SemanticProvider()
            provider.embed = lambda *a, **kw: (_ for _ in ()).throw(error)
            result = json.loads(self.tool(provider).run('lookup behavior'))
            self.assertTrue(result['degraded'])
            self.assertEqual(result['retrieval_backend'], 'lexical')
            self.assertEqual(result['results'][0]['symbol'], 'lookup')
        for error in (CancelledError('cancelled'), ExecutionStopped('budget_exceeded:active_time')):
            provider.embed = lambda *a, **kw: (_ for _ in ()).throw(error)
            with self.assertRaises(type(error)):
                self.tool(provider).run('lookup behavior')
        with patch('codeagent.code_search.embedding.httpx.AsyncClient') as remote:
            self.assertIsNone(EmbeddingConfig().create_provider())
            self.tool().run('lookup')
            remote.assert_not_called()

    def test_quota_dedup_current_source_and_partial_vector_scan(self):
        source = '\n'.join(f'def item_{i}():\n    return "common"' for i in range(8))
        self.write('many.py', source)
        self.write('other.py', 'def item_other():\n    return "common"\n')
        result = json.loads(self.tool(SemanticProvider()).run('common', top_k=10))
        self.assertEqual(sum(r['path'] == 'many.py' for r in result['results']), 3)
        self.assertIn('other.py', [r['path'] for r in result['results']])
        for hit in result['results']:
            lines = (self.root / hit['path']).read_text().splitlines()
            self.assertEqual(hit['quote'], '\n'.join(lines[hit['line']-1:hit['end_line']]))
        tool = self.tool(SemanticProvider())
        tool.service.index.directory = self.index / 'partial'
        tool.service.max_vector_chunks = 1
        self.assertEqual(json.loads(tool.run('common'))['vector_status'], 'partial')

    def test_scope_ignore_workspace_identity_and_concurrent_indices(self):
        self.write('.gitignore', 'hidden.py\n')
        self.write('hidden.py', 'def discard_pending():\n    return 1\n')
        self.write('visible.py', 'def visible():\n    return 1\n')
        provider = SemanticProvider()
        tool = self.tool(provider)
        self.assertEqual(json.loads(tool.run('清除等待中的操作'))['results'], [])
        self.assertNotIn('hidden.py', str(provider.calls))
        self.assertTrue(str(tool.run('visible', path='..')).startswith('Error:'))
        other = self.base / 'other'
        other.mkdir()
        (other / 'b.py').write_text('def distinct():\n    return 2\n')
        result = json.loads(SearchCodeTool(other, self.index, embedding_provider=provider).run('distinct'))
        self.assertEqual(result['results'][0]['symbol'], 'distinct')
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: json.loads(self.tool().run('visible')), range(12)))
        self.assertTrue(all(r['results'][0]['symbol'] == 'visible' for r in results))
        with closing(sqlite3.connect(self.index / 'source-v2.sqlite3')) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM chunks').fetchone()[0], 1)

    def service(self, mode='push', timeout=2):
        service = LspService(self.root, lsp_config(mode, self.base / 'rpc.log', timeout))
        self.addCleanup(service.close)
        return service

    def test_lsp_protocol_sync_locations_diagnostics_and_release(self):
        path = self.write('space 名.py', 'BROKEN = 1\n')
        service = self.service()
        report = service.execute('diagnostics', path)
        self.assertEqual(report['status'], 'ok')
        self.assertTrue(report['diagnostics_versioned'])
        self.assertIn('simulated diagnostic', report['diagnostics'][0]['message'])
        rpc = service.sessions['mock-python']['rpc']
        for operation in ('definition', 'references'):
            result = service.execute(operation, path)
            self.assertEqual(result['locations'][0]['path'], path.name)
            self.assertEqual(result['locations'][0]['line'], 1)
        path.write_text('okay = "😀"\n', encoding='utf-8')
        report = service.execute('diagnostics', path)
        self.assertEqual(report['diagnostics'], [])
        self.assertEqual(report['version'], 2)
        path.unlink()
        self.assertEqual(service.execute('sync', path)['status'], 'deleted')
        service.close()
        self.assertIsNotNone(rpc.process.poll())
        self.assertFalse(rpc.reader.is_alive())
        self.assertFalse(rpc.writer.is_alive())
        messages = [json.loads(line) for line in (self.base / 'rpc.log').read_text(encoding='utf-8').splitlines()]
        self.assertTrue(any(m.get('id') == 'server-config' and m.get('result') == [{}] for m in messages))
        changed = next(m for m in messages if m.get('method') == 'textDocument/didChange')
        self.assertIn('range', changed['params']['contentChanges'][0])
        self.assertIn('textDocument/didClose', [m.get('method') for m in messages])

    def test_lsp_pull_missing_timeout_start_failure_and_process_release(self):
        path = self.write('x.py', 'BROKEN = 1\n')
        pulled = self.service('pull').execute('diagnostics', path)
        self.assertEqual(pulled['status'], 'ok')
        self.assertFalse(pulled['diagnostics_versioned'])
        self.assertEqual(pulled['diagnostic_freshness'], 'requested_after_sync')
        self.assertEqual(self.service('options').execute('definition', path)['status'], 'ok')
        missing = LspService(self.root, LspConfig(servers=()))
        self.assertEqual(missing.execute('diagnostics', path)['status'], 'unavailable')
        for mode in ('stall_init', 'stall_request', 'silent', 'exit', 'malformed'):
            service = self.service(mode, timeout=.3)
            clients = []
            def capture(*args):
                rpc = RpcClient(*args)
                clients.append(rpc)
                return rpc
            with patch('codeagent.lsp.service.RpcClient', side_effect=capture):
                started = time.monotonic()
                report = service.execute('diagnostics' if mode == 'silent' else 'definition', path)
            self.assertIn(report['status'], ('timeout', 'unavailable'), mode)
            self.assertLess(time.monotonic() - started, 2)
            self.assertTrue(all(rpc.process.poll() is not None for rpc in clients))
            self.assertEqual(service.sessions, {})

    def test_lsp_cancellation_budget_and_boundary(self):
        path = self.write('x.py', 'x = 1\n')
        for stop in (CancelledError('cancel'), ExecutionStopped('budget_exceeded:active_time')):
            service = self.service('stall_request')
            service.execute('sync', path)
            rpc = service.sessions['mock-python']['rpc']
            start = time.monotonic()
            def check():
                if time.monotonic() - start > .1:
                    raise stop
            service.bind_runtime(cancellation_check=check)
            with self.assertRaises(type(stop)):
                service.execute('definition', path)
            self.assertIsNotNone(rpc.process.poll())
        with self.assertRaises(ValueError):
            self.service().execute('sync', '../outside.py')

    def test_agent_edit_diagnostic_enters_next_model_request_and_closes(self):
        self.write('edit.py', 'okay = 1\n')
        registry = create_default_registry(workspace_guard=WorkspaceGuard(self.root),
            code_search_tool=self.tool(), lsp_config=lsp_config('push'))
        class Client:
            def __init__(inner):
                inner.calls = []
            def create_message(inner, **kwargs):
                inner.calls.append(deepcopy(kwargs))
                if len(inner.calls) == 1:
                    return ModelResponse('tool_use', [{'type': 'tool_use', 'id': 'edit', 'name': 'edit_file',
                        'input': {'file_path': 'edit.py', 'old_string': 'okay', 'new_string': 'BROKEN'}}])
                return ModelResponse('end_turn', [{'type': 'text', 'text': 'done'}])
        client = Client()
        context = ContextManager(config=ContextConfig(mode='off', transcript_dir=self.base / 'history', tool_output_dir=self.base / 'outputs'))
        agent = Agent(client=client, tools=registry, config=AgentConfig('mock', loop_guard=None),
                      context=context, allow_subagents=False)
        agent.run('edit')
        result = next(block for message in client.calls[1]['messages'] for block in message.get('content', [])
                      if isinstance(block, dict) and block.get('type') == 'tool_result')
        self.assertIn('simulated diagnostic', result['content'])
        self.assertIn('edit_diagnostics', result['content'])
        self.assertTrue(next(t for t in registry.owners() if isinstance(t, SearchCodeTool)).service._invalidated)
        self.assertEqual(next(t for t in registry.owners() if isinstance(t, LspTool)).service.sessions, {})

    def test_registry_child_instances_runtime_and_lsp_documents_are_isolated(self):
        self.write('x.py', 'x = 1\n')
        parent = create_default_registry(code_search_tool=self.tool(), lsp_config=lsp_config())
        child = parent.read_only_copy()
        self.assertIn('search_code', child)
        self.assertIn('lsp', child)
        self.assertNotIn('write_file', child)
        parent_search = next(t for t in parent.owners() if isinstance(t, SearchCodeTool))
        child_search = next(t for t in child.owners() if isinstance(t, SearchCodeTool))
        self.assertIsNot(parent_search.service, child_search.service)
        parent.bind_runtime(lambda: (_ for _ in ()).throw(CancelledError()), lambda: 1)
        child.bind_runtime(lambda: None, lambda: 2)
        self.assertEqual(json.loads(child.execute('search_code', {'query': 'x'}))['results'][0]['path'], 'x.py')
        with self.assertRaises(CancelledError):
            parent.execute('search_code', {'query': 'x'})
        self.addCleanup(child.close_workspace_tools)
        self.assertEqual(json.loads(child.execute('lsp', {'operation': 'sync', 'file_path': 'x.py'}))['status'], 'ok')
        parent_lsp = next(t for t in parent.owners() if isinstance(t, LspTool))
        self.assertEqual(parent_lsp.service.sessions, {})

    def test_remote_provider_mock_http_order_deadline_and_cancellation(self):
        config = EmbeddingConfig(enabled=True, base_url='https://embedding.invalid/v1', model='test', dimensions=2)
        provider = config.create_provider()
        real_client = httpx.AsyncClient
        requests = []
        async def respond(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={'data': [
                {'index': 1, 'embedding': [0, 1]}, {'index': 0, 'embedding': [1, 0]}]})
        with patch('codeagent.code_search.embedding.httpx.AsyncClient',
                   side_effect=lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw)):
            self.assertEqual(provider.embed(['a', 'b'], check=lambda: None, timeout=1), [[1, 0], [0, 1]])
        self.assertEqual(requests[0], {'model': 'test', 'input': ['a', 'b'], 'encoding_format': 'float'})
        closed = []
        async def delayed(request):
            try:
                await asyncio.sleep(20)
            finally:
                closed.append(True)
        with patch('codeagent.code_search.embedding.httpx.AsyncClient',
                   side_effect=lambda **kw: real_client(transport=httpx.MockTransport(delayed), **kw)):
            with self.assertRaises(TimeoutError):
                provider.embed(['x'], check=lambda: None, timeout=.1)
            for error in (CancelledError('stop'), ExecutionStopped('budget_exceeded:active_time')):
                start = time.monotonic()
                def check():
                    if time.monotonic() - start >= .1:
                        raise error
                with self.assertRaises(type(error)):
                    provider.embed(['x'], check=check, timeout=5)
                self.assertLess(time.monotonic() - start, 1)
        self.assertEqual(len(closed), 3)

    def test_remote_batch_limit_and_explicit_hybrid_eval_factory(self):
        from codeagent.tools.search_eval import build_tool, build_hybrid_tool
        settings = {'CODEAGENT_EMBEDDING_ENABLED': 'true',
                    'CODEAGENT_EMBEDDING_BASE_URL': 'https://embedding.invalid/v1',
                    'CODEAGENT_EMBEDDING_MODEL': 'test', 'CODEAGENT_EMBEDDING_DIMENSIONS': '2',
                    'CODEAGENT_EMBEDDING_BATCH_SIZE': '10', 'API_KEY': 'chat-only'}
        args = dict(workspace=self.root, index_dir=self.index, client=None, model=None, event_emitter=None)
        with patch.dict(os.environ, settings, clear=True):
            self.assertIsNone(build_tool(**args).service.embedding_provider)
            hybrid = build_hybrid_tool(**args)
            provider = hybrid.service.embedding_provider
            self.assertEqual(hybrid.embedding_config.api_key, '')
            self.assertEqual(hybrid.embedding_config.batch_size, 10)
            seen = []
            real_client = httpx.AsyncClient

            async def respond(request):
                inputs = json.loads(request.content)['input']
                seen.append(inputs)
                self.assertLessEqual(len(inputs), 10)
                return httpx.Response(200, json={'data': [
                    {'index': i, 'embedding': [float(value), 1.]} for i, value in enumerate(inputs)]})

            with patch('codeagent.code_search.embedding.httpx.AsyncClient',
                       side_effect=lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw)):
                vectors = provider.embed([str(i) for i in range(23)], check=lambda: None, timeout=2)
            self.assertEqual([len(batch) for batch in seen], [10, 10, 3])
            self.assertEqual([v[0] for v in vectors], list(range(23)))
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, 'ENABLED'):
                build_hybrid_tool(**args)

    def test_lsp_resyncs_other_open_documents_after_external_edit(self):
        a = self.write('a.py', 'a = 1\n')
        b = self.write('b.py', 'b = 1\n')
        service = self.service()
        service.execute('sync', a)
        service.execute('sync', b)
        a.write_text('a = 2\n')
        service.execute('definition', b)
        self.assertEqual(service.sessions['mock-python']['documents'][a.as_uri()][2], a.read_bytes().decode())

    def test_cli_subagent_and_web_factories_bind_actual_workspaces(self):
        from codeagent.cli import create_default_subagent_environment
        from codeagent.events import EventEmitter, ExecutionContext
        from codeagent.memory import MemoryConfig
        from codeagent.permissions import WaitingPermissionBroker
        from codeagent.prompts import PromptMode
        from codeagent.runtime import CancellationToken
        from codeagent.web.factory import WebAgentFactory
        from codeagent.web.storage import SQLiteRepository
        env = EnvironmentConfig(model_id='mock', data_dir=self.base / 'data', enable_skills=False,
            context_config=ContextConfig(mode='off'), memory_config=MemoryConfig(enabled=False),
            lsp_config=lsp_config())
        cli_tools, _, _ = create_default_subagent_environment(self.root, None, None, env, self.base / 'context')
        self.assertIn('search_code', cli_tools)
        self.assertIn('lsp', cli_tools)
        self.assertEqual(next(t for t in cli_tools.owners() if isinstance(t, LspTool)).service.guard.root, self.root)
        repository = SQLiteRepository(self.base / 'state.db', recover_incomplete=False)
        self.addCleanup(repository.close)
        conversation = repository.create_conversation(workspace=str(self.root))
        factory = WebAgentFactory(env, self.root, repository)
        self.addCleanup(factory.close)
        kwargs = dict(event_emitter=EventEmitter(context=ExecutionContext(conversation_id=conversation.id, run_id='intelligence-test')),
                      cancellation=CancellationToken(), permission_broker=WaitingPermissionBroker())
        self.write('a.py', 'def main_only():\n    return 1\n')
        subprocess.run(['git', '-C', str(self.root), 'add', 'a.py'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                        'commit', '-qm', 'fixture'], check=True, capture_output=True)
        worktree = self.base / 'worktree'
        subprocess.run(['git', '-C', str(self.root), 'worktree', 'add', '--detach', str(worktree)], check=True, capture_output=True)
        (worktree / 'a.py').write_text('def branch_only():\n    return 2\n')
        with patch.object(EnvironmentConfig, 'create_anthropic_client', return_value=object()):
            normal = factory.create(**kwargs)
            planner = factory.create(**kwargs, root_prompt_mode=PromptMode.TEAM_PLANNER)
            isolated = factory.for_workspace(worktree).create(**kwargs)
        self.assertIn('search_code', planner.tools)
        self.assertIn('lsp', planner.tools)
        self.assertNotIn('write_file', planner.tools)
        for agent, workspace in ((normal, self.root), (isolated, worktree)):
            sub_tools = agent._subagent_environment()[0]
            self.assertIn('search_code', sub_tools)
            self.assertIn('lsp', sub_tools)
            self.assertEqual(next(t for t in agent.tools.owners() if isinstance(t, LspTool)).service.guard.root, workspace)
        parent_search = next(t for t in normal.tools.owners() if isinstance(t, SearchCodeTool))
        isolated_search = next(t for t in isolated.tools.owners() if isinstance(t, SearchCodeTool))
        self.assertNotEqual(parent_search.service.index.directory, isolated_search.service.index.directory)
        self.assertNotIn('branch_only', [r['symbol'] for r in json.loads(parent_search.run('branch_only'))['results']])
        self.assertEqual(json.loads(isolated_search.run('branch_only'))['results'][0]['symbol'], 'branch_only')

    def test_team_wrapper_preserves_edit_facts_and_diagnostic_feedback(self):
        from types import SimpleNamespace
        from codeagent.teams.tool_gate import TeamToolExecutionGate
        from codeagent.hooks.code_intelligence import CodeIntelligenceFeedback
        from codeagent.messages import ToolUse
        from codeagent.teams.models import TaskAttemptState
        self.write('x.py', 'okay = 1\n')
        attempt = SimpleNamespace(state=TaskAttemptState.RUNNING, write_enabled=True)
        binding = SimpleNamespace(id='binding', path=str(self.root), write_scopes=('.',), write_enabled=True)
        from unittest.mock import Mock
        repository = Mock()
        repository.get_task_attempt.return_value = attempt
        repository.get_attempt_worktree_binding.return_value = binding
        manager = Mock()
        manager.validate_binding.return_value = binding
        gate = TeamToolExecutionGate(repository, manager, 'attempt')
        registry = create_default_registry(workspace_guard=WorkspaceGuard(self.root), code_search_tool=self.tool(), lsp_config=lsp_config())
        wrapped = gate.wrap(registry)
        self.addCleanup(wrapped.close_workspace_tools)
        with patch.object(gate, '_verify_active_leases'), patch.object(gate, '_check_file_target'), \
             patch.object(gate, '_outside_scope_changes', return_value=[]):
            output = wrapped.execute('edit_file', {'file_path': 'x.py', 'old_string': 'okay', 'new_string': 'BROKEN'})
        self.assertEqual(output.status, 'success')
        self.assertEqual(output.changed_files, (str(self.root / 'x.py'),))
        CodeIntelligenceFeedback(lambda: wrapped)(ToolUse(id='edit', name='edit_file', input={}), output)
        self.assertIn('simulated diagnostic', output.context_feedback)

    def test_cli_entry_registers_search_lsp_and_explicit_config(self):
        import contextlib
        import io
        from codeagent.cli import main
        from codeagent.memory import MemoryConfig
        # Preserve the pre-existing generic Tool injection contract.
        custom = type('CustomSearch', (), {'definition': SearchCodeTool.definition,
                      'run': lambda self, **kwargs: 'custom'})()
        custom_registry = create_default_registry(code_search_tool=custom)
        self.assertEqual(custom_registry.execute('search_code', {'query': 'q'}), 'custom')
        self.assertNotIn('lsp', custom_registry)
        env = EnvironmentConfig(model_id='mock', data_dir=self.base / 'data', enable_skills=False,
            context_config=ContextConfig(mode='off'), memory_config=MemoryConfig(enabled=False),
            lsp_config=LspConfig(enabled=False))
        calls = []
        class Client:
            def create_message(inner, **kwargs):
                calls.append(kwargs)
                return ModelResponse('end_turn', [{'type': 'text', 'text': 'done'}])
        with patch('codeagent.cli.EnvironmentConfig.from_env', return_value=env), \
             patch.object(EnvironmentConfig, 'create_anthropic_client', return_value=Client()), \
             patch('codeagent.cli.Path.cwd', return_value=self.root), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--no-stream', 'inspect']), 0)
        self.assertTrue({'search_code', 'lsp'} <= {tool['name'] for tool in calls[0]['tools']})
        with patch('codeagent.config._load_dotenv'), patch.dict(os.environ, {
            'MODEL_ID': 'mock', 'CONTEXT_COMPACT_MODE': 'off', 'OPENAI_API_KEY': 'unrelated-test-key',
            'CODEAGENT_PYTHON_LSP_COMMAND': '["custom-python-server", "--stdio"]',
        }, clear=True):
            config = EnvironmentConfig.from_env()
            self.assertFalse(config.embedding_config.enabled)
            self.assertEqual(config.embedding_config.api_key, '')
            self.assertEqual(config.lsp_config.servers[0].command, ('custom-python-server', '--stdio'))

    def test_independent_processes_share_only_transactional_index(self):
        self.write('a.py', 'def shared_symbol():\n    return 1\n')
        program = '''import json, sys
from pathlib import Path
from codeagent.tools.search_code import SearchCodeTool
tool = SearchCodeTool(Path(sys.argv[1]), Path(sys.argv[2]))
for _ in range(3):
    value = json.loads(tool.run('shared_symbol'))
    assert value['results'][0]['symbol'] == 'shared_symbol', value
'''
        children = [subprocess.Popen([sys.executable, '-c', program, str(self.root), str(self.index)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
        try:
            for child in children:
                stdout, stderr = child.communicate(timeout=30)
                self.assertEqual(child.returncode, 0, (stdout, stderr))
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.communicate()
        with closing(sqlite3.connect(self.index / 'source-v2.sqlite3')) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM chunks').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM identity').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
