"""Offline Agent integration, using real files and real Bash/unittest processes."""
from copy import deepcopy
from dataclasses import replace
import json
import threading
from unittest.mock import patch

from codeagent.cvm import CvmConfig
from codeagent.cvm.state import CvmRuntime
from codeagent.events import CallbackEventSink, EventEmitter
from codeagent.runtime import CancelledError
from codeagent.tools import ToolDefinition
from codeagent.tools.base import ToolOutput
from codeagent.tools.bash import BashTool
from codeagent.tools.workspace import WorkspaceGuard
from cvm_fixtures import done, tool
from test_cvm_contract import ContractFixture


COMMAND = 'python -m unittest discover -s checks'


class VerificationSupervisionTests(ContractFixture):
    def setUp(self):
        super().setUp()
        (self.root / 'checks').mkdir()
        self.source = self.root / 'checks/test_sample.py'
        self.source.write_text('import unittest\nclass Sample(unittest.TestCase):\n def test_ok(self): self.assertEqual(1, 1)\n')
        (self.root / '.codeagent').mkdir()
        self.configuration = self.root / '.codeagent/cvm-verification.json'
        self.configuration.write_text(json.dumps({'version': 1, 'checks': [
            {'command': COMMAND, 'inputs': ['checks']},
        ]}))
        self.events = []

    def make(self, responses, mode='active', bash=None, **kwargs):
        responses = deepcopy(responses)
        for index, response in enumerate(responses):
            for block_index, block in enumerate(response['content']):
                if block['type'] == 'tool_use':
                    block['id'] = f'call-{index}-{block_index}'
        agent = self.agent(mode='shadow', responses=responses,
                           event_emitter=EventEmitter(CallbackEventSink(self.events.append)), **kwargs)
        agent.config.cvm = CvmConfig(contract='shadow', evidence='shadow', verification=mode)
        agent.tools.register(bash or BashTool(workspace_guard=WorkspaceGuard(self.root), output_root=self.root / 'logs'))
        def edit():
            self.source.write_text(self.source.read_text() + '# edited\n')
            return ToolOutput('edited', changed_files=[str(self.source)])
        agent.tools.register_handler(ToolDefinition('edit', '', {}), edit)
        agent.tools.register_handler(ToolDefinition('read', '', {}), lambda: ToolOutput('read'))
        return agent

    def advice(self, phase):
        return [e.payload for e in self.events if e.type == 'cvm.verification_advice' and e.payload['phase'] == phase]

    def statuses(self):
        return [e.payload for e in self.events if e.type == 'cvm.verification_status']

    def test_real_pass_edit_stale_once_recheck_current(self):
        agent = self.make([tool('bash', command=COMMAND), tool('edit'), tool('read'),
                           tool('read'), tool('bash', command=COMMAND), done()])
        agent.run('修改 Python 代码并检查')
        states = [s['checks'][0]['status'] for s in self.statuses()]
        self.assertIn('passed', states)
        self.assertIn('stale', states)
        self.assertEqual(states[-1], 'passed')
        self.assertEqual(len(self.advice('delivered')), 1)
        self.assertEqual(self.advice('delivered')[0]['reason'], 'stale')
        self.assertTrue(all(self.advice('followup')[-1][key] for key in ('attempted', 'completed', 'passed', 'current')))
        self.assertEqual(len(agent.client.requests), 6)  # No forced continuation.
        self.assertNotIn('CVM 验证建议', str(agent.client.requests[2]['messages']))  # edit -> test grace
        self.assertIn(COMMAND, str(agent.client.requests[3]['messages']))
        self.assertIn('CVM 验证建议', str(agent.client.requests[3]['messages']))

    def test_edit_immediately_test_no_reminder(self):
        agent = self.make([tool('edit'), tool('bash', command=COMMAND), done()])
        agent.run('修改 Python 代码并检查')
        self.assertFalse(self.advice('candidate'))
        self.assertEqual(self.statuses()[-1]['checks'][0]['status'], 'passed')

    def test_missing_check_reminds_once_and_does_not_run_command(self):
        agent = self.make([tool('edit'), tool('read'), tool('read'), tool('read'), done()])
        agent.run('修改代码')
        self.assertEqual(len(self.advice('delivered')), 1)
        self.assertEqual(self.advice('delivered')[0]['reason'], 'missing')
        self.assertFalse(agent._cvm.evidence.verifications)

    def test_question_preexisting_changes_and_docs_only_stay_quiet(self):
        self.source.write_text(self.source.read_text() + '# user change before task\n')
        agent = self.make([tool('read'), tool('read'), done()])
        agent.run('解释这个代码')
        self.assertFalse(self.advice('candidate'))
        agent = self.make([tool('edit_docs'), tool('read'), done()])
        def docs():
            (self.root / 'checks/README.md').write_text('docs')
            return ToolOutput('edited documentation')
        agent.tools.register_handler(ToolDefinition('edit_docs', '', {}), docs)
        agent.run('修改说明文档')
        self.assertFalse(self.advice('candidate'))

    def test_forbidden_tests_readonly_and_pending_followup_are_quiet(self):
        for prompt in ('修改代码，不要运行测试', '暂停开发，只做交接', '只修改文档，不要检查'):
            with self.subTest(prompt=prompt):
                agent = self.make([tool('edit'), tool('read'), done()])
                agent.run(prompt)
                self.assertFalse(self.advice('candidate'))
        agent = self.make([tool('edit'), done(), tool('read'), tool('read'), done()])
        agent.run('修改代码')
        agent.run('说说现在进展到哪里了')  # pending natural-language goal update
        self.assertFalse(self.advice('candidate'))
        agent = self.make([tool('read'), done()], read_only=True)
        agent.run('调查代码')
        self.assertFalse(self.advice('candidate'))

    def test_shadow_does_not_inject_and_off_emits_nothing(self):
        for mode in ('shadow', 'off'):
            self.events.clear()
            agent = self.make([tool('edit'), tool('read'), done()], mode=mode)
            agent.run('修改代码')
            self.assertFalse(self.advice('delivered'))
            self.assertNotIn('CVM 验证建议', str(agent.client.requests))
            if mode == 'off':
                self.assertFalse(self.statuses())
            else:
                self.assertTrue(self.advice('candidate'))

    def test_stop_refreshes_external_edit_without_extra_request(self):
        agent = self.make([tool('bash', command=COMMAND), done()])
        create = agent.client.create_message
        def model(**kwargs):
            if len(agent.client.requests) == 1:
                self.source.write_text(self.source.read_text() + '# external\n')
            return create(**kwargs)
        with patch.object(agent.client, 'create_message', side_effect=model):
            agent.run('检查代码')
        self.assertEqual(self.statuses()[-1]['checks'][0]['status'], 'stale')
        self.assertEqual(len(agent.client.requests), 2)

    def test_trimmed_or_cancelled_candidate_is_not_delivered(self):
        for cancel in (False, True):
            self.events.clear()
            agent = self.make([tool('edit'), tool('read'), done()])
            prepare = agent.context.prepare_before_model_call
            def trimmed(*args, **kwargs):
                return [m for m in prepare(*args, **kwargs) if 'CVM 验证建议' not in str(m.get('content'))]
            create = agent.client.create_message
            def cancelled(**kwargs):
                if len(agent.client.requests) >= 2:
                    raise CancelledError('cancel before request')
                return create(**kwargs)
            with patch.object(agent.context, 'prepare_before_model_call', side_effect=trimmed if not cancel else prepare), \
                 patch.object(agent.client, 'create_message', side_effect=cancelled if cancel else create):
                try:
                    agent.run('修改代码')
                except CancelledError:
                    self.assertTrue(cancel)
            self.assertTrue(self.advice('candidate'))
            self.assertFalse(self.advice('delivered'))

    def test_restore_deduplicates_and_new_task_has_new_baseline(self):
        agent = self.make([tool('edit'), tool('read'), done()])
        agent.run('修改代码')
        payload = agent.export_cvm_state()
        self.assertEqual(len(self.advice('delivered')), 1)
        resumed = self.make([tool('read'), tool('read'), done(), done()])
        resumed._cvm.config = resumed.config.cvm
        resumed.restore_cvm_state(json.loads(json.dumps(payload)))
        resumed.run('继续')
        self.assertEqual(len(self.advice('delivered')), 1)
        resumed.run('新任务：解释代码')
        self.assertEqual(len(self.advice('delivered')), 1)
        self.assertFalse(resumed._cvm.supervision['delivered'])
        corrupt = deepcopy(payload)
        corrupt['supervision']['delivered'] = {}
        runtime = CvmRuntime(agent.config.cvm, self.root)
        runtime.restore(corrupt)
        self.assertFalse(runtime.recording)

    def test_unrecognized_command_and_unbound_results_are_unknown(self):
        agent = self.make([tool('edit'), tool('bash', command='echo custom-test'), done()])
        agent.run('修改代码')
        self.assertTrue(all(row['status'] == 'unknown' for row in self.statuses()[-1]['checks']))
        self.assertFalse(self.advice('delivered'))

    def test_bad_config_is_visible_and_not_executed(self):
        for config in ({'version': 9, 'checks': []}, {'version': 1, 'checks': [{'command': COMMAND, 'inputs': ['../escape']}]},
                       {'version': 1, 'checks': [{'command': 'python -m unittest; echo x', 'inputs': ['checks']}]}):
            self.configuration.write_text(json.dumps(config))
            agent = self.make([tool('edit'), tool('read'), done()])
            agent.run('修改代码')
            self.assertFalse(self.statuses()[-1]['configuration_valid'])
            self.assertFalse(self.advice('delivered'))

    def test_failure_survives_unrelated_pass_then_target_pass_clears(self):
        self.source.write_text(self.source.read_text().replace('1, 1', '1, 2'))
        other = self.root / 'other'
        other.mkdir()
        (other / 'test_other.py').write_text('import unittest\nclass T(unittest.TestCase):\n def test_ok(self): pass\n')
        command2 = 'python -m unittest discover -s other'
        config = json.loads(self.configuration.read_text())
        config['checks'].append({'command': command2, 'inputs': ['other']})
        self.configuration.write_text(json.dumps(config))
        agent = self.make([tool('bash', command=COMMAND), tool('repair'), tool('bash', command=command2),
                           tool('bash', command=COMMAND), done()])
        def repair():
            # Change size too: CPython's timestamp/size bytecode cache can otherwise
            # reuse the failing module when both real processes run within a second.
            self.source.write_text(self.source.read_text().replace('1, 2', '1, 1') + '# repaired\n')
            return ToolOutput('repaired')
        agent.tools.register_handler(ToolDefinition('repair', '', {}), repair)
        agent.run('修复测试')
        self.assertTrue(any([r['status'] for r in status['checks']] == ['failed', 'passed'] for status in self.statuses()))
        self.assertEqual([r['status'] for r in self.statuses()[-1]['checks']], ['passed', 'passed'])

    def test_runtime_config_edit_invalidates_scope(self):
        agent = self.make([tool('bash', command=COMMAND), tool('edit_config'), done()])
        def edit_config():
            self.configuration.write_text(self.configuration.read_text() + '\n')
            return ToolOutput('configuration changed')
        agent.tools.register_handler(ToolDefinition('edit_config', '', {}), edit_config)
        agent.run('检查并修改配置')
        self.assertEqual(self.statuses()[-1]['checks'][0]['status'], 'unknown')
        self.assertFalse(self.statuses()[-1]['configuration_valid'])

    def test_removed_project_check_is_not_rebound_from_historical_definition(self):
        agent = self.make([tool('bash', command=COMMAND), done(), tool('bash', command=COMMAND), done()])
        agent.run('检查项目')
        self.configuration.write_text(json.dumps({'version': 1, 'checks': [
            {'command': 'python -m unittest discover -s other', 'inputs': ['other']},
        ]}))
        agent.run('继续')
        self.assertEqual(agent._cvm.evidence.verifications[-1]['definition_id'], '')
        self.assertEqual(self.statuses()[-1]['checks'][-1]['status'], 'unknown')

    def test_failed_check_without_edit_gets_one_advisory(self):
        self.source.write_text(self.source.read_text().replace('1, 1', '1, 2'))
        agent = self.make([tool('bash', command=COMMAND), tool('read'), tool('read'), done()])
        agent.run('检查 Python 项目')
        self.assertEqual(len(self.advice('delivered')), 1)
        self.assertEqual(self.advice('delivered')[0]['reason'], 'failed')

    def test_unknown_incomplete_and_empty_checks_never_become_passed(self):
        outputs = [
            ToolOutput('Ran 0 tests in 0.001s\n\nOK', exit_code=0, process_id=42),
            ToolOutput('Ran 1 test in 0.001s\n\nOK (skipped=1)', exit_code=0, process_id=42),
            ToolOutput('incomplete', exit_code=0, process_id=42),
            ToolOutput('waiting', process_id=42, process_running=True),
            ToolOutput('timeout', status='error', outcome='timeout', process_id=42),
            ToolOutput('cancelled', status='error', outcome='cancelled', process_id=42),
            ToolOutput('denied', status='blocked', outcome='permission_denied'),
        ]
        incomplete = ToolOutput('Ran 1 test in 0.001s\n\nOK', exit_code=0, process_id=42)
        incomplete.source_complete = False
        outputs.append(incomplete)
        class ControlledBash(BashTool):
            def run(self, command):
                return self.result
        for output in outputs:
            with self.subTest(outcome=output.outcome, text=str(output)):
                self.events.clear()
                bash = ControlledBash(workspace_guard=WorkspaceGuard(self.root), output_root=self.root / 'logs')
                bash.result = output
                agent = self.make([tool('edit'), tool('bash', command=COMMAND), done()], bash=bash)
                agent.run('修改并检查代码')
                self.assertIn(self.statuses()[-1]['checks'][0]['status'], {'unknown', 'incomplete'})
                self.assertFalse(self.advice('delivered'))

    def test_parallel_reads_are_one_grace_opportunity_not_two_reminders(self):
        batch = {'stop_reason': 'tool_use', 'content': [
            {'type': 'tool_use', 'id': str(n), 'name': 'parallel_read', 'input': {'number': n}} for n in (1, 2)]}
        agent = self.make([tool('edit'), batch, done()])
        barrier = threading.Barrier(2)
        def read(number):
            barrier.wait(timeout=5)
            return ToolOutput(str(number))
        agent.tools.register_handler(ToolDefinition('parallel_read', '', {}, effect='read', reentrant=True), read)
        agent.run('修改代码')
        self.assertEqual(len(self.advice('delivered')), 1)
        self.assertEqual(agent._cvm.supervision['batches'], 2)
        self.assertEqual([o['tool_use_id'] for o in agent._cvm.evidence.observations][-2:], ['call-1-0', 'call-1-1'])

    def test_retry_records_attempts_but_delivers_only_one_advice(self):
        class RetryingBash(BashTool):
            def run(self, command):
                return self.outputs.pop(0)
        bash = RetryingBash(workspace_guard=WorkspaceGuard(self.root), output_root=self.root / 'logs')
        bash.outputs = [ToolOutput('transient', status='error', outcome='transient', retryable=True, retry_safe=True),
                        ToolOutput('Ran 1 test in 0.001s\n\nOK', exit_code=0, process_id=42)]
        agent = self.make([tool('edit'), tool('read'), tool('bash', command=COMMAND), done()], bash=bash)
        agent._loop_guard.config = replace(agent._loop_guard.config, tool_max_retries=1, retry_delay_seconds=0)
        agent.run('修改代码')
        self.assertEqual(len(self.advice('delivered')), 1)
        self.assertEqual(len(self.advice('followup')), 2)
        self.assertFalse(self.advice('followup')[0]['completed'])
        self.assertTrue(self.advice('followup')[1]['passed'])
