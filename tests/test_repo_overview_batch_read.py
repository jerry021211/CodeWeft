"""Project discovery and multi-file reads through the public tool registry."""
from pathlib import Path
import subprocess
import tempfile
import unittest

from codeagent import Agent, AgentConfig, ModelResponse
from codeagent.context import ContextConfig, ContextManager
from codeagent.messages import ToolUse
from codeagent.permissions.read_only import READ_ONLY_TOOLS
from codeagent.runtime.cancellation import CancelledError
from codeagent.tools import create_default_registry, WorkspaceGuard
from codeagent.tools.output_limits import BATCH_CHARS, READ_BODY_CHARS
from codeagent.tools.read import ReadFileTool, with_read_feedback
from codeagent.tools.repo_map import RepoMapTool


class OverviewBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'repo'
        self.root.mkdir()
        self.guard = WorkspaceGuard(self.root)
        self.reader = ReadFileTool(workspace_guard=self.guard)
        self.registry = create_default_registry(workspace_guard=self.guard)

    def write(self, name, text='hello\n'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode('utf-8'))
        return path

    def test_registry_accepts_batch_only_and_keeps_order_ranges_and_single_reads(self):
        names = [f'{i}.py' for i in range(5)]
        for name in names:
            self.write(name, 'first\nsecond\nthird\n')
        result = self.registry.execute('read_file', {'file_paths': names, 'offset': 2, 'limit': 1})
        self.assertEqual(result.status, 'success')
        self.assertEqual([p for p, _ in result.read_batch], names)
        for path, value in result.read_batch:
            self.assertEqual(value.body, '2\tsecond\n')
            self.assertEqual(value.read_page['metadata']['next_offset'], 3)
            single = self.reader.run(path, offset=2, limit=1)
            self.assertEqual(str(single), str(value))

    def test_large_batch_shares_budget_and_continues_each_file_without_gaps(self):
        source = '汉🙂' * 65000 + '\r\nlast\n'
        names = [f'large{i}.txt' for i in range(5)]
        for name in names:
            self.write(name, source)
        result = self.reader.run(file_paths=names)
        self.assertLessEqual(len(result.body), READ_BODY_CHARS)
        for path, first in result.read_batch:
            raw = first.read_page['raw']
            current = first
            while current.has_more:
                current = self.reader.run(file_path=path, **current.next_cursor)
                self.assertEqual(current.status, 'success')
                raw += current.read_page['raw']
            self.assertEqual(raw, source)
        self.write(names[0], 'changed')
        self.assertEqual(self.reader.run(names[0], **result.read_batch[0][1].next_cursor).outcome, 'source_changed')

    def test_partial_failure_preserves_success_without_workspace_escape(self):
        self.write('ok.py')
        outside = Path(self.temp.name) / 'secret.txt'
        outside.write_text('SECRET_CONTENT', encoding='utf-8')
        result = self.reader.run(file_paths=['ok.py', 'missing.py', '../secret.txt'])
        self.assertEqual(result.outcome, 'partial_success')
        self.assertEqual([v.status for _, v in result.read_batch], ['success', 'error', 'error'])
        self.assertNotIn('SECRET_CONTENT', result)
        self.assertFalse(result.source_complete)
        self.assertEqual(self.reader.run(file_paths=['missing.py']).status, 'error')

    def test_batch_rejects_ambiguous_invalid_and_oversized_inputs(self):
        for args in ({}, {'file_paths': []}, {'file_paths': ['a'] * 6},
                     {'file_paths': ['a', 1]}, {'file_path': 'a', 'file_paths': ['b']},
                     {'file_paths': ['']}, {'file_paths': ['a'], 'expected_version': 'v'},
                     {'file_paths': ['a'], 'char_offset': 1}):
            with self.subTest(args=args):
                self.assertEqual(self.registry.execute('read_file', args).outcome, 'parameter_error')

    def test_context_shrinks_batch_pages_without_dropping_file_cursors(self):
        names = [f'{i}.txt' for i in range(5)]
        for name in names:
            self.write(name, 'x' * 130000)
        output = self.reader.run(file_paths=names)
        context = ContextManager(config=ContextConfig(tool_output_dir=Path(self.temp.name) / 'outputs'))
        calls = [ToolUse(str(i), 'read_file', {'file_paths': names}) for i in range(4)]
        values = context.finalize_tool_results(calls, [output] * 4)
        self.assertLessEqual(sum(map(len, values)), BATCH_CHARS)
        for value in values:
            self.assertEqual(len(value.read_batch), 5)
            for _, part in value.read_batch:
                self.assertTrue(part.has_more)
                self.assertTrue(part.next_cursor['expected_version'])
        zero = with_read_feedback(output, 'note').page_renderer(0)
        self.assertEqual(zero.body, '')
        self.assertIn('note', zero)
        self.assertEqual(len(zero.read_batch), 5)

    def test_repo_map_shows_labels_sizes_and_subdirectory_navigation(self):
        for path in ('package.json', 'src/server.js', 'tests/booking.test.js', 'README.md', 'src/deep/hidden.py'):
            self.write(path)
        result = self.registry.execute('repo_map', {'depth': 2})
        for label in ('package.json  6 B [配置]', 'server.js  6 B [入口候选]',
                      'booking.test.js  6 B [测试]', 'README.md  6 B [文档]'):
            self.assertIn(label, result)
        self.assertNotIn('hidden.py', result)
        self.assertTrue(result.has_more)
        nested = self.registry.execute('repo_map', {'path': 'src/deep'})
        self.assertIn('hidden.py', nested)
        self.assertFalse(nested.has_more)
        self.assertIn('repo_map', READ_ONLY_TOOLS)
        self.assertIn('repo_map', self.registry.read_only_copy())

    def test_repo_map_honors_git_ignore_and_fallback_directory_exclusions(self):
        self.write('node_modules/vendor.js')
        self.write('src/main.py')
        self.write('ignored.py')
        self.write('.gitignore', 'ignored.py\nnode_modules/\n')
        fallback = self.registry.execute('repo_map')
        self.assertNotIn('vendor.js', fallback)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True, capture_output=True)
        result = self.registry.execute('repo_map')
        self.assertNotIn('ignored.py', result)
        self.assertNotIn('vendor.js', result)
        self.assertIn('main.py', result)

    def test_repo_map_limits_and_cancellation(self):
        for i in range(8):
            self.write(f'{i}.py')
        result = self.registry.execute('repo_map', {'max_files': 2})
        self.assertIn('省略6个', result)
        self.assertTrue(result.has_more)
        self.assertEqual(self.registry.execute('repo_map', {'path': '..'}).status, 'error')
        self.assertEqual(self.registry.execute('repo_map', {'depth': 0}).outcome, 'parameter_error')
        tool = RepoMapTool(self.guard)
        def cancel():
            raise CancelledError('stop')
        tool.bind_runtime(cancellation_check=cancel)
        with self.assertRaises(CancelledError):
            tool.run()

    def test_agent_exposes_tools_and_returns_all_batch_files_in_one_result(self):
        self.write('a.py', 'alpha\n')
        self.write('b.py', 'beta\n')
        test = self
        class Client:
            calls = 0
            def create_message(self, **kwargs):
                self.calls += 1
                test.assertIn('repo_map', {schema['name'] for schema in kwargs['tools']})
                if self.calls == 1:
                    return ModelResponse('tool_use', [{'type': 'tool_use', 'id': 'batch', 'name': 'read_file',
                                                       'input': {'file_paths': ['a.py', 'b.py']}}])
                text = str(kwargs['messages'][-1]['content'])
                test.assertIn('alpha', text)
                test.assertIn('beta', text)
                return ModelResponse('end_turn', [{'type': 'text', 'text': 'done'}])
        context = ContextManager(config=ContextConfig(tool_output_dir=Path(self.temp.name) / 'outputs'))
        agent = Agent(Client(), self.registry, AgentConfig(model='fake'), context=context)
        self.assertEqual(agent.run('read both').final_text, 'done')
        result = next(b for m in agent.messages for b in m['content'] if isinstance(m['content'], list)
                      and isinstance(b, dict) and b.get('type') == 'tool_result')
        self.assertFalse(hasattr(result['content'], 'read_batch'))


if __name__ == '__main__':
    unittest.main()
