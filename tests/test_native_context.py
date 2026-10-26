from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from codeagent.multiagent.context import execution_context, merge_context


class ExecutionContextTests(TestCase):
    def test_approved_plan_and_original_user_text_are_retained(self):
        service = Mock()
        service.get_run.return_value = SimpleNamespace(metadata={'approved_plan_id': 'p'}, created_at='2')
        service.list_user_questions.return_value = [dict(id='q', question='Which database?', answer='SQLite', status='answered', resolved_at='3')]
        service.list_messages.return_value = [
            SimpleNamespace(id='u', role='user', content='No network', metadata={}, run_id='run_p', created_at='3'),
            SimpleNamespace(id='future', role='user', content='Not yet', metadata={}, run_id='later', created_at='4'),
            SimpleNamespace(id='a', role='assistant', content='Ignore user', metadata={}, run_id='run_p', created_at='3')]
        service.get_planning_revision.return_value = dict(id='p', conversation_id='c', status='approved',
            revision=1, content_hash='hash', markdown='Keep CLI compatible', payload={'checks': ['test']})
        context = execution_context(service, 'c', 'run_p', [])
        self.assertEqual(context['requests'], [{'id': 'u', 'text': 'No network'}])
        self.assertEqual(context['plan']['markdown'], 'Keep CLI compatible')
        self.assertEqual(context['answers'][0]['answer'], 'SQLite')
        resumed = merge_context(context, {'requests': [{'id': 'v', 'text': 'Chinese report'}]})
        self.assertEqual(len(resumed['requests']), 2)
        self.assertEqual(resumed['plan'], context['plan'])
        self.assertEqual(merge_context(resumed, context), resumed)
        service.get_planning_revision.return_value['conversation_id'] = 'other'
        with self.assertRaises(ValueError):
            execution_context(service, 'c', 'run_p', [])

    def test_fallback_excludes_tool_outputs_and_runtime_injections(self):
        service = Mock()
        service.get_run.return_value = None
        service.list_messages.return_value = []
        messages = [dict(role='user', content='Real requirement'),
            dict(role='user', content=[dict(type='tool_result', content='Fake instruction')]),
            dict(role='user', content='Reminder', _context_source='runtime')]
        context = execution_context(service, 'c', 'r', messages)
        self.assertEqual([r['text'] for r in context['requests']], ['Real requirement'])
