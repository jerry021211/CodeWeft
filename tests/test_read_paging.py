"""Continuous, versioned source pages survive all common output budgets."""
import io
import json
from pathlib import Path
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch

from codeagent.context import ContextConfig, ContextManager
from codeagent.messages import ToolUse
from codeagent.tools import ReadFileTool
from codeagent.tools import read
from codeagent.tools.workspace import WorkspaceGuard


class ReadPagingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / 'source.txt'
        self.reader = ReadFileTool(workspace_guard=WorkspaceGuard(self.root))

    def write(self, source):
        self.path.write_bytes(source.encode('utf-8'))

    def page(self, **args):
        result = self.reader.run(str(self.path), **args)
        self.assertEqual(result.status, 'success', str(result))
        meta = result.read_page['metadata']
        header, body = str(result).split('\n', 1)
        self.assertEqual(json.loads(header[len(read.PAGE_MARKER):]), meta)
        self.assertEqual(len(body), meta['body_chars'])
        self.assertLessEqual(len(body), 120_000)
        if meta['start_line'] is not None:
            self.assertLessEqual(meta['end_line'] - meta['start_line'] + 1, 5000)
        return result

    def collect(self, **args):
        raw, seen = [], set()
        for _ in range(100):
            result = self.page(**args)
            raw.append(result.read_page['raw'])
            meta = result.read_page['metadata']
            if not meta['has_more']:
                self.assertIsNone(meta['next_offset'])
                self.assertIsNone(meta['next_char_offset'])
                return ''.join(raw)
            cursor = meta['next_offset'], meta['next_char_offset']
            self.assertNotIn(cursor, seen)
            seen.add(cursor)
            args.update(offset=cursor[0], char_offset=cursor[1], expected_version=meta['source_version'])
        self.fail('Pagination failed to terminate')

    def test_continuous_unicode_long_lines_and_original_newlines(self):
        source = '\r\n' + '汉🙂' * 180_000 + '\r\n' + '\nabc\rdef\n' + 'z' * 120_001
        self.write(source)
        self.assertEqual(self.collect(), source)

    def test_default_and_explicit_line_limits(self):
        self.write('x\n' * 5001)
        first = self.page()
        self.assertEqual(first.read_page['raw'], 'x\n' * 5000)
        self.assertEqual(first.read_page['metadata']['next_offset'], 5001)
        self.assertEqual(self.collect(limit=123), 'x\n' * 5001)

    def test_normal_lines_remain_whole(self):
        self.write('a' * 70_000 + '\n' + 'b' * 60_000 + '\n')
        first = self.page()
        self.assertEqual(first.read_page['raw'], 'a' * 70_000 + '\n')
        self.assertEqual(first.read_page['metadata']['next_offset'], 2)
        self.assertEqual(first.read_page['metadata']['next_char_offset'], 0)

    def test_crlf_at_exact_page_boundary(self):
        source = 'x' * 119_997 + '\r\nend'
        self.write(source)
        first = self.page()
        self.assertEqual(first.read_page['metadata']['next_char_offset'], 119_997)
        self.assertEqual(self.collect(), source)
        self.assertEqual(self.page(char_offset=119_998).read_page['raw'], '\nend')

    def test_midline_and_eof(self):
        self.write('abc\r\nlast')
        self.assertEqual(self.collect(char_offset=2, limit=1), 'c\r\nlast')
        for args in ({'offset': 3}, {'offset': 99}, {'offset': 2, 'char_offset': 4}):
            meta = self.page(**args).read_page['metadata']
            self.assertTrue(meta['eof'])
            self.assertFalse(meta['has_more'])
            self.assertIsNone(meta['start_line'])
        self.write('')
        self.assertTrue(self.page().read_page['metadata']['eof'])

    def test_parameter_validation(self):
        self.write('abc\n')
        for key, values in [('limit', [True, False, 0, -1, 5001, 2.5, '2']),
                            ('offset', [True, 0, -1]), ('char_offset', [True, -1, 0.5]),
                            ('expected_version', ['', 1]), ('force_full', [1])]:
            for value in values:
                with self.subTest(key=key, value=value):
                    self.assertEqual(self.reader.run(str(self.path), **{key: value}).status, 'error')
        self.assertEqual(self.reader.run(str(self.path), char_offset=10).status, 'error')

    def test_version_mismatch_and_mutation_discard_all_body(self):
        self.write('before\n')
        version = self.page().read_page['metadata']['source_version']
        self.write('after\n')
        result = self.reader.run(str(self.path), expected_version=version)
        self.assertEqual(result.outcome, 'source_changed')
        self.assertIsNone(result.file_read_snapshot)
        original = read._hash
        count = 0
        def changed_hash(handle):
            nonlocal count
            count += 1
            return original(handle) if count == 1 else 'changed-during-read'
        with patch.object(read, '_hash', side_effect=changed_hash):
            result = self.reader.run(str(self.path))
        self.assertEqual(result.outcome, 'source_changed')
        self.assertNotIn('after', result)
        self.assertIsNone(result.file_read_snapshot)

    def test_force_full_and_workspace_permissions(self):
        self.write('x' * 250_000)
        self.assertEqual(self.page(), self.page(force_full=True))
        denied = self.reader.run(str(self.root.parent / 'outside.txt'), force_full=True)
        self.assertEqual(denied.status, 'error')

    def test_memory_does_not_grow_with_file_size(self):
        with self.path.open('wb') as handle:
            for _ in range(256):
                handle.write(b'x' * 65536)
        tracemalloc.start()
        try:
            self.page()
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 4_000_000)

    def test_explicit_resize_uses_same_paginator_and_correct_cursor(self):
        source = 'hello\r\n' + '🙂' * 250_000 + '\r\nlast'
        self.write(source)
        original = self.page(char_offset=2)
        small = read.resize_read_page(original, 12_000)
        self.assertLessEqual(len(small), 12_000)
        meta = small.read_page['metadata']
        rest = self.collect(offset=meta['next_offset'], char_offset=meta['next_char_offset'],
                            expected_version=meta['source_version'])
        self.assertEqual(small.read_page['raw'] + rest, source[2:])

    def test_common_single_and_batch_budgets_do_not_limit_pages(self):
        self.write('x' * 250_000)
        manager = ContextManager(config=ContextConfig(single_tool_output_max_chars=1000,
            tool_result_budget_chars=2000, tool_output_dir=self.root / 'outputs'))
        calls = [ToolUse(str(i), 'read_file', {'file_path': str(self.path)}) for i in range(3)]
        pages = [self.page() for _ in calls]
        for call, page in zip(calls, pages):
            manager.record_tool_result(call, page)
        final = manager.finalize_tool_results(calls, pages)
        self.assertLessEqual(sum(map(len, final)), 300000)
        self.assertTrue(all(len(item.body) > 80000 for item in final))
        restored = json.loads(json.dumps(final))
        self.assertEqual(manager.finalize_tool_results(calls, restored), restored)
        generic = ToolUse('other', 'grep', {})
        mixed = manager.finalize_tool_results(calls + [generic], pages + ['y' * 3000])
        self.assertLessEqual(sum(map(len, mixed)), 300000)
        self.assertTrue(all(item.next_cursor for item in mixed[:3]))



if __name__ == '__main__':
    unittest.main()
