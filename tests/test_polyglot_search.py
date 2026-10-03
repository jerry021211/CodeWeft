"""Multi-language acceptance tests; embedding vectors are deterministic fakes."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from copy import deepcopy

from codeagent.code_intelligence.languages import analyze, language_for
from codeagent.code_intelligence.models import QueryContext
from codeagent.code_search.vector import VectorIndex
from codeagent.code_search.embedding import EmbeddingBatch
from codeagent.tools.search_code import SearchCodeTool
from codeagent.tools.defaults import create_default_registry
from codeagent.tools.workspace import WorkspaceGuard
from codeagent.lsp import LspService, LspConfig, ServerDefinition
from codeagent.lsp.projects import project_root, server_command
from codeagent.lsp.registry import configured_servers
from codeagent.runtime.cancellation import CancelledError

FIXTURE = Path(__file__).parent / 'fixtures' / 'lsp_server.py'


class Provider:
    provider, model, dimensions = 'fake', 'one', 2

    def __init__(self):
        self.calls = []

    def embed(self, texts, *, check, timeout):
        check()
        self.calls.append(texts)
        return EmbeddingBatch([[1., .1] if 'refund' in t or '退款' in t else [.1, 1.] for t in texts],
                              {'input_tokens': sum(len(t) for t in texts)})


class PolyglotTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / 'repo'
        self.root.mkdir()
        self.cache = self.base / 'cache'

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
        return target

    def tool(self, provider=None):
        return SearchCodeTool(self.root, self.cache, embedding_provider=provider)

    def test_java_overloads_constructors_annotations_and_locations(self):
        path = self.write('src/Orders.java', 'package demo;\n/** orders */\npublic class Orders {\n'
            ' private int count;\n public Orders() {}\n @Deprecated\n public void refund(\n String id) { count++; }\n'
            ' public void refund(int id) { count--; }\n}\n')
        tool = self.tool()
        hits = json.loads(tool.run('Orders.refund'))['results']
        methods = [h for h in hits if h['symbol'] == 'Orders.refund']
        self.assertEqual(len(methods), 2)
        self.assertEqual(len({h['entity_id'] for h in methods}), 2)
        self.assertTrue(all(h['parser'] == 'tree_sitter' and h['language'] == 'java' for h in methods))
        for hit in hits:
            self.assertEqual(hit['quote'], '\n'.join(path.read_text().splitlines()[hit['line'] - 1:hit['end_line']]))
        docs = analyze(path.name, path.read_bytes())
        self.assertTrue({'class', 'field', 'constructor', 'method'} <= {d['kind'] for d in docs})

    def test_ts_js_tsx_and_python_entities(self):
        cases = {'card.tsx': 'export function Card(){ return <div>😀</div>; }',
                 'api.ts': 'export interface Item { id: string; }\nexport type ID = string;\nexport const refund = (id: ID) => id;',
                 'app.jsx': 'export const View = () => <span>Hello</span>;',
                 'models.py': 'class Item:\n    """inventory"""\n    id: str\n    def refund(self):\n        return self.id\n'}
        for name, text in cases.items():
            with self.subTest(name=name):
                docs = analyze(name, text.encode())
                self.assertTrue(docs)
                self.assertTrue(all(d['parse_quality'] == 'syntax' for d in docs))
        self.assertTrue({'Item', 'Item.id', 'Item.refund'} <= {d['symbol'] for d in analyze('x.py', cases['models.py'].encode())})

    def test_missing_grammar_and_text_capabilities(self):
        with patch('codeagent.code_intelligence.languages.importlib.import_module', side_effect=ImportError):
            docs = analyze('X.java', b'class X {}')
        self.assertEqual(docs[0]['parse_quality'], 'fallback')
        self.assertEqual(analyze('mapper.xml', b'<select>select * from orders</select>')[0]['parse_quality'], 'text')
        self.assertIsNone(language_for('.env'))
        self.assertEqual(analyze('X.java', 'class X { int count; }'.encode('utf-16'))[0]['symbol'], 'X')

    def test_insert_lines_reuses_embeddings_and_updates_evidence(self):
        path = self.write('Orders.java', 'class Orders {\n void refund() { System.out.println("refund"); }\n}\n')
        provider = Provider()
        tool = self.tool(provider)
        before = json.loads(tool.run('订单退款'))
        identity = next(h['entity_id'] for h in before['results'] if h['symbol'] == 'Orders.refund')
        path.write_text('\n\n' + path.read_text(), encoding='utf-8')
        provider.calls.clear()
        after = json.loads(tool.run('订单退款'))
        self.assertEqual(provider.calls, [['订单退款']])
        hit = next(h for h in after['results'] if h['symbol'] == 'Orders.refund')
        self.assertEqual(hit['entity_id'], identity)
        self.assertEqual(hit['line'], 4)
        self.assertEqual(hit['content_hash'], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertTrue(after['embedding_usage']['usage_complete'])

    def test_exact_symbol_does_not_build_or_call_remote(self):
        self.write('X.java', 'class X { void refund() {} }')
        provider = Provider()
        result = json.loads(self.tool(provider).run('X.refund'))
        self.assertEqual(result['vector_status'], 'skipped_exact')
        self.assertEqual(provider.calls, [])

    def test_vector_budget_progresses_past_prefix_and_model_switch(self):
        self.write('jobs.py', '\n'.join(f'def job_{i}():\n    return {i}\n' for i in range(7)))
        provider = Provider()
        tool = self.tool(provider)
        tool.service.max_vector_chunks = 2
        ready = []
        for _ in range(4):
            ready.append(json.loads(tool.run('background processing'))['embedding_usage']['ready'])
        self.assertEqual(ready, [2, 4, 6, 7])
        provider.model = 'two'
        switched = json.loads(tool.run('background processing'))
        self.assertEqual(switched['embedding_usage']['cache_hits'], 0)
        self.assertEqual(switched['embedding_usage']['ready'], 2)

    def test_corrupt_cached_vector_is_rebuilt(self):
        self.write('Order.java', 'class Order {\n void refund() {}\n}\n')
        provider = Provider()
        tool = self.tool(provider)
        first = json.loads(tool.run('退款流程'))
        with closing(sqlite3.connect(self.cache / 'vectors-v2.sqlite3')) as conn:
            conn.execute("UPDATE vectors SET vector='[0,0]'")
            conn.commit()
        repaired = json.loads(tool.run('退款流程'))
        self.assertTrue(repaired['vector_complete'])
        self.assertEqual(repaired['embedding_usage']['embedded'], first['embedding_usage']['eligible'])
        self.assertEqual(repaired['embedding_usage']['cache_hits'], 0)

    def test_more_than_2000_blocks_eventually_covered(self):
        self.write('jobs.py', '\n'.join(f'def task_{i}():\n    return {i}\n' for i in range(2005)))
        tool = self.tool(Provider())
        first = json.loads(tool.run('background execution'))
        second = json.loads(tool.run('background execution'))
        self.assertEqual(first['embedding_usage']['ready'], 2000)
        self.assertEqual(second['embedding_usage']['ready'], 2005)
        self.assertTrue(second['vector_complete'])

    def test_generation_snapshot_and_no_parse_under_write_lock(self):
        path = self.write('x.py', 'def refund():\n    return 1\n')
        tool = self.tool()
        tool.run('refund')
        from codeagent.code_search import coordinator
        original = coordinator.analyze
        def parse(*args):
            with closing(sqlite3.connect(self.cache / 'source-v2.sqlite3', timeout=.01)) as conn:
                conn.execute('BEGIN IMMEDIATE')
            return original(*args)
        path.write_text('def refund():\n    return 2\n')
        with patch.object(coordinator, 'analyze', side_effect=parse):
            old = tool.service.index
            old.sync(lambda: None)
        self.addCleanup(old.close_snapshot)
        old_generation = old.generation
        path.write_text('def replacement():\n    return 3\n')
        newer = json.loads(self.tool().run('replacement'))
        self.assertGreater(newer['index_generation'], old_generation)
        self.assertTrue(old.rank('refund', '.', lambda: None))
        self.assertFalse(old.rank('replacement', '.', lambda: None))

    def test_identical_concurrent_publication_does_not_mark_scan_incomplete(self):
        self.write('same.py', 'def refund():\n    return 1\n')
        first, second = self.tool(), self.tool()
        from codeagent.code_search import coordinator
        original = coordinator.analyze
        triggered = False
        def interleaved(*args):
            nonlocal triggered
            if not triggered:
                triggered = True
                second.service.index.sync(lambda: None)
                second.service.index.close_snapshot()
            return original(*args)
        with patch.object(coordinator, 'analyze', side_effect=interleaved):
            result = json.loads(first.run('refund'))
        self.assertTrue(result['scan_complete'], result)
        self.assertEqual(result['results'][0]['symbol'], 'refund')

    def test_cross_instance_embedding_claims_and_request_cancellation(self):
        self.write('x.py', 'def refund():\n    return 1\n')
        provider = Provider()
        original = provider.embed
        def slow(*args, **kwargs):
            time.sleep(.03)
            return original(*args, **kwargs)
        provider.embed = slow
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: json.loads(self.tool(provider).run('退款流程')), range(2)))
        inputs = [t for batch in provider.calls for t in batch if 'def refund' in t]
        self.assertEqual(len(inputs), 1)
        self.assertTrue(all(r['scan_complete'] for r in results))
        tool = self.tool()
        def cancel():
            raise CancelledError('test')
        with self.assertRaises(CancelledError):
            tool.service.search('refund', context=QueryContext(cancellation_check=cancel))
        self.assertTrue(json.loads(tool.run('refund'))['results'])

    def test_lsp_project_roots_settings_and_slow_start_recovery(self):
        self.write('frontend/package.json', '{}')
        path = self.write('frontend/src/app.tsx', 'const x = <span />;\n')
        log = self.base / 'rpc.jsonl'
        server = ServerDefinition('mock-ts', ('.tsx',), 'typescript', (sys.executable, str(FIXTURE), 'slow_init', str(log)),
                                  settings={'python': {'analysis': {'strict': True}}})
        service = LspService(self.root, LspConfig(servers=(server,), timeout_seconds=2))
        self.addCleanup(service.close)
        first = service.execute('sync', path, timeout=.05)
        self.assertEqual(first['status'], 'initializing')
        process = next(iter(service.sessions.values()))['rpc'].process
        result = service.execute('diagnostics', path)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['project_root'], 'frontend')
        self.assertIs(next(iter(service.sessions.values()))['rpc'].process, process)
        service.close()
        self.assertIsNotNone(process.poll())
        messages = [json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()]
        opened = next(m for m in messages if m.get('method') == 'textDocument/didOpen')
        self.assertEqual(opened['params']['textDocument']['languageId'], 'typescriptreact')
        answer = next(m for m in messages if m.get('id') == 'server-config')
        self.assertEqual(answer['result'], [{'analysis': {'strict': True}}])

    def test_java_project_root_and_worktree_data_isolation(self):
        self.write('pom.xml', '<project/>')
        self.write('module/pom.xml', '<project/>')
        path = self.write('module/src/Order.java', 'class Order {}')
        guard = WorkspaceGuard(self.root)
        root = project_root(guard, path, 'java')
        self.assertEqual(root, self.root)
        server = ServerDefinition('jdtls', ('.java',), 'java', ('jdtls',))
        with patch.dict(os.environ, {'CODEAGENT_DATA_DIR': str(self.base / 'data')}):
            first = server_command(server, self.root, root, 'agent1')
            second = server_command(server, self.root, root, 'agent2')
            other = server_command(server, self.base / 'worktree', root, 'agent1')
        self.assertEqual(first[-2], '-data')
        self.assertEqual(len({first[-1], second[-1], other[-1]}), 3)

    def test_stalled_startup_is_reaped_without_another_request(self):
        path = self.write('wait.py', 'x = 1\n')
        server = ServerDefinition('stall', ('.py',), 'python',
            (sys.executable, str(FIXTURE), 'stall_init'), startup_seconds=.3)
        service = LspService(self.root, LspConfig(servers=(server,)))
        self.addCleanup(service.close)
        result = service.execute('sync', path, timeout=.05)
        self.assertEqual(result['status'], 'initializing')
        process = next(iter(service.sessions.values()))['rpc'].process
        process.wait(timeout=3)
        with service.lock:
            self.assertFalse(service.sessions)

    def test_window_baseline_and_syntax_share_inventory_but_not_cached_parser(self):
        from codeagent.tools.search_eval import build_tool, build_window_tool
        self.write('Order.java', 'class Order {\n void refund() {}\n}\n')
        args = dict(workspace=self.root, index_dir=self.cache, client=None, model=None, event_emitter=None)
        baseline = build_window_tool(**args)
        baseline_result = json.loads(baseline.run('refund'))
        self.assertTrue(all(d['parse_quality'] == 'text' for d in baseline_result['results']))
        syntax = build_tool(**args)
        result = json.loads(syntax.run('Order.refund'))
        self.assertEqual(result['results'][0]['symbol'], 'Order.refund')
        self.assertEqual(result['coverage']['eligible_files'], baseline_result['coverage']['eligible_files'])
        self.assertTrue(any(d['parse_quality'] != 'text' for d in result['results']))

    def test_lsp_registration_independent_of_search_and_env(self):
        registry = create_default_registry(workspace_guard=WorkspaceGuard(self.root))
        self.assertIn('lsp', registry)
        with patch.dict(os.environ, {'CODEAGENT_JAVA_LSP_COMMAND': '["custom-java","--stdio"]',
                                    'CODEAGENT_JAVA_LSP_SETTINGS': '{"java":{"home":"runtime"}}'}):
            server = next(s for s in configured_servers() if s.name == 'jdtls')
        self.assertEqual(server.command, ('custom-java', '--stdio'))
        self.assertEqual(server.settings['java']['home'], 'runtime')

    def test_dynamic_lsp_capabilities_and_encoded_windows_drive_uri(self):
        path = self.write('X.java', 'class X {}\n')
        server = ServerDefinition('dynamic-java', ('.java',), 'java', (sys.executable, str(FIXTURE), 'dynamic'))
        service = LspService(self.root, LspConfig(servers=(server,)))
        self.addCleanup(service.close)
        self.assertEqual(service.execute('definition', path)['status'], 'ok')
        self.assertEqual(service.execute('references', path)['status'], 'ok')
        if os.name == 'nt':
            uri = path.as_uri().replace(':', '%3A', 1)  # Keep scheme intact below.
            uri = path.as_uri().replace(path.drive[0] + ':', path.drive[0].lower() + '%3A')
            items, omitted = service._locations([{'uri': uri, 'range': {
                'start': {'line': 0, 'character': 0}, 'end': {'line': 0, 'character': 3}}}])
            self.assertEqual(omitted, 0)
            self.assertEqual(items[0]['path'], 'X.java')

    @unittest.skipUnless(os.name == 'nt', 'Windows job ownership regression')
    def test_shutdown_reaps_descendant_after_parent_gracefully_exits(self):
        from codeagent.lsp.rpc import RpcClient
        import ctypes as c
        from ctypes import wintypes as w
        log = self.base / 'child.jsonl'
        rpc = RpcClient((sys.executable, str(FIXTURE), 'child', str(log)), self.root, lambda *args: None)
        self.addCleanup(rpc.close)
        rpc.request('initialize', {}, time.monotonic() + 3, lambda: None)
        pid = next(json.loads(line)['child_pid'] for line in log.read_text().splitlines() if 'child_pid' in line)
        kernel = c.WinDLL('kernel32')
        kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
        kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        kernel.CloseHandle.argtypes = [w.HANDLE]
        handle = kernel.OpenProcess(0x00100000, False, pid)
        self.assertTrue(handle)
        try:
            rpc.close()
            self.assertEqual(kernel.WaitForSingleObject(handle, 1000), 0)
        finally:
            kernel.CloseHandle(handle)

    def test_java_edit_feedback_enters_agent_context_and_child_rewrite_is_bound(self):
        from codeagent import Agent, AgentConfig, ModelResponse
        from codeagent.context import ContextConfig, ContextManager
        from codeagent.tools.lsp import LspTool
        self.write('Order.java', 'class Order { int okay; }\n')
        server = ServerDefinition('mock-java', ('.java',), 'java', (sys.executable, str(FIXTURE), 'push'))
        registry = create_default_registry(workspace_guard=WorkspaceGuard(self.root), code_search_tool=self.tool(),
                                           lsp_config=LspConfig(servers=(server,), feedback_seconds=2))
        class Client:
            def __init__(inner):
                inner.calls = []
            def create_message(inner, **kwargs):
                inner.calls.append(deepcopy(kwargs))
                if len(inner.calls) == 1:
                    return ModelResponse('tool_use', [{'type': 'tool_use', 'id': 'edit', 'name': 'edit_file',
                        'input': {'file_path': 'Order.java', 'old_string': 'okay', 'new_string': 'BROKEN'}}])
                return ModelResponse('end_turn', [{'type': 'text', 'text': 'done'}])
        client = Client()
        context = ContextManager(config=ContextConfig(mode='off', transcript_dir=self.base / 'history', tool_output_dir=self.base / 'outputs'))
        agent = Agent(client=client, tools=registry, config=AgentConfig('mock', loop_guard=None), context=context, allow_subagents=False)
        agent.run('edit')
        self.assertIn('simulated diagnostic', str(client.calls[1]['messages']))
        self.assertEqual(next(t for t in registry.owners() if isinstance(t, LspTool)).service.sessions, {})
        child = registry.read_only_copy()
        child_client = Client()
        Agent(client=child_client, tools=child, config=AgentConfig('child', loop_guard=None), context=context, allow_subagents=False)
        search = next(t for t in child.owners() if isinstance(t, SearchCodeTool))
        self.assertIs(search.service.client, child_client)
        self.assertEqual(search.service.model, 'child')

    def test_polyglot_manual_annotations_freeze_and_grade_overloads(self):
        from evals.code_retrieval.polyglot import prepare_polyglot
        from evals.code_retrieval.dataset import load_bundle
        from evals.code_retrieval.scoring import grade_case, citation_documents
        self.write('Order.java', 'class Order {\n void refund(String id) {}\n void refund(int id) {}\n}\n')
        annotations = {'documents': [
            {'id': 'string-overload', 'path': 'Order.java', 'symbol': 'Order.refund', 'start_line': 2, 'end_line': 2},
            {'id': 'int-overload', 'path': 'Order.java', 'symbol': 'Order.refund', 'start_line': 3, 'end_line': 3}],
            'cases': [{'id': 'J1', 'query': 'Order.refund', 'split': 'test', 'category': 'exact',
                       'groups': [{'id': 'g', 'members': [{'doc_id': 'int-overload', 'evidence_lines': [3]}]}]}]}
        path = self.base / 'annotations.json'
        path.write_text(json.dumps(annotations), encoding='utf-8')
        output = prepare_polyglot(self.root, self.base / 'bundle', path)
        _, docs, cases = load_bundle(output)
        docs = citation_documents(output / 'target', docs)
        row = {'outcome': 'completed', 'answer': {'status': 'found', 'results': [
            {'path': 'Order.java', 'symbol': 'Order.refund', 'line': 3, 'quote': ' void refund(int id) {}'}]}}
        self.assertEqual(grade_case(cases[0], row, docs)['hit1'], 1)
        row['answer']['results'][0].update(line=2, quote=' void refund(String id) {}')
        self.assertEqual(grade_case(cases[0], row, docs)['hit1'], 0)


if __name__ == '__main__':
    unittest.main()
