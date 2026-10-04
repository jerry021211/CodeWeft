"""Fixed limits, immutable sources and lossless recovery across the whole chain."""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from codeagent.context import ContextConfig, ContextManager
from codeagent.context.output_archive import OutputArchive, archive_text, log_page
from codeagent.context.summary_chunks import chunk_requests
from codeagent.messages import ToolUse
from codeagent.runtime_platform import RuntimePlatform
from codeagent.tools.base import ToolOutput
from codeagent.tools.bash import BashTool
from codeagent.tools.read import ReadFileTool
from codeagent.tools.runtime_data import LoadToolOutputTool, LoadContextHistoryTool
from codeagent.tools.output_pages import allocate, json_page
from codeagent.tools.search_files import records_page
from codeagent.tools.output_limits import *
from codeagent.skills.loader import SkillLoader
from codeagent.tools.skill import LoadSkillTool
from tests.test_context_simplification import SummaryClient, rounds


class FixedOutputPolicyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.manager = ContextManager(config=ContextConfig(tool_output_dir=self.root / 'outputs',
            transcript_dir=self.root / 'history', summarization_model='summary'))

    def test_fixed_config_and_water_filling(self):
        config = ContextConfig(single_tool_output_max_chars=1, command_output_max_chars=1,
                               tool_result_budget_chars=1, summary_max_chars=1)
        self.assertEqual(config.single_tool_output_max_chars, DEFAULT_BODY_CHARS)
        self.assertEqual(config.command_output_max_chars, BASH_BODY_CHARS)
        self.assertEqual(config.tool_result_budget_chars, BATCH_CHARS)
        self.assertEqual(config.summary_max_chars, SUMMARY_CHARS)
        self.assertEqual(allocate([1, 100, 100], 12), [1, 6, 5])

    def test_archive_byte_limit_utf8_and_no_later_gap(self):
        with patch('codeagent.context.output_archive.ARCHIVE_BYTES', 10):
            archive = OutputArchive(self.root)
            archive.append('汉🙂汉🙂', 'stdout')
            archive.append('abc', 'stderr')
            archive.finish()
        self.assertLessEqual(archive.saved_bytes, 10)
        self.assertEqual(archive.path.read_bytes().decode(), '汉🙂汉')
        self.assertFalse(archive.source_complete)
        page = LoadToolOutputTool(self.root).run(output_id=archive.output_id)
        self.assertFalse(page.has_more)
        self.assertFalse(page.source_complete)

    def test_storage_failure_never_claims_a_reference_or_changes_exit_status(self):
        blocked_root = self.root / 'file'
        blocked_root.write_text('not a directory')
        output = archive_text(blocked_root, ToolOutput('evidence', status='error', exit_code=7))
        result = log_page(output)
        self.assertEqual(result.exit_code, 7)
        self.assertIsNone(result.output_id)
        self.assertFalse(result.source_complete)
        self.assertTrue(result.storage_error)
        self.assertIn('evidence', result.body)

    def test_incomplete_json_archive_is_explicitly_plain_text(self):
        output = json_page(ToolOutput(''), {'results': [{'content': 'x' * 1000}]})
        with patch('codeagent.context.output_archive.ARCHIVE_BYTES', 100):
            final = self.manager.finalize_tool_results([ToolUse('json', 'search_code', {})], [output])[0]
        self.assertFalse(final.source_complete)
        self.assertIn('incomplete_json_text', final.truncated_reason)
        self.assertIsInstance(json.loads(final), dict)
        saved = LoadToolOutputTool(self.manager.config.tool_output_dir).run(output_id=final.output_id)
        self.assertFalse(saved.source_complete)
        self.assertFalse(saved.has_more)
        self.assertIn('incomplete_json_text', saved.truncated_reason)

    def test_live_read_batch_recomputes_real_cursors(self):
        file = self.root / 'source'
        file.write_text('x' * 240000)
        calls = [ToolUse(str(i), 'read_file', {'file_path': str(file)}) for i in range(4)]
        outputs = [ReadFileTool().run(str(file)) for _ in calls]
        final = self.manager.finalize_tool_results(calls, outputs)
        self.assertLessEqual(sum(map(len, final)), BATCH_CHARS)
        for result in final:
            cursor = result.next_cursor
            self.assertEqual(cursor['char_offset'], len(result.read_page['raw']))
            tail = ReadFileTool().run(str(file), offset=cursor['offset'], char_offset=cursor['char_offset'],
                                      expected_version=cursor['expected_version'])
            self.assertTrue(tail.startswith('[read_file page]'))

    def test_skills_indivisible_soft_limit_and_zero_read_page(self):
        from codeagent.tools.output_pages import page
        skill = page(ToolOutput(''), 's' * SKILL_BODY_CHARS)
        skill.page_renderer = lambda size: skill
        source = self.root / 'source'
        source.write_text('remaining')
        calls = [ToolUse(str(i), 'load_skill', {'name': str(i)}) for i in range(3)]
        calls.append(ToolUse('read', 'read_file', {'file_path': str(source)}))
        final = self.manager.finalize_tool_results(calls, [skill] * 3 + [ReadFileTool().run(str(source))])
        self.assertTrue(self.manager.state.last_tool_batch['batch_soft_limit_exceeded'])
        self.assertEqual([len(item.body) for item in final], [120000, 120000, 120000, 0])
        self.assertEqual(final[-1].next_cursor['char_offset'], 0)
        self.assertEqual(final[-1].status, 'success')

    def test_records_cursor_uses_returned_count_and_zero_does_not_advance(self):
        result = records_page(['first', 'second', 'third'], offset=7, limit=3)
        short = result.page_renderer(5)
        self.assertEqual(short.next_cursor, {'offset': 8})
        self.assertEqual(short.returned_range['count'], 1)
        zero = result.page_renderer(0)
        self.assertEqual(zero.next_cursor, {'offset': 7})
        self.assertEqual(zero.body, '')

    def test_json_pages_remain_parseable_after_batch_shrink(self):
        payload = {'results': [{'url': 'https://example.com/' + 'u' * 100, 'content': '汉' * 10000} for _ in range(10)],
                   'scan_complete': False, 'absence_proven': False}
        output = json_page(ToolOutput(''), payload, WEB_SEARCH_BODY_CHARS)
        small = output.page_renderer(800)
        parsed = json.loads(small)
        self.assertFalse(parsed['absence_proven'])
        self.assertLessEqual(len(small.body), 800)
        self.assertTrue(all(item['url'] == payload['results'][0]['url'] for item in parsed['results']))

    def test_array_json_and_known_mcp_search_keep_types_and_fixed_count(self):
        payload = [{'url': 'https://example.com/' + str(i), 'content': 'evidence'} for i in range(30)]
        result = json_page(ToolOutput(''), payload, 1000)
        self.assertIsInstance(json.loads(result)['result'], list)
        output = ToolOutput(json.dumps({'results': payload}))
        output.output_policy = 'web_search'
        final = self.manager.finalize_tool_results([ToolUse('mcp', 'mcp__tavily__tavily-search', {})], [output])[0]
        self.assertEqual(len(json.loads(final)['results']), 10)
        self.assertEqual(json.loads(final)['omitted_count'], 20)
        self.assertEqual(len(json.loads(final.archive.path.read_text())['results']), 30)

    def test_archive_search_preserves_cross_chunk_and_unshown_matches(self):
        output = archive_text(self.root, ToolOutput('x' * 8190 + 'ABCDE' + 'x' * 600 + 'ABCDE'))
        tool = LoadToolOutputTool(self.root)
        first = tool.run(output_id=output.output_id, query='ABCDE', max_matches=1)
        self.assertIn('char_offset=8190', first.body)
        following = tool.run(output_id=output.output_id, query='ABCDE', **first.next_cursor)
        self.assertIn('char_offset=8795', following.body)
        self.assertNotIn('char_offset=8190', following.body)
        self.assertTrue(following.scan_complete)
        tiny = tool.run(output_id=output.output_id, query='ABCDE', char_limit=1)
        self.assertIn('record_too_large', tiny.truncated_reason)
        self.assertEqual(tiny.next_cursor, {'offset': 1, 'char_offset': 8190})

    def test_checkpoint_metadata_and_archive_reference_survive_restore(self):
        from dataclasses import asdict
        from codeagent.context import RuntimeState
        call = ToolUse('saved', 'bash', {'command': 'already executed'})
        output = ToolOutput('evidence', status='error', exit_code=9)
        first = self.manager.finalize_tool_results([call], [output])[0]
        state = RuntimeState(**json.loads(json.dumps(asdict(self.manager.state))))
        restored = ContextManager(config=self.manager.config, state=state)
        final = restored.finalize_tool_results([call], [output])[0]
        self.assertEqual(final.output_id, first.output_id)
        self.assertEqual(final.exit_code, 9)
        self.assertEqual(final.body, 'evidence')
        self.assertEqual(len(state.tool_artifacts), 1)
        retried = restored.finalize_tool_results([call], [ToolOutput('evidence', status='error', exit_code=9)])[0]
        self.assertNotEqual(retried.output_id, first.output_id)

    def test_batch_same_priority_fairness_and_failed_skill_is_divisible(self):
        calls = [ToolUse(str(i), 'load_skill' if i == 0 else 'echo', {}) for i in range(4)]
        outputs = [ToolOutput('x' * 120000, status='error') for _ in calls]
        final = self.manager.finalize_tool_results(calls, outputs)
        sizes = [len(value.body) for value in final]
        self.assertLessEqual(max(sizes) - min(sizes), 1)
        self.assertLessEqual(sum(map(len, final)), BATCH_CHARS)
        self.assertTrue(all(value.metadata_chars <= RESULT_METADATA_CHARS for value in final))

    def test_history_partial_message_tail_precedes_next_message(self):
        file = self.root / 'history.jsonl'
        first = json.dumps({'role': 'user', 'content': 'a' * 30000})
        second = json.dumps({'role': 'assistant', 'content': 'second'})
        file.write_text(first + '\n' + second + '\n', encoding='utf-8')
        tool = LoadContextHistoryTool(self.root)
        initial = tool.run(str(file))
        self.assertEqual(initial.next_cursor, {'message_offset': 1, 'char_offset': HISTORY_MESSAGE_CHARS})
        self.assertNotIn('second', initial.body)
        following = tool.run(str(file), **initial.next_cursor)
        self.assertIn('second', following.body)
        self.assertFalse(following.has_more)

    def test_archive_read_reconstructs_unicode_and_keeps_original_archive(self):
        raw = '汉🙂' * 65000 + '\r\nlast'
        output = archive_text(self.root, ToolOutput(raw))
        before = output.archive.path.read_bytes()
        tool = LoadToolOutputTool(self.root)
        args, parts = {}, []
        for _ in range(5):
            result = tool.run(output_id=output.output_id, **args)
            # Strip only known display line numbers, preserving source terminators.
            import re
            parts.append(re.sub(r'(^|(?<=[\r\n]))\d+\t', '', result.body))
            if not result.has_more:
                break
            args = result.next_cursor
        self.assertEqual(''.join(parts), raw)
        self.assertEqual(output.archive.path.read_bytes(), before)

    def test_real_shell_collects_both_channels_with_true_exit(self):
        platform = RuntimePlatform('Test', 'Python', sys.executable, ('-c',), 'Python')
        result = BashTool(runtime_platform=platform, output_root=self.root).run(
            "import sys; sys.stdout.write('x'*90000); sys.stderr.write('AssertionError: failure\\n'); sys.exit(3)")
        self.assertEqual(result.exit_code, 3)
        self.assertLessEqual(len(result.body), BASH_BODY_CHARS)
        self.assertIn('AssertionError', result.body)
        self.assertTrue(result.source_complete)
        self.assertEqual({row['channel'] for row in result.archive.channels}, {'stdout', 'stderr'})

    def test_large_skill_is_error_and_qualified_skill_is_complete(self):
        folder = self.root / 'skills' / 'demo'
        folder.mkdir(parents=True)
        manifest = folder / 'SKILL.md'
        manifest.write_text('---\nname: demo\n---\n' + 'x' * 120001)
        tool = LoadSkillTool(SkillLoader([folder.parent]))
        self.assertEqual(tool.run('demo').status, 'error')
        manifest.write_text('---\nname: demo\n---\nfull instructions')
        self.assertEqual(tool.run('demo').body, manifest.read_bytes().decode())

    def test_summary_chunks_cover_large_field_without_head_tail_loss(self):
        text = '甲乙🙂' * 30000
        make = lambda raw: dict(model='summary', system='instructions', tools=[], max_tokens=SUMMARY_OUTPUT_TOKENS,
                                messages=[{'role': 'user', 'content': raw}])
        requests = chunk_requests([{'role': 'user', 'content': text}], make, 100000)
        self.assertGreater(len(requests), 1)
        pieces = [item['text'] for request in requests for item in json.loads(request['messages'][0]['content'])
                  if item['field'] == '/content']
        self.assertEqual(''.join(pieces), text)
        self.assertTrue(all(request['max_tokens'] == 32768 for request in requests))

    def test_multiblock_summary_commits_only_after_complete_coverage(self):
        history = rounds(15, size=18000)
        self.manager.begin_turn(len(history))
        client = SummaryClient()
        self.manager.force_compact(history, client=client)
        self.assertEqual(self.manager.last_compaction['status'], 'written')
        self.assertTrue(self.manager.last_compaction['coverage_complete'])
        self.assertGreater(len(client.calls), 1)
        self.assertTrue(all(call['max_tokens'] == SUMMARY_OUTPUT_TOKENS for call in client.calls))


if __name__ == '__main__':
    unittest.main()
