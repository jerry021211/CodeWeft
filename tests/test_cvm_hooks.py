"""Exercise CVM observer wiring through real Agent boundaries, without a model API."""
from copy import deepcopy
import threading
from unittest.mock import patch

from codeagent.cvm import CvmConfig
from codeagent.hooks import HookDecision, HookManager
from codeagent.messages import validate_tool_history
from codeagent.runtime import CancelledError
from codeagent.tools import ToolDefinition
from codeagent.tools.base import ToolOutput
from cvm_fixtures import done, tool
from test_cvm_contract import ContractFixture


class CvmHookIntegrationTests(ContractFixture):
    def test_observer_return_values_cannot_skip_contract_or_evidence(self):
        hooks = HookManager()
        for event in ('UserInputAccepted', 'RunInputReady', 'CollectRuntimeReminders',
                      'ToolBatchFinished', 'RunFinished'):
            hooks.observe(event, lambda *args, **kwargs: HookDecision('finalize'))
        hooks.register('UserPromptSubmit', lambda prompt: 'legacy short circuit')
        agent = self.agent(responses=[tool('probe'), done('done')], hooks=hooks)
        agent.config.cvm = CvmConfig(contract='active', evidence='shadow')
        agent.tools.register_handler(ToolDefinition('probe', '', {}), lambda: ToolOutput('failed', exit_code=1, status='error'))
        agent.run('调查登录失败')
        saved = agent.export_cvm_state()
        self.assertEqual(saved['current']['objective'], '调查登录失败')
        self.assertIn('调查登录失败', agent.context.state.user_goal)
        self.assertTrue(agent.context.state.runtime_reminders['cvm_contract']['active'])
        self.assertIn('运行时提醒：任务契约', str(agent.client.requests))
        self.assertEqual(saved['evidence']['observations'][0]['exit_code'], 1)
        self.assertEqual(len(agent.client.requests), 2)

    def test_existing_tool_events_notify_all_observers_before_control(self):
        hooks, seen = HookManager(), []
        hooks.observe('PreToolUse', lambda call: seen.append('before') or 'ignored')
        hooks.observe('PostToolUse', lambda call, output: seen.append('first') or 'ignored')
        hooks.observe('PostToolUse', lambda call, output: seen.append('second'))
        hooks.register('PostToolUse', lambda call, output: seen.append('control') or 'short circuit')
        agent = self.agent(responses=[tool('probe'), done('done')], hooks=hooks)
        agent.tools.register_handler(ToolDefinition('probe', '', {}), lambda: 'ok')
        agent.run('inspect')
        self.assertEqual(seen, ['before', 'first', 'second', 'control'])

    def test_batch_observer_failure_does_not_skip_cvm_or_result_cleanup(self):
        hooks = HookManager()
        def fail(*args):
            raise OSError('observer failed')
        hooks.observe('ToolBatchFinished', fail)
        agent = self.agent(responses=[tool('probe')], hooks=hooks)
        agent.config.cvm = CvmConfig(contract='active', evidence='shadow')
        agent.tools.register_handler(ToolDefinition('probe', '', {}), lambda: ToolOutput('failed', exit_code=1, status='error'))
        with self.assertRaisesRegex(OSError, 'observer failed'):
            agent.run('inspect')
        self.assertEqual(agent.export_cvm_state()['evidence']['observations'][0]['exit_code'], 1)
        validate_tool_history(agent.messages)

    def test_run_observer_failure_still_refreshes_evidence_and_closes_tools(self):
        hooks = HookManager()
        def fail():
            raise OSError('finish failed')
        hooks.observe('RunFinished', fail)
        agent = self.agent(hooks=hooks)
        agent.config.cvm = CvmConfig(contract='active', evidence='shadow')
        with patch.object(agent._cvm.evidence, 'refresh') as refresh, \
                patch.object(agent.tools, 'close_workspace_tools') as close:
            with self.assertRaisesRegex(OSError, 'finish failed'):
                agent.run('inspect')
        refresh.assert_called_once()
        close.assert_called_once()

    def test_child_hooks_update_only_child_contract_and_ledger(self):
        parent = self.agent()
        parent.config.cvm = CvmConfig(contract='active', evidence='shadow')
        parent.run('父任务')
        before = deepcopy(parent.export_cvm_state())
        child = self.agent(hooks=parent.hooks, responses=[tool('probe'), done('done')], cvm_input_source='delegation')
        child.config.cvm = CvmConfig(contract='active', evidence='shadow')
        child.tools.register_handler(ToolDefinition('probe', '', {}), lambda: 'ok')
        child.run('子任务')
        self.assertEqual(parent.export_cvm_state(), before)
        self.assertEqual(child.export_cvm_state()['current']['objective'], '子任务')
        self.assertEqual(len(child.export_cvm_state()['evidence']['observations']), 1)

    def test_finish_failure_does_not_replace_original_cancellation(self):
        hooks = HookManager()
        def fail():
            raise OSError('finish failed')
        hooks.observe('RunFinished', fail)
        agent = self.agent(hooks=hooks)
        original = CancelledError('user cancelled')
        agent.cancellation.cancel('user cancelled')
        with patch.object(agent.cancellation, 'raise_if_cancelled', side_effect=original), \
                patch.object(agent.tools, 'close_workspace_tools') as close:
            with self.assertRaises(CancelledError) as raised:
                agent.run('inspect')
        self.assertIs(raised.exception, original)
        self.assertIn('finish failed', original.__notes__[0])
        close.assert_called_once()

    def test_post_tool_observer_mutation_cannot_rewrite_captured_evidence(self):
        hooks = HookManager()
        def mutate(call, output):
            output.exit_code = 0
            return 'ignored'
        hooks.observe('PostToolUse', mutate)
        agent = self.agent(hooks=hooks, responses=[tool('probe'), done('done')])
        agent.config.cvm = CvmConfig(contract='active', evidence='shadow')
        agent.tools.register_handler(ToolDefinition('probe', '', {}), lambda: ToolOutput('failed', exit_code=1, status='error'))
        agent.run('inspect')
        self.assertEqual(agent.export_cvm_state()['evidence']['observations'][0]['exit_code'], 1)

    def test_parallel_observers_and_evidence_commits_run_on_parent_thread(self):
        hooks, threads = HookManager(), []
        hooks.observe('PostToolUse', lambda call, output: threads.append(threading.get_ident()))
        hooks.observe('ToolBatchFinished', lambda *args: threads.append(threading.get_ident()))
        calls = {'stop_reason': 'tool_use', 'content': [
            {'type': 'tool_use', 'id': str(i), 'name': 'reader', 'input': {}} for i in range(2)]}
        agent = self.agent(hooks=hooks, responses=[calls, done('done')])
        agent.config.cvm = CvmConfig(contract='active', evidence='shadow')
        agent.tools.register_handler(ToolDefinition('reader', '', {}, effect='read', reentrant=True), lambda: 'ok')
        commit = agent._cvm.evidence.commit
        def record_commit(packet):
            threads.append(threading.get_ident())
            return commit(packet)
        with patch.object(agent._cvm.evidence, 'commit', side_effect=record_commit):
            agent.run('inspect')
        self.assertEqual(threads, [threading.get_ident()] * 5)
        self.assertEqual([o['tool_use_id'] for o in agent.export_cvm_state()['evidence']['observations']], ['0', '1'])
