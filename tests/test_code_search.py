"""Small synthetic source fixtures; no formal evaluation answers or keywords."""
import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

from codeagent.code_search.chunks import chunks
from codeagent.code_search.service import has_code_locator
from codeagent.events import TokenUsage
from codeagent.hooks.loop_guard import LoopGuardConfig
from codeagent.models import ModelResponse
from codeagent.permissions.discuss import READ_ONLY_TOOLS
from codeagent.runtime.execution import RunBudget, ExecutionStopped
from codeagent.tools.defaults import create_default_registry
from codeagent.tools.search_code import SearchCodeTool
from evals.code_retrieval.scoring import citation_documents, check_citation


class CodeSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'repo'
        self.root.mkdir()
        self.index = Path(self.temp.name) / 'index'
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True, capture_output=True)
        self.tool = SearchCodeTool(self.root, self.index)

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def search(self, query, **kwargs):
        return json.loads(self.tool.run(query, **kwargs))

    def test_symbol_scope_decorators_and_exact_source(self):
        source = 'class Shelf:\n    @staticmethod\n    async def fetchBlueItem():\n        return "blue"\n'
        self.write('pkg/shelf.py', source)
        self.write('other.py', 'def fetchBlueItem():\n    return "wrong scope"\n')
        hit = self.search('Shelf.fetchBlueItem', path='pkg')['results'][0]
        self.assertEqual(hit['symbol'], 'Shelf.fetchBlueItem')
        self.assertEqual((hit['line'], hit['definition_start_line']), (2, 3))
        self.assertEqual(hit['quote'], '\n'.join(source.splitlines()[hit['line']-1:hit['end_line']]))
        self.assertEqual(hit['content_hash'], hashlib.sha256((self.root / 'pkg/shelf.py').read_bytes()).hexdigest())
        self.assertEqual(self.search('blue item', path='pkg')['results'][0]['path'], 'pkg/shelf.py')
        self.assertTrue(self.tool.run('blue', path='..').startswith('Error:'))

    def test_incremental_add_modify_delete_and_preserved_mtime(self):
        path = self.write('a.py', 'def orange():\n    return 1\n')
        self.assertEqual(self.search('orange')['indexed_files_updated'], 1)
        self.assertEqual(self.search('orange')['indexed_files_updated'], 0)
        stat = path.stat()
        path.write_text('def orange():\n    return 2\n', encoding='utf-8')
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertIn('return 2', self.search('orange')['results'][0]['quote'])
        self.write('b.py', 'def purple():\n    return 3\n')
        self.assertEqual(self.search('purple')['results'][0]['path'], 'b.py')
        path.unlink()
        self.assertEqual(self.search('orange')['results'], [])

    def test_ignored_and_syntax_fallback(self):
        self.write('.gitignore', 'hidden.py\n')
        self.write('hidden.py', 'def secretPeach():\n    return 42\n')
        self.write('broken.py', 'def brokenPear(:\n    return 42\n')
        self.assertEqual(self.search('secretPeach')['results'], [])
        result = self.search('brokenPear')
        self.assertFalse(result['scan_complete'])
        self.assertEqual(result['results'][0]['kind'], 'parse_fallback')

    def test_corrupt_and_busy_cache_fallback(self):
        self.write('x.py', 'def copper():\n    return 9\n')
        self.index.mkdir()
        database = self.index / 'source-v1.sqlite3'
        database.write_bytes(b'not a sqlite database')
        result = self.search('copper')
        self.assertEqual(result['index_backend'], 'python_fallback')
        self.assertEqual(result['results'][0]['symbol'], 'copper')
        database.unlink()
        self.search('copper')
        with closing(sqlite3.connect(database)) as conn:
            conn.execute('BEGIN EXCLUSIVE')
            result = self.search('copper')
            self.assertEqual(result['index_backend'], 'python_fallback')

    def test_long_function_dedup_and_output_budget(self):
        self.write('long.py', 'def loadGold():\n' + ''.join(f'    gold_{i} = {i}\n' for i in range(200)))
        for i in range(9):
            self.write(f'other{i}.py', f'def loadGold{i}():\n' + '    # gold ' + 'x'*1800 + '\n    return 1\n')
        output = self.tool.run('gold', top_k=10)
        result = json.loads(output)
        self.assertLessEqual(len(output), 8000)
        self.assertEqual(len([r for r in result['results'] if r['symbol']=='loadGold']), 1)
        for r in result['results']:
            self.assertLessEqual(len(r['quote'].splitlines()), 20)

    def test_rewrite_shared_budget_and_agent_keywords(self):
        self.write('queue.py', 'def discard_pending():\n    pending.clear()\n')
        budget = RunBudget(LoopGuardConfig(max_model_calls=3))

        class Client:
            activity = SimpleNamespace(execution_budget=budget)
            base_url = 'https://example.invalid/custom'
            calls = 0
            def fork(inner, **kwargs):
                inner.kind = kwargs['call_kind']
                return inner
            def create_message(inner, **kwargs):
                inner.calls += 1
                return ModelResponse('end_turn', [{'type': 'text', 'text': '{"queries":["discard pending"]}'}], usage=TokenUsage(input_tokens=5, output_tokens=3))

        client = Client()
        self.tool = SearchCodeTool(self.root, self.index, client=client, model='test')
        result = self.search('清除等待中的操作')
        self.assertEqual(result['results'][0]['symbol'], 'discard_pending')
        self.assertEqual((client.calls, budget.state.model_calls), (1, 1))
        self.assertEqual(client.kind, 'code_search_rewrite')
        self.search('清除等待中的操作', keywords=['discard pending'])
        self.assertEqual(client.calls, 1)
        budget.state.model_calls = 2
        self.assertEqual(self.search('不存在的行为')['rewrite_status'], 'budget_skipped')

    def test_plain_english_words_do_not_suppress_chinese_rewrite(self):
        self.write('widget.py', 'def suppress_failed_widget():\n    return "failure"\n\ndef display_fault_banner():\n    return "fault"\n')

        class Client:
            calls = 0
            def fork(inner, **kwargs):
                return inner
            def create_message(inner, **kwargs):
                inner.calls += 1
                return ModelResponse('end_turn', [{'type': 'text', 'text': '{"queries":["suppress failed widget"]}'}])

        client = Client()
        self.tool = SearchCodeTool(self.root, self.index, client=client, model='test')
        result = self.search('组件失败时将异常变为文字 fault')
        self.assertEqual((result['rewrite_status'], client.calls), ('completed', 1))
        self.assertIn('suppress_failed_widget', {r['symbol'] for r in result['results']})
        self.search('组件失败时将异常变为文字 fault', keywords=['suppress failed widget'])
        self.search('suppress_failed_widget')
        self.assertEqual(client.calls, 1)

    def test_code_locator_shapes_and_ordinary_words(self):
        for query in ('处理 network failure', '等待 token 返回', '发生 XML 解析问题'):
            self.assertFalse(has_code_locator(query), query)
        for query in ('定位 parse_data', '查找 parseData', 'JsonParser', 'Parser.parse',
                      'pkg/parser.py', '错误 E42', '搜索 `literal failure`'):
            self.assertTrue(has_code_locator(query), query)

    def test_cancellation_and_injection_compatibility(self):
        def cancel():
            raise ExecutionStopped('cancelled')
        self.tool.bind_runtime(cancellation_check=cancel)
        with self.assertRaises(ExecutionStopped):
            self.tool.run('anything')
        self.assertIn('search_code', READ_ONLY_TOOLS)
        old = create_default_registry()
        new = create_default_registry(code_search_tool=self.tool)
        self.assertNotIn('search_code', [s['name'] for s in old.schemas()])
        self.assertIn('search_code', [s['name'] for s in new.schemas()])

    def test_grading_decorators_preserves_id_and_rejects_wrong_line(self):
        self.write('x.py', 'class X:\n    @staticmethod\n    def f():\n        return 7\n')
        docs = [{'_id': 'x.py::X.f@3', 'path': 'x.py', 'symbol': 'X.f', 'start_line': 3, 'end_line': 4, 'text': '    def f():\n        return 7'}]
        corrected = citation_documents(self.root, docs)
        self.assertEqual(corrected[0]['_id'], docs[0]['_id'])
        cite = {'path': 'x.py', 'symbol': 'X.f', 'line': 2, 'quote': '@staticmethod\n    def f():\n        return 7'}
        self.assertIsNone(check_citation(cite, corrected)[2])
        self.assertIsNotNone(check_citation(dict(cite, line=3), corrected)[2])

    def test_encoded_python_and_symlink_scope(self):
        encoded = self.root / 'encoded.py'
        encoded.write_bytes(b'# coding: latin-1\ndef cafe():\n    return "caf\xe9"\n')
        self.assertIn('caf\u00e9', self.search('cafe')['results'][0]['quote'])
        outside = Path(self.temp.name) / 'outside.py'
        outside.write_text('def outsideOnly():\n    return 1\n', encoding='utf-8')
        try:
            (self.root / 'link.py').symlink_to(outside)
        except OSError:
            self.skipTest('OS does not allow creating symlinks')
        self.assertEqual(self.search('outsideOnly')['results'], [])


if __name__ == '__main__':
    unittest.main()
