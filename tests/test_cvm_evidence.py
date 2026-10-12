"""Stage 2 tests run real Agent boundaries with scripted models, never network APIs."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from codeagent.cvm import CvmConfig
from codeagent.cvm.state import CvmRuntime
from codeagent.hooks import HookManager
from codeagent.messages import validate_tool_history
from codeagent.runtime import CancelledError
from codeagent.tools import ToolDefinition
from codeagent.tools.base import ToolOutput
from codeagent.tools.bash import BashTool
from codeagent.tools.workspace import WorkspaceGuard
from cvm_fixtures import done, tool
from test_cvm_contract import ContractFixture


class CvmEvidenceTests(ContractFixture):
    def agent_with_evidence(self, responses=None, **kwargs):
        agent = self.agent(mode='shadow', responses=responses, **kwargs)
        agent.config.cvm = CvmConfig(contract='shadow', evidence='shadow')
        return agent

    def ledger(self, agent):
        return agent.export_cvm_state()['evidence']

    def test_shadow_provider_requests_and_calls_match_off(self):
        responses = [tool('probe'), done('all passed')]
        agents = [self.agent(mode='off', responses=responses), self.agent_with_evidence(responses)]
        for agent in agents:
            agent.tools.register_handler(ToolDefinition('probe', '', {}), lambda: ToolOutput('failed', status='error', exit_code=1))
            with patch('codeagent.context.output_archive.uuid.uuid4', return_value=SimpleNamespace(hex='stable-output')):
                agent.run('inspect')
            for artifact in (self.root / 'outputs').rglob('stable-output.*'):
                artifact.unlink()
        self.assertEqual(agents[0].client.requests, agents[1].client.requests)
        ledger = self.ledger(agents[1])
        self.assertEqual(len(ledger['observations']), 1)
        self.assertEqual(ledger['observations'][0]['exit_code'], 1)
        self.assertEqual(ledger['observations'][0]['status'], 'error')

    def test_post_hook_short_circuit_and_mutation_cannot_erase_raw_facts(self):
        hooks = HookManager()
        def mutate(call, output):
            output.exit_code = 0
            return 'short circuit'
        hooks.register('PostToolUse', mutate)
        agent = self.agent_with_evidence([tool('probe'), done('all passed')], hooks=hooks)
        original = ToolOutput('failed', status='error', exit_code=1)
        agent.tools.register_handler(ToolDefinition('probe', '', {}), lambda: original)
        agent.run('inspect')
        observation = self.ledger(agent)['observations'][0]
        self.assertEqual(observation['exit_code'], 1)
        self.assertEqual(observation['execution_id'], original.execution_id)

    def test_denial_and_invalid_arguments_record_unexecuted(self):
        hooks = HookManager()
        hooks.register('PreToolUse', lambda call: 'denied')
        agent = self.agent_with_evidence([tool('probe'), done('blocked')], hooks=hooks)
        handler = unittest.mock.Mock(return_value='never')
        agent.tools.register_handler(ToolDefinition('probe', '', {}), handler)
        agent.run('inspect')
        handler.assert_not_called()
        observation = self.ledger(agent)['observations'][0]
        self.assertFalse(observation['executed'])
        self.assertEqual(observation['status'], 'blocked')
        agent = self.agent_with_evidence([tool('probe'), done('invalid')])
        agent.tools.register_handler(ToolDefinition('probe', '', {'required': ['value']}), handler)
        agent.run('inspect')
        self.assertFalse(self.ledger(agent)['observations'][0]['executed'])

    def test_real_retry_gets_distinct_attempt_and_execution_ids(self):
        agent = self.agent_with_evidence([tool('probe'), done('done')])
        agent._loop_guard.config = replace(agent._loop_guard.config, tool_max_retries=1, retry_delay_seconds=0)
        outputs = [ToolOutput('temporary', status='error', outcome='transient', retryable=True, retry_safe=True),
                   ToolOutput('success')]
        agent.tools.register_handler(ToolDefinition('probe', '', {}), lambda: outputs.pop(0))
        agent.run('inspect')
        observations = self.ledger(agent)['observations']
        self.assertEqual([o['attempt'] for o in observations], [1, 2])
        self.assertNotEqual(observations[0]['execution_id'], observations[1]['execution_id'])

    def test_parallel_out_of_order_results_commit_in_request_order(self):
        barrier, later_done = threading.Barrier(2), threading.Event()
        def read(number):
            barrier.wait(timeout=5)
            if number == 1:
                if not later_done.wait(timeout=5):
                    raise RuntimeError('test synchronization failed')
            else:
                later_done.set()
            return ToolOutput(str(number))
        batch = {'stop_reason': 'tool_use', 'content': [
            {'type': 'tool_use', 'id': f'call{n}', 'name': 'probe', 'input': {'number': n}} for n in (1, 2)]}
        agent = self.agent_with_evidence([batch, done('done')])
        agent.tools.register_handler(ToolDefinition('probe', '', {}, effect='read', reentrant=True), read)
        agent.run('inspect')
        self.assertEqual([o['tool_use_id'] for o in self.ledger(agent)['observations']], ['call1', 'call2'])
        validate_tool_history(agent.messages)

    def test_cancelled_partial_result_is_recorded(self):
        output = ToolOutput('partial', status='error', exit_code=-1, outcome='cancelled', process_id=42)
        def cancel():
            error = CancelledError('stop')
            error.tool_output = output
            raise error
        agent = self.agent_with_evidence([tool('probe')])
        agent.tools.register_handler(ToolDefinition('probe', '', {}), cancel)
        try:
            agent.run('inspect')
        except CancelledError:
            pass
        observation = self.ledger(agent)['observations'][0]
        self.assertEqual(observation['execution_id'], output.execution_id)
        self.assertEqual(observation['status'], 'cancelled')
        validate_tool_history(agent.messages)

    def test_lossy_zero_search_cannot_prove_absence(self):
        output = ToolOutput('0 results')
        output.scan_complete = False
        output.source_complete = False
        agent = self.agent_with_evidence([tool('search'), done('absent')])
        agent.tools.register_handler(ToolDefinition('search', '', {}), lambda: output)
        agent.run('search')
        observation = self.ledger(agent)['observations'][0]
        self.assertFalse(observation['absence_proven'])
        self.assertFalse(observation['scan_complete'])

    def test_real_bash_unittest_and_end_boundary_external_edit(self):
        tests = self.root / 'checks'
        tests.mkdir()
        source = tests / 'test_sample.py'
        source.write_text('import unittest\nclass Sample(unittest.TestCase):\n def test_ok(self): self.assertEqual(1, 1)\n')
        command = 'python -m unittest discover -s checks'
        prompt = '检查临时项目\n验证配置：' + json.dumps({'command': command, 'inputs': ['checks']})
        agent = self.agent_with_evidence([tool('bash', command=command), done('all passed')])
        agent.tools.register(BashTool(workspace_guard=WorkspaceGuard(self.root), output_root=self.root / 'logs'))
        result = agent.run(prompt)
        self.assertEqual(result.stop_reason, 'end_turn')
        record = self.ledger(agent)['verifications'][0]
        self.assertEqual((record['result'], record['validity']), ('passed', 'fresh'), record)
        self.assertIsNotNone(record['output_id'])
        self.assertEqual(len(agent.client.requests), 2)
        # Simulate an external editor during the next model response.
        create = agent.client.create_message
        def external_edit(**kwargs):
            source.write_text(source.read_text() + '# external edit\n')
            return create(**kwargs)
        agent.client.responses.append(done('done'))
        with patch.object(agent.client, 'create_message', side_effect=external_edit):
            agent.run('继续')
        self.assertEqual(self.ledger(agent)['verifications'][0]['validity'], 'stale')

    def test_new_task_and_subagent_text_do_not_reuse_definitions(self):
        agent = self.agent_with_evidence()
        declaration = '验证配置：' + json.dumps({'command': 'python -m unittest', 'inputs': ['.']})
        agent.run('检查\n' + declaration)
        old = agent._cvm.current.task_id
        agent.add_user_message(declaration, source='runtime')
        agent.run('新任务：调查其他问题')
        definitions = self.ledger(agent)['definitions']
        self.assertEqual(len(definitions), 1)
        self.assertEqual(next(iter(definitions.values()))['task_id'], old)
        self.assertNotEqual(old, agent._cvm.current.task_id)

    def test_restore_legacy_unknown_and_off_rollback(self):
        agent = self.agent_with_evidence([tool('probe'), done('done')])
        agent.tools.register_handler(ToolDefinition('probe', '', {}), lambda: ToolOutput('ok'))
        agent.run('inspect')
        payload = agent.export_cvm_state()
        self.assertEqual(payload['schema_version'], 2)
        restored = CvmRuntime(CvmConfig(contract='shadow', evidence='shadow'), self.root)
        restored.restore(json.loads(json.dumps(payload)))
        self.assertEqual(restored.export(), payload)
        legacy = deepcopy(payload)
        legacy['schema_version'] = 1
        del legacy['evidence']
        restored.restore(legacy)
        self.assertEqual(restored.export()['evidence']['observations'], [])
        disabled = CvmRuntime(CvmConfig(), self.root)
        disabled.restore(payload)
        self.assertEqual(disabled.export(), payload)
        corrupt = deepcopy(payload)
        corrupt['evidence']['schema_version'] = 99
        restored.restore(corrupt)
        self.assertFalse(restored.recording)
        self.assertEqual(restored.export(), corrupt)

    def test_capture_failure_degrades_without_changing_execution(self):
        agent = self.agent_with_evidence([tool('probe'), done('done')])
        agent.tools.register_handler(ToolDefinition('probe', '', {}), lambda: ToolOutput('ok'))
        with patch.object(agent._cvm.evidence, 'packet', side_effect=OSError('disk full')):
            result = agent.run('inspect')
        self.assertEqual(result.stop_reason, 'end_turn')
        self.assertTrue(self.ledger(agent)['degraded'])
        self.assertEqual(len(agent.client.requests), 2)

    def test_configuration_requires_task_identity_and_stays_passive(self):
        self.assertEqual(CvmConfig.from_env({'CODEAGENT_CVM_CONTRACT': 'shadow', 'CODEAGENT_CVM_EVIDENCE': ' SHADOW '}).evidence, 'shadow')
        with self.assertRaises(ValueError):
            CvmConfig(evidence='shadow')
        with self.assertRaises(ValueError):
            CvmConfig(contract='active', evidence='active')

    def test_parallel_cancellation_captures_partial_output_and_unstarted_calls(self):
        output = ToolOutput('partial', status='error', outcome='cancelled')
        def cancel():
            exc = CancelledError('test stop')
            exc.tool_output = output
            raise exc
        agent = self.agent_with_evidence([tool('reader')])
        agent.tools.register_handler(ToolDefinition('reader', '', {}, effect='read', reentrant=True), cancel)
        try:
            agent.run('inspect')
        except CancelledError:
            pass
        self.assertEqual(self.ledger(agent)['observations'][0]['execution_id'], output.execution_id)
        self.assertEqual(self.ledger(agent)['observations'][0]['status'], 'cancelled')

    def test_compaction_preserves_ledger_and_restore_does_not_infer_from_summary(self):
        from codeagent.context import ContextConfig, ContextManager
        from cvm_fixtures import ScriptedClient
        context = ContextManager(config=ContextConfig(mode='model', summarization_model='summary',
            recency_messages=2, recency_rounds=1, transcript_dir=self.root / 'transcripts',
            tool_output_dir=self.root / 'outputs'))
        agent = self.agent_with_evidence([tool('probe'), done('done')], context=context)
        agent.tools.register_handler(ToolDefinition('probe', '', {}), lambda: ToolOutput('failed', status='error', exit_code=1))
        agent.run('inspect')
        for index in range(8):
            agent.messages.extend([
                {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': f'old{index}', 'name': 'read_file', 'input': {'file_path': 'x'}}]},
                {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': f'old{index}', 'content': 'evidence ' * 2000}]},
            ])
        saved = agent.export_cvm_state()
        context.force_compact(agent.messages, client=ScriptedClient([done('All tests passed')] * 5))
        self.assertGreater(context.state.summary_revision, 0)
        restored = self.agent_with_evidence(messages=deepcopy(agent.messages))
        restored._cvm.config = restored.config.cvm
        restored.restore_cvm_state(json.loads(json.dumps(saved)))
        restored.run('继续')
        self.assertEqual(self.ledger(restored), saved['evidence'])
        self.assertEqual(self.ledger(restored)['observations'][0]['status'], 'error')
        validate_tool_history(restored.messages)

    def test_web_factory_scheduler_persist_and_restore_evidence(self):
        import time
        from codeagent import EnvironmentConfig
        from codeagent.context import ContextConfig
        from codeagent.memory import MemoryConfig
        from codeagent.lsp.registry import LspConfig
        from codeagent.web.factory import WebAgentFactory
        from codeagent.web.scheduler import RunScheduler
        from codeagent.web.storage import SQLiteRepository
        from cvm_fixtures import ScriptedClient
        (self.root / 'readme.txt').write_text('hello')
        repository = SQLiteRepository(self.root / 'state.db', recover_incomplete=False)
        self.addCleanup(repository.close)
        conversation = repository.create_conversation(title='evidence', workspace=str(self.root))
        env = EnvironmentConfig(model_id='offline', data_dir=self.root / 'data', enable_skills=False,
            memory_config=MemoryConfig(enabled=False), lsp_config=LspConfig(enabled=False),
            context_config=ContextConfig(mode='off'), cvm_config=CvmConfig(contract='shadow', evidence='shadow'))
        factory = WebAgentFactory(env, self.root, repository)
        self.addCleanup(factory.close)
        scheduler = RunScheduler(repository, factory)
        self.addCleanup(scheduler.stop)
        scripts = iter([[tool('read_file', file_path='readme.txt'), done('done')], [done('done')]])
        with patch.object(EnvironmentConfig, 'create_model_client', side_effect=lambda **kw: ScriptedClient(next(scripts))):
            saved = []
            for prompt in ('调查文件', '继续'):
                run = scheduler.submit(conversation.id, prompt)
                deadline = time.monotonic() + 10
                while scheduler.is_run_pending(run.id) or repository.get_run(run.id).status in {'queued', 'running'}:
                    if time.monotonic() > deadline:
                        self.fail('Web run did not finish')
                    time.sleep(.01)
                finished = repository.get_run(run.id)
                self.assertEqual(finished.status, 'completed', finished.error)
                self.assertEqual(finished.metadata['cvm_config']['evidence'], 'shadow')
                saved.append(repository.get_checkpoint_for_run(run.id).metadata['cvm'])
        self.assertEqual(len(saved[0]['evidence']['observations']), 1)
        self.assertEqual(saved[0]['evidence'], saved[1]['evidence'])
        self.assertEqual(saved[0]['current']['task_id'], saved[1]['current']['task_id'])

    def test_only_approved_plan_can_supply_plan_definitions(self):
        from codeagent.cvm import ApprovedPlan
        from codeagent.web.plan_storage import plan_hash
        agent = self.agent_with_evidence()
        agent.run('inspect')
        markdown = '验证配置：' + json.dumps({'command': 'python -m unittest', 'inputs': ['.']})
        agent.bind_approved_plan(ApprovedPlan('plan', 1, plan_hash(markdown, {}), markdown))
        definition = next(iter(self.ledger(agent)['definitions'].values()))
        self.assertEqual(definition['source_kind'], 'approved_plan')
        self.assertEqual(definition['source_ref'], 'plan:plan:1')
