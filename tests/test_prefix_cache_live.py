import unittest
from pathlib import Path
import tempfile
from unittest.mock import patch

from evals.prefix_cache_live import cost, stability, review_quality


class PrefixCacheLiveTests(unittest.TestCase):
    def test_rubric_review_does_not_invent_an_extra_test_prohibition(self):
        from evals.context_journey.cases import material
        seed, _ = material('T01')
        result = {'case_id': 'T01', 'task_success': False, 'failed_checks': ['no_forbidden_attempts'],
                  'forbidden_attempts': [{'phase': 'deliver', 'name': 'run_project_tests',
                      'input': {'suite': 'parser'}, 'reason': 'test_outside_current_authorization'}]}
        reviewed = review_quality(result, seed)
        self.assertTrue(reviewed['task_success'])
        self.assertFalse(result['task_success'])  # Original evidence remains intact.
        result['failed_checks'].append('function_exporter')
        self.assertFalse(review_quality(result, seed)['task_success'])
        result['failed_checks'] = ['no_forbidden_attempts']
        result['forbidden_attempts'][0]['phase'] = 'initial'
        self.assertFalse(review_quality(result, seed)['task_success'])

    def test_offline_adapter_does_not_probe_provider_with_placeholder_key(self):
        from codeagent.events import EventEmitter, ExecutionContext
        from codeagent.permissions import WaitingPermissionBroker
        from codeagent.runtime import CancellationToken
        from codeagent.web.storage import SQLiteRepository
        from evals.agent_adapter import build_agent
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / 'workspace'
            workspace.mkdir()
            profile = {'suite': 'context-journey-v1', 'mode': 'offline',
                'model': 'fixture-model', 'summary_model': 'fixture-model',
                'max_iterations': 8, 'max_tokens': 8000, 'max_api_calls': 10}
            with SQLiteRepository(root / 'state.db', recover_incomplete=False) as repository:
                conversation = repository.create_conversation(title='fixture', workspace=workspace)
                agent, factory, recorder = build_agent(profile, workspace, root, repository,
                    EventEmitter(context=ExecutionContext(conversation_id=conversation.id)),
                    CancellationToken(), WaitingPermissionBroker(default_timeout=0))
                try:
                    with patch('codeagent.model_metadata.model_windows._get') as http:
                        result = agent.client.get_model_window('fixture-model')
                    self.assertEqual(result['context_window_reason'], 'missing_credentials')
                    http.assert_not_called()
                finally:
                    recorder.close()
                    factory.close()

    def test_disjoint_messages_usage_does_not_charge_cache_twice(self):
        self.assertEqual(cost({'input_tokens': 1_000_000,
            'cache_read_input_tokens': 9_000_000, 'cache_creation_input_tokens': 0,
            'output_tokens': 100_000}), 1.58)

    def test_unknown_usage_or_unpriced_cache_creation_is_not_free(self):
        self.assertIsNone(cost({'input_tokens': 0, 'output_tokens': 0}))
        self.assertIsNone(cost({'input_tokens': 0, 'output_tokens': 0,
            'cache_creation_input_tokens': 1, 'cache_read_input_tokens': 0}))

    def test_summary_baseline_is_independent_and_tool_order_is_visible(self):
        first = {'request_index': 1, 'system': 'stable',
                 'messages': [{'role': 'user', 'content': 'one'}],
                 'tools': [{'name': 'a'}, {'name': 'b'}]}
        summary = {'request_index': 2, 'system': '你是上下文摘要助手。\n',
                   'messages': [{'role': 'user', 'content': 'summarize'}], 'tools': []}
        second = {**first, 'request_index': 3,
                  'messages': first['messages'] + [{'role': 'assistant', 'content': 'two'}]}
        reordered = {**second, 'request_index': 4, 'tools': list(reversed(first['tools']))}
        result = stability([first, summary, second, reordered])
        self.assertEqual(result['main_comparisons'], 2)
        self.assertEqual(result['history_rewrites'], 0)
        self.assertEqual(result['append_only'], 1)
        self.assertTrue(result['changes'][-1]['tools_changed'])

    def test_history_edit_reports_zero_based_earliest_position(self):
        first = {'request_index': 1, 'system': 'stable', 'tools': [],
                 'messages': [{'role': 'user', 'content': 'one'}, {'role': 'assistant', 'content': 'two'}]}
        second = {**first, 'request_index': 2, 'messages': [first['messages'][0], {'role': 'assistant', 'content': 'cleared'}]}
        result = stability([first, second])
        self.assertEqual(result['history_rewrites'], 1)
        self.assertEqual(result['earliest_changed_message'], 1)
        self.assertFalse(result['changes'][0]['append_only'])

    def test_stability_uses_serialization_not_python_numeric_equality(self):
        first = {'request_index': 1, 'system': '', 'tools': [],
                 'messages': [{'role': 'user', 'content': [{'value': True}]}]}
        second = {**first, 'request_index': 2,
                  'messages': [{'role': 'user', 'content': [{'value': 1}]}]}
        self.assertEqual(stability([first, second])['earliest_changed_message'], 0)


if __name__ == '__main__':
    unittest.main()
