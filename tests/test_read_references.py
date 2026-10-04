"""Actual reads + fake SDK captures: references must never lose source evidence."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from codeagent import Agent, AgentConfig, ToolRegistry
from codeagent.config import EnvironmentConfig
from codeagent.context import ContextConfig, ContextManager
from codeagent.context.budget import RequestBudgetError
from codeagent.context.read_references import READ_REFERENCE_MARKER
from codeagent.events import CallbackEventSink, EventEmitter
from codeagent.hooks import HookManager
from codeagent.messages import ToolUse, validate_tool_history
from codeagent.prompts import PromptRuntime
from codeagent.runtime.parallel import ParallelConfig
from codeagent.tools import ReadFileTool, EditFileTool, WriteFileTool
from codeagent.tools.workspace import WorkspaceGuard
from codeagent.web.factory import serialize_runtime_state, _restore_runtime_state
from tests.test_context_simplification import SummaryClient
from tests.test_prefix_cache import LocalClient, SDK


def results(messages):
    return {block['tool_use_id']: block['content'] for message in messages
            if message['role'] == 'user' and isinstance(message.get('content'), list)
            for block in message['content'] if block.get('type') == 'tool_result'}


class ReadReferenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.file = self.root / 'module.py'
        self.file.write_text(''.join(f'value_{i} = "evidence {i:04d}"\n' for i in range(400)), encoding='utf-8')
        self.reader = ReadFileTool(workspace_guard=WorkspaceGuard(self.root))
        self.history = [{'role': 'user', 'content': 'Inspect this source, preserving exact evidence.'}]
        self.manager = self.make_manager()

    def make_manager(self, **options):
        manager = ContextManager(config=ContextConfig(**{
            'summarization_model': 'summary', 'transcript_dir': self.root / 'history',
            'tool_output_dir': self.root / 'outputs', 'tool_projection_enabled': False, **options}))
        manager.begin_turn(0)
        return manager

    def read(self, call_id, *, manager=None, history=None, **arguments):
        manager = manager or self.manager
        history = self.history if history is None else history
        args = {'file_path': str(self.file), **arguments}
        output = self.reader.run(**args)
        call = ToolUse(call_id, 'read_file', args)
        manager.record_tool_result(call, output)
        content = manager.finalize_tool_results([call], [str(output)])[0]
        history.extend([
            {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': call_id, 'name': 'read_file', 'input': args}]},
            {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': call_id, 'content': content,
                                        **({'is_error': True} if str(output).startswith('Error:') else {})}]},
        ])
        return output

    def test_only_request_view_is_shortened_and_reference_chains_are_forbidden(self):
        body = str(self.read('first'))
        first = deepcopy(self.manager.project_messages(self.history))
        self.read('second')
        second = deepcopy(self.manager.project_messages(self.history))
        self.read('third')
        original = deepcopy(self.history)
        view = self.manager.project_messages(self.history)
        self.assertEqual(self.history, original)
        self.assertEqual(results(self.history), dict(first=body, second=body, third=body))
        self.assertEqual(results(view)['first'], body)
        for name in ('second', 'third'):
            self.assertIn('source_tool_use_id: "first"', results(view)[name])
            self.assertLess(len(results(view)[name]), len(body) // 10)
        self.assertEqual(view[:len(second)], second)
        self.assertEqual(second[:len(first)], first)
        validate_tool_history(view)
        self.assertEqual(self.manager.last_read_projection['read_reference_count'], 2)
        self.assertEqual(self.manager.last_read_projection['read_reference_chars_saved'],
                         2 * len(body) - len(results(view)['second']) - len(results(view)['third']))

    def test_force_full_and_changed_range_return_the_requested_body(self):
        body = str(self.read('first', offset=1, limit=100))
        self.read('same', offset=1, limit=100)
        self.read('forced', offset=1, limit=100, force_full=True)
        different = str(self.read('different', offset=2, limit=100))
        values = results(self.manager.project_messages(self.history))
        self.assertIn(READ_REFERENCE_MARKER, values['same'])
        self.assertEqual(values['forced'], body)
        self.assertEqual(values['different'], different)

    def test_reference_receipt_cannot_be_written_as_file_content(self):
        self.read('first')
        self.read('second')
        reference = results(self.manager.project_messages(self.history))['second']
        guard = WorkspaceGuard(self.root)
        before = self.file.read_bytes()
        self.assertTrue(str(WriteFileTool(workspace_guard=guard).run(str(self.file), reference)).startswith('Error:'))
        self.assertTrue(str(EditFileTool(workspace_guard=guard).run(
            str(self.file), 'value_0 = "evidence 0000"', reference)).startswith('Error:'))
        self.assertEqual(self.file.read_bytes(), before)

    def test_byte_change_outside_requested_range_invalidates_even_with_restored_mtime(self):
        first = self.read('first', limit=100)
        stamp = self.file.stat()
        content = self.file.read_bytes()
        self.file.write_bytes(content.replace(b'evidence 0399', b'CHANGED! 0399'))
        os.utime(self.file, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        second = self.read('second', limit=100)
        self.assertEqual(str(first).split('\n', 1)[1], str(second).split('\n', 1)[1])
        self.assertNotEqual(first.read_page['metadata']['source_version'], second.read_page['metadata']['source_version'])
        self.assertNotEqual(first.file_read_snapshot['file_hash'], second.file_read_snapshot['file_hash'])
        self.assertNotIn(READ_REFERENCE_MARKER, results(self.manager.project_messages(self.history))['second'])

    def test_edit_after_reference_keeps_old_prefix_but_new_read_is_full(self):
        self.read('first')
        self.read('second')
        sent = deepcopy(self.manager.project_messages(self.history))
        self.file.write_text('changed = 1\n' * 1000, encoding='utf-8')
        body = str(self.read('new_version'))
        view = self.manager.project_messages(self.history)
        self.assertEqual(view[:len(sent)], sent)
        self.assertEqual(results(view)['new_version'], body)

    def test_missing_deleted_and_failed_reads_are_never_references(self):
        self.read('first')
        self.file.unlink()
        self.read('missing')
        self.assertIn('Error:', results(self.manager.project_messages(self.history))['missing'])
        self.assertNotIn('missing', self.manager.state.read_references['receipts'])

    def test_edit_tool_changes_invalidate_the_next_read(self):
        self.read('first')
        editor = EditFileTool(workspace_guard=WorkspaceGuard(self.root))
        edited = editor.run(str(self.file), 'value_0 = "evidence 0000"', 'value_0 = "corrected"')
        self.assertEqual(edited.status, 'success')
        fresh = str(self.read('after_edit'))
        self.assertIn('corrected', fresh)
        self.assertEqual(results(self.manager.project_messages(self.history))['after_edit'], fresh)

    def test_racing_read_has_no_reusable_receipt(self):
        from codeagent.tools.read import _stamp
        initial = _stamp(self.file.stat())
        changed = [*initial[:-1], initial[-1] + 1]
        with patch('codeagent.tools.read._stamp', side_effect=[initial, initial, initial, changed]):
            output = self.read('racing')
        self.assertIsNone(output.file_read_snapshot)
        self.read('next')
        self.assertNotIn(READ_REFERENCE_MARKER, results(self.manager.project_messages(self.history))['next'])

    def test_archive_preview_and_hook_modified_text_cannot_be_anchors(self):
        self.read('first')
        self.history[-1]['content'][0]['content'] += '\nRuntime feedback that must stay visible.'
        self.read('second')
        self.assertNotIn(READ_REFERENCE_MARKER, results(self.manager.project_messages(self.history))['second'])
        other = self.make_manager(single_tool_output_max_chars=1000)
        history = deepcopy(self.history[:1])
        self.read('archived1', manager=other, history=history)
        self.read('archived2', manager=other, history=history)
        view = results(other.project_messages(history))
        self.assertNotIn('[tool output stored]', view['archived1'])
        self.assertIn(READ_REFERENCE_MARKER, view['archived2'])

    def test_cleanup_of_anchor_rehydrates_surviving_duplicate(self):
        body = str(self.read('first'))
        self.read('second')
        self.assertIn(READ_REFERENCE_MARKER, results(self.manager.project_messages(self.history))['second'])
        # Simulate an existing stable cleanup patch removing the first raw body.
        cleaned = deepcopy(self.history)
        cleaned[2]['content'][0]['content'] = '[older evidence cleared]'
        self.manager._remember_tool_view(self.history, cleaned)
        view = self.manager.project_messages(self.history)
        self.assertEqual(results(view)['second'], body)
        self.assertEqual(results(view)['first'], '[older evidence cleared]')
        validate_tool_history(view)

    def test_page_cleanup_keeps_source_anchor_and_continuation_metadata(self):
        manager = self.make_manager(mode='off', tool_projection_enabled=True,
                                    investigation_keep_rounds=1, tool_clear_min_chars=1000)
        for name in ('first', 'second', 'third'):
            self.read(name, manager=manager, limit=100)
        cleaned = manager.project_messages(self.history, clean_tools=True)
        self.assertIn(READ_REFERENCE_MARKER, results(cleaned)['third'])
        self.assertEqual(results(cleaned)['first'], results(self.history)['first'])
        self.assertIn('"next_offset":101', results(cleaned)['third'])
        self.read('fourth', manager=manager, limit=100)
        view = manager.project_messages(self.history)
        self.assertEqual(view[:len(cleaned)], cleaned)
        self.assertIn('source_tool_use_id: "first"', results(view)['fourth'])
        self.assertEqual(results(view)['third'], results(cleaned)['third'])

    def test_reference_projection_does_not_bypass_hard_limit(self):
        self.read('first')
        self.read('second')
        self.manager.config.mode = 'off'
        self.manager.config.context_window_tokens = 1000
        with self.assertRaises(RequestBudgetError):
            self.manager.prepare_before_model_call(self.history)

    def test_real_compaction_and_checkpoint_never_leave_dangling_references(self):
        for index in range(12):
            self.read(f'read-{index}')
        before = deepcopy(self.history)
        sent = self.manager.project_messages(self.history)
        self.assertIn('source_tool_use_id: "read-0"', results(sent)['read-11'])
        self.manager.force_compact(self.history, client=SummaryClient('Earlier investigation complete.'))
        self.assertEqual(self.manager.state.summary_revision, 1)
        view = self.manager.project_messages(self.history)
        values = results(view)
        self.assertNotIn('read-0', values)
        first_remaining = next(iter(values))
        self.assertEqual(values[first_remaining], results(self.history)[first_remaining])
        self.assertIn(f'source_tool_use_id: "{first_remaining}"', values['read-11'])
        self.assertEqual(self.history, before)
        restored = ContextManager(config=self.manager.config, state=_restore_runtime_state(
            json.loads(json.dumps(serialize_runtime_state(self.manager.state)))))
        self.assertEqual(restored.project_messages(self.history), view)
        validate_tool_history(view)

    def test_checkpoint_restore_validates_effective_content_and_file_version(self):
        self.read('first')
        self.read('second')
        sent = deepcopy(self.manager.project_messages(self.history))
        restored = ContextManager(config=self.manager.config, state=_restore_runtime_state(
            json.loads(json.dumps(serialize_runtime_state(self.manager.state)))))
        self.assertEqual(restored.project_messages(self.history), sent)
        self.read('third', manager=restored)
        self.assertIn(READ_REFERENCE_MARKER, results(restored.project_messages(self.history))['third'])
        self.file.write_text('new data\n' * 1000, encoding='utf-8')
        body = str(self.read('fourth', manager=restored))
        self.assertEqual(results(restored.project_messages(self.history))['fourth'], body)
        # No unverified body can be retained merely because a receipt was restored.
        self.history[2]['content'][0]['content'] = '[removed first body]'
        restored_view = results(restored.project_messages(self.history))
        self.assertNotIn(READ_REFERENCE_MARKER, restored_view['second'])

    def test_old_or_incompatible_checkpoints_fall_back_to_full_results(self):
        self.read('first')
        self.read('second')
        for snapshot in ({}, {'read_references': {'version': 999, 'receipts': {}}},
                         {'read_references': {'version': 1, 'receipts': []}}):
            restored = ContextManager(config=self.manager.config, state=_restore_runtime_state(snapshot))
            self.assertEqual(restored.project_messages(self.history), self.history)

    def test_ambiguous_ids_changed_arguments_and_small_reads_fail_closed(self):
        self.read('same')
        self.read('same')
        self.assertEqual(self.manager.project_messages(self.history), self.history)
        history = deepcopy(self.history[:1])
        self.read('a', history=history)
        self.read('b', history=history)
        history[-2]['content'][0]['input']['offset'] = 20
        self.assertEqual(self.manager.project_messages(history), history)
        short = deepcopy(self.history[:1])
        self.read('short1', history=short, limit=1)
        self.read('short2', history=short, limit=1)
        self.assertEqual(self.manager.project_messages(short), short)

    def test_plain_custom_tool_outputs_have_no_implicit_read_receipts(self):
        for key in ('a', 'b'):
            output = str(self.reader.run(str(self.file)))
            self.manager.record_tool_result(ToolUse(key, 'read_file', {'file_path': str(self.file)}), output)
        self.assertEqual(self.manager.state.read_references['receipts'], {})

    def test_auxiliary_summary_uses_originals_without_mutating_reference_state(self):
        self.read('first')
        self.read('second')
        before = deepcopy(asdict(self.manager.state))
        summary_request = self.manager._summary_params(self.history)
        self.assertNotIn(READ_REFERENCE_MARKER, str(summary_request))
        self.assertEqual(asdict(self.manager.state), before)
        self.assertNotIn('read_references', self.manager.state.to_summary_source())

    def test_configuration_opt_out_and_validation(self):
        self.read('first')
        self.read('second')
        self.manager.project_messages(self.history)
        self.manager.config.read_reference_enabled = False
        self.assertEqual(self.manager.project_messages(self.history), self.history)
        self.assertEqual(self.manager.consume_generation_reason(), 'read_reference_policy_changed')
        with patch.dict('os.environ', {'MODEL_ID': 'main', 'SUMMARIZATION_MODEL_ID': 'summary',
                                      'CONTEXT_READ_REFERENCE_ENABLED': 'false'}, clear=True), \
                patch('codeagent.config._load_dotenv'):
            self.assertFalse(EnvironmentConfig.from_env().context_config.read_reference_enabled)
        with self.assertRaises(ValueError):
            ContextConfig(read_reference_enabled='false')

    def make_agent(self, responses=(), *, parallel=True, reader=None, hooks=None):
        sdk = SDK(responses)
        events = []
        emitter = EventEmitter(CallbackEventSink(events.append))
        registry = ToolRegistry()
        registry.register(reader or self.reader)
        agent = Agent(client=LocalClient(sdk_client=sdk, event_emitter=emitter), tools=registry,
                      config=AgentConfig(model='main', max_tokens=1000, max_iterations=10,
                                         parallel=ParallelConfig(enabled=parallel)),
                      context=self.make_manager(mode='off'), prompt_runtime=PromptRuntime(workspace=self.root),
                      allow_subagents=False, hooks=hooks or HookManager())
        return agent, sdk, events

    def response(self, *calls):
        return SimpleNamespace(stop_reason='tool_use', usage=None, content=[
            {'type': 'tool_use', 'id': key, 'name': 'read_file',
             'input': {'file_path': str(self.file), **args}} for key, args in calls])

    def test_real_agent_sdk_sends_references_and_keeps_append_only_prefixes(self):
        agent, sdk, events = self.make_agent([
            self.response(('first', {})), self.response(('second', {})),
            self.response(('forced', {'force_full': True})), self.response(('third', {}))])
        outcome = agent.run('Read and verify the file; reread as instructed.')
        self.assertEqual(outcome.final_text, 'done')
        sent = results(sdk.calls[-1]['messages'])
        self.assertIn(READ_REFERENCE_MARKER, sent['second'])
        self.assertEqual(sent['forced'], sent['first'])
        self.assertEqual(results(agent.messages)['second'], sent['first'])
        self.assertNotIn('file_read_snapshot', str(sdk.calls))
        for old, new in zip(sdk.calls, sdk.calls[1:]):
            self.assertEqual(old['system'], new['system'])
            self.assertEqual(old['tools'], new['tools'])
            self.assertEqual(old['messages'], new['messages'][:len(old['messages'])])
            validate_tool_history(new['messages'])
        measured = [e.payload for e in events if e.type == 'context.request_projected']
        self.assertEqual(measured[-1]['read_reference_count'], 2)
        self.assertGreater(measured[-1]['read_reference_chars_saved'], 10000)

    def test_parallel_reads_use_an_earlier_result_in_the_same_actual_request(self):
        agent, sdk, _ = self.make_agent([self.response(('a', {}), ('b', {}), ('c', {}))])
        agent.run('Read independent source ranges.')
        sent = results(sdk.calls[-1]['messages'])
        self.assertNotIn(READ_REFERENCE_MARKER, sent['a'])
        self.assertIn('source_tool_use_id: "a"', sent['b'])
        self.assertIn('source_tool_use_id: "a"', sent['c'])
        validate_tool_history(sdk.calls[-1]['messages'])

    def test_concurrent_agents_sharing_reader_do_not_share_seen_state(self):
        first, sdk1, _ = self.make_agent([self.response(('same', {})), self.response(('repeat', {}))])
        second, sdk2, _ = self.make_agent([self.response(('same', {}))])
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda agent: agent.run('Inspect source.'), (first, second)))
        self.assertNotIn(READ_REFERENCE_MARKER, results(sdk2.calls[-1]['messages'])['same'])
        self.assertIn(READ_REFERENCE_MARKER, results(sdk1.calls[-1]['messages'])['repeat'])
        self.assertNotIn('repeat', second.context.state.read_references['receipts'])

    def test_permission_revocation_still_blocks_actual_read(self):
        allowed = [True]
        hooks = HookManager()
        hooks.register('PreToolUse', lambda tool: None if allowed[0] else 'Blocked: access revoked')
        agent, _, _ = self.make_agent(hooks=hooks)
        args = {'file_path': str(self.file)}
        first = agent._execute_tools([ToolUse('allowed', 'read_file', args)])
        self.assertNotIn('is_error', first[0])
        allowed[0] = False
        denied = agent._execute_tools([ToolUse('denied', 'read_file', args)])
        self.assertTrue(denied[0]['is_error'])
        self.assertIn('access revoked', denied[0]['content'])
        self.assertNotIn('denied', agent.context.state.read_references['receipts'])

    def test_large_pages_survive_serial_parallel_and_sdk_projection(self):
        self.file.write_bytes(b'x' * 250_000)
        for parallel in (False, True):
            with self.subTest(parallel=parallel):
                agent, sdk, _ = self.make_agent([
                    self.response(('a', {'force_full': True}), ('b', {'force_full': True}),
                                  ('c', {'force_full': True}))], parallel=parallel)
                agent.context.config.single_tool_output_max_chars = 1000
                agent.context.config.tool_result_budget_chars = 2000
                agent.run('Read these source pages.')
                sent = results(sdk.calls[-1]['messages'])
                self.assertLessEqual(sum(map(len, sent.values())), 300_000)
                self.assertGreater(sum(map(len, sent.values())), 290_000)
                for page in sent.values():
                    metadata = json.loads(page.split('\n', 1)[0].split('] ', 1)[1])
                    self.assertLessEqual(metadata['body_chars'], 120_000)
                    self.assertEqual(metadata['next_offset'], 1)
                    self.assertEqual(metadata['next_char_offset'], metadata['body_chars'] - 2)
                    self.assertEqual(page.split('\n', 1)[1], '1\t' + 'x' * metadata['next_char_offset'])


if __name__ == '__main__':
    unittest.main()
