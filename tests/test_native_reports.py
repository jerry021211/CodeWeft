from types import SimpleNamespace
from unittest import TestCase

from codeagent.multiagent.reports import worker_report, tool_evidence
from codeagent.tools.base import ToolOutput


class WorkerReportTests(TestCase):
    def test_claimed_success_does_not_override_failed_runtime_evidence(self):
        observed = tool_evidence(SimpleNamespace(name='bash', input={'command': 'test'}),
                                 ToolOutput('failed', status='error', exit_code=1))
        report = worker_report('{"summary":"done","checks":["all passed"],"limitations":[]}', 'end_turn', [observed])
        self.assertEqual(report['verification'], 'not_run')
        self.assertEqual(report['tool_evidence'][0]['exit_code'], 1)
        self.assertEqual(report['tool_evidence'][0]['status'], 'error')
        self.assertTrue(report['structured'])

    def test_old_plain_text_is_readable_but_not_verified(self):
        report = worker_report('all tests passed', 'end_turn', [])
        self.assertFalse(report['structured'])
        self.assertEqual(report['claims']['summary'], 'all tests passed')
        self.assertEqual(report['verification'], 'not_run')

    def test_fenced_json_and_invalid_optional_fields(self):
        report = worker_report('```json\n{"summary":"done","changes":["a.py"],"checks":true}\n```', 'end_turn', [])
        self.assertEqual(report['claims']['changes'], ['a.py'])
        self.assertNotIn('checks', report['claims'])

    def test_evidence_references_do_not_duplicate_large_file_bodies(self):
        output = ToolOutput('written', changed_files=('a.py',))
        evidence = tool_evidence(SimpleNamespace(name='write_file', input={'path': 'a.py', 'content': 'x' * 100_000}), output)
        self.assertNotIn('input', evidence)
        self.assertEqual(evidence['label'], 'a.py')
        self.assertEqual(evidence['execution_id'], output.execution_id)
        self.assertEqual(len(evidence['input_hash']), 64)
