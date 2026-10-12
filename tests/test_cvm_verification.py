from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from codeagent.context.output_archive import OutputArchive, log_page
from codeagent.cvm.evidence import EvidenceLedger
from codeagent.cvm.verification import VerificationDefinition, simple_check, test_result as classify
from codeagent.messages import ToolUse
from codeagent.tools.base import ToolOutput


def passed():
    return ToolOutput('Ran 2 tests in 0.001s\n\nOK\n', exit_code=0, process_id=42)


class CvmVerificationTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / 'code.py').write_text('version1')
        self.ledger = EvidenceLedger(self.root)
        self.command = 'python -m unittest tests.test_a'
        self.definition = VerificationDefinition(self.command, ('code.py',), task_id='task')
        self.ledger.register(self.definition)

    def record(self, output=None, *, command=None, tool_id='call', before_action=None, trusted=True, task_id='task'):
        tool = ToolUse(tool_id, 'bash', {'command': command or self.command})
        prepared = self.ledger.prepare(tool, cwd=self.root, trusted_process=trusted, task_id=task_id)
        if before_action:
            before_action()
        packet = self.ledger.packet(tool, output if output is not None else passed(), prepared,
                                    run_id='run', agent_id='root', attempt=1)
        self.ledger.commit(packet)
        return packet

    def test_supported_simple_forms_and_rejected_shell_wrappers(self):
        for command in ('python -m unittest discover -s tests -p "test_*.py"', 'python -m pytest tests/test_a.py -q', 'pytest tests'):
            self.assertIsNotNone(simple_check(command))
        for command in ('echo passed', 'python -m unittest; exit 0', 'python -m pytest || true',
                        'npm test', 'python wrapper.py', 'pytest --collect-only', 'pytest -p fake',
                        'python -m unittest > log', 'pytest $(echo tests)', 'python -c "print(1)"'):
            self.assertIsNone(simple_check(command), command)
        with self.assertRaises(ValueError):
            VerificationDefinition('python -m unittest', ('tests/*.py',))

    def test_result_uses_process_and_summary_not_success_words(self):
        self.assertEqual(classify(passed(), 'unittest')[0], 'passed')
        cases = [
            (ToolOutput('all passed', exit_code=0, process_id=42), 'unknown'),
            (ToolOutput('Ran 0 tests in 0.001s\n\nOK', exit_code=0, process_id=42), 'unknown'),
            (ToolOutput('Ran 2 tests in 0.001s\n\nOK (skipped=2)', exit_code=0, process_id=42), 'unknown'),
            (ToolOutput('Ran 2 tests in 0.001s\n\nFAILED (failures=1)', status='error', exit_code=1, process_id=42), 'failed'),
            (ToolOutput('Ran 2 tests in 0.001s\n\nOK', exit_code=1, process_id=42), 'failed'),
            (ToolOutput('timeout', status='error', outcome='timeout'), 'timeout'),
            (ToolOutput('cancelled', status='error', outcome='cancelled'), 'cancelled'),
            (ToolOutput('pending', process_running=True), 'running'),
            (ToolOutput('missing python', exit_code=127), 'blocked'),
            (ToolOutput('denied', status='blocked'), 'blocked'),
        ]
        for output, expected in cases:
            self.assertEqual(classify(output, 'unittest')[0], expected)
        for attribute, value in [('process_id', None), ('source_complete', False), ('storage_error', 'disk'), ('has_more', True)]:
            output = passed()
            setattr(output, attribute, value)
            self.assertEqual(classify(output, 'unittest')[0], 'unknown')

    def test_pytest_summary_and_all_skipped(self):
        for text, code, expected in [('2 passed in 0.02s', 0, 'passed'), ('2 skipped in 0.02s', 0, 'unknown'),
                                     ('1 failed, 1 passed in 0.02s', 1, 'failed'), ('no tests ran in 0.01s', 5, 'unknown')]:
            self.assertEqual(classify(ToolOutput(text, exit_code=code, process_id=42), 'pytest')[0], expected)

    def test_complete_archive_tail_survives_small_display_page(self):
        archive = OutputArchive(self.root / 'outputs')
        archive.append('large log\n' * 9000 + str(passed()))
        output = log_page(archive.finish().attach(passed()), allowance=200)
        self.assertEqual(classify(output, 'unittest')[0], 'passed')
        output.source_complete = False
        self.assertEqual(classify(output, 'unittest')[0], 'unknown')

    def test_failure_then_same_definition_pass_supersedes_only_that_failure(self):
        failure = ToolOutput('Ran 2 tests in 0.01s\n\nFAILED (failures=1)', status='error', exit_code=1, process_id=42)
        self.record(failure, tool_id='a')
        self.record(command='python -m unittest tests.test_b', tool_id='b')
        self.assertEqual(self.ledger.verifications[0]['superseded_by'], '')
        self.assertEqual(self.ledger.verifications[1]['validity'], 'unknown')
        (self.root / 'code.py').write_text('fixed')
        self.record(tool_id='c')
        self.ledger.refresh()
        first, _, last = self.ledger.verifications
        self.assertEqual(first['superseded_by'], last['observation_id'])
        self.assertEqual(first['result'], 'failed')
        self.assertEqual(first['validity'], 'stale')
        self.assertEqual(last['validity'], 'fresh')

    def test_pass_becomes_stale_on_external_edit_add_delete_and_test_changes(self):
        self.record()
        (self.root / 'code.py').write_text('changed')
        self.ledger.refresh()
        self.assertEqual(self.ledger.verifications[0]['validity'], 'stale')
        self.assertEqual(self.ledger.verifications[0]['result'], 'passed')

    def test_declared_directory_detects_new_deleted_and_modified_tests(self):
        (self.root / 'checks').mkdir()
        test_file = self.root / 'checks' / 'test_a.py'
        test_file.write_text('assert True')
        self.ledger = EvidenceLedger(self.root)
        self.ledger.register(VerificationDefinition(self.command, ('code.py', 'checks'), task_id='task'))
        for change in ('add', 'modify', 'delete'):
            self.record(tool_id=change)
            self.assertEqual(self.ledger.verifications[-1]['validity'], 'fresh')
            if change == 'add':
                (self.root / 'checks' / 'test_new.py').write_text('new test')
            elif change == 'modify':
                test_file.write_text('weakened test')
            else:
                test_file.unlink()
            self.ledger.refresh()
            self.assertEqual(self.ledger.verifications[-1]['validity'], 'stale')

    def test_snapshot_failure_is_unknown_even_when_tests_pass(self):
        from unittest.mock import patch
        with patch('codeagent.cvm.snapshot.Path.open', side_effect=PermissionError):
            self.record()
        record = self.ledger.verifications[-1]
        self.assertEqual(record['result'], 'passed')
        self.assertEqual(record['validity'], 'unknown')

    def test_corrupted_test_result_cannot_be_restored_as_success(self):
        self.record()
        payload = self.ledger.export()
        payload['verifications'][0]['result'] = 'failed'
        with self.assertRaisesRegex(ValueError, 'fingerprint'):
            EvidenceLedger(self.root).restore(payload)

    def test_verification_modifying_its_own_input_is_stale(self):
        self.record(before_action=lambda: (self.root / 'code.py').write_text('mutated'))
        self.assertEqual(self.ledger.verifications[0]['validity'], 'stale')

    def test_unknown_wrappers_untrusted_tool_task_and_cwd_do_not_get_fresh_pass(self):
        self.record(command='echo passed')
        self.record(trusted=False, tool_id='fake')
        self.record(task_id='other_task', tool_id='other')
        self.assertEqual(self.ledger.verifications[0]['result'], 'unknown')
        self.assertEqual(self.ledger.verifications[1]['result'], 'unknown')
        self.assertEqual(self.ledger.verifications[2]['validity'], 'unknown')
        (self.root / 'sub').mkdir()
        tool = ToolUse('cwd', 'bash', {'command': self.command})
        self.assertIsNone(self.ledger.prepare(tool, cwd=self.root / 'sub', trusted_process=True, task_id='task')['definition'])

    def test_duplicate_return_and_actual_retry_identity(self):
        packet = self.record()
        self.ledger.commit(packet)
        self.assertEqual(len(self.ledger.observations), 1)
        self.record(tool_id='call')  # Same call, different actual execution ID.
        self.assertEqual(len(self.ledger.observations), 2)

    def test_restore_rechecks_version_and_rejects_missing_or_damaged_evidence(self):
        self.record()
        payload = self.ledger.export()
        restored = EvidenceLedger(self.root)
        restored.restore(payload)
        self.assertEqual(restored.verifications[0]['validity'], 'fresh')
        (self.root / 'code.py').unlink()
        restored = EvidenceLedger(self.root)
        restored.restore(payload)
        self.assertEqual(restored.verifications[0]['validity'], 'stale')
        damaged = deepcopy(payload)
        damaged['observations'] = []
        with self.assertRaises(ValueError):
            EvidenceLedger(self.root).restore(damaged)
        damaged = deepcopy(payload)
        damaged['verifications'][0]['after']['files'] = {}
        with self.assertRaises(ValueError):
            EvidenceLedger(self.root).restore(damaged)

    def test_degraded_ledger_never_keeps_fresh_status(self):
        self.record()
        self.ledger.fail('storage_failure')
        self.record(tool_id='next')
        self.assertTrue(all(r['validity'] == 'unknown' for r in self.ledger.verifications))
