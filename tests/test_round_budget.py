"""Fixed 200-round execution budget with a model-visible wrap-up reminder."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codeagent import Agent, AgentConfig, EnvironmentConfig, EventEmitter, ModelResponse, ToolDefinition, ToolRegistry
from codeagent.context import ContextConfig, ContextManager
from codeagent.hooks.loop_guard import LoopGuardConfig
from codeagent.messages import validate_tool_history


class FiniteClient:
    def __init__(self, rounds):
        self.rounds, self.calls = rounds, 0
        self.reminders = []

    def fork(self, **kwargs):
        return self

    def create_message(self, **kwargs):
        self.calls += 1
        self.reminders.append(any(isinstance(message['content'], str) and '运行时提醒：收尾提醒' in message['content']
                                  for message in kwargs['messages']))
        if self.calls <= self.rounds:
            return ModelResponse('tool_use', [{'type': 'tool_use', 'id': f'call-{self.calls}',
                                              'name': 'echo', 'input': {'value': str(self.calls)}}])
        return ModelResponse('end_turn', [{'type': 'text', 'text': 'finished'}])


class RoundBudgetTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def agent(self, rounds, *, guard=True, children=False):
        registry = ToolRegistry()
        registry.register_handler(ToolDefinition('echo', 'Return evidence', {'type': 'object'}), lambda value: 'evidence ' + value)
        return Agent(client=FiniteClient(rounds), tools=registry, allow_subagents=children,
                     subagent_max_iterations=1,
                     config=AgentConfig(model='test', max_iterations=1,
                                        loop_guard=LoopGuardConfig() if guard else None),
                     context=ContextManager(config=ContextConfig(mode='off', tool_output_dir=self.root / 'outputs',
                                                                  transcript_dir=self.root / 'history')))

    def test_more_than_fifty_rounds_with_and_without_guard(self):
        for guard in (True, False):
            with self.subTest(guard=guard):
                agent = self.agent(60, guard=guard)
                # An old SDK override cannot replace the fixed 200-round cap.
                agent.config.max_iterations = 1
                result = agent.run('continue to completion')
                self.assertEqual(result.stop_reason, 'end_turn')
                self.assertEqual(result.iterations, 61)
                self.assertEqual(agent.client.calls, 61)

    def test_child_exceeds_old_thirty_round_limit(self):
        parent = self.agent(35, children=True)
        child = parent._create_subagent(EventEmitter())
        self.assertEqual(child.config.max_iterations, 200)
        result = child.run('continue child task')
        self.assertEqual(result.stop_reason, 'end_turn')
        self.assertEqual(result.iterations, 36)

    def test_legacy_round_stop_checkpoint_resumes_without_resetting_counters(self):
        for reason in ('max_iterations:tool_use', 'budget_exceeded:iterations'):
            with self.subTest(reason=reason):
                agent = self.agent(0)
                state = agent.export_execution_state()
                state.pop('round_budget')  # Simulate a checkpoint predating the new counter.
                state['state'].update(rounds=50, stop_reason=reason)
                state['budget']['model_calls'] = 4
                agent.restore_execution_state(state)
                result = agent.run()
                self.assertEqual(result.stop_reason, 'end_turn')
                restored = agent.export_execution_state()
                self.assertEqual(restored['state']['rounds'], 51)
                self.assertEqual(restored['budget']['model_calls'], 5)

    def test_old_environment_limit_is_ignored_including_non_numeric_value(self):
        for value in ('1', '50', 'invalid-retired-setting'):
            with patch.dict(os.environ, {'MODEL_ID': 'test', 'MAX_ITERATIONS': value}, clear=True), patch('codeagent.config._load_dotenv'):
                config = EnvironmentConfig.from_env()
                self.assertEqual(config.max_iterations, 200)
                self.assertEqual(config.to_agent_config().max_iterations, 200)

    def test_retired_call_stop_checkpoint_resumes_without_resetting_rounds(self):
        for reason in ('budget_exceeded:model_calls', 'budget_exceeded:tool_calls'):
            with self.subTest(reason=reason):
                agent = self.agent(0)
                saved = agent.export_execution_state()
                saved['round_budget'] = {'scope_id': 'legacy-call-stop', 'rounds': 184}
                saved['state'].update(scope_id='legacy-call-stop', rounds=184, stop_reason=reason)
                saved['budget'].update(model_calls=80, tool_calls=200, stop_reason=reason)
                agent.restore_execution_state(saved)
                self.assertEqual(agent.run().stop_reason, 'end_turn')
                state = agent.export_execution_state()
                self.assertEqual(state['round_budget']['rounds'], 185)
                self.assertEqual(state['budget']['model_calls'], 81)
                self.assertEqual(state['budget']['tool_calls'], 200)
                self.assertTrue(agent.client.reminders[0])

    def test_185_reminder_and_200_round_stop_with_or_without_guard(self):
        for guard in (True, False):
            with self.subTest(guard=guard):
                agent = self.agent(201, guard=guard)
                result = agent.run('finish the task')
                self.assertEqual(result.stop_reason, 'max_iterations:200')
                self.assertEqual(result.iterations, 200)
                self.assertEqual(agent.client.calls, 200)
                self.assertFalse(any(agent.client.reminders[:184]))
                self.assertTrue(all(agent.client.reminders[184:]))
                self.assertEqual(sum('运行时提醒：收尾提醒' in message['content']
                                     for message in agent.messages if isinstance(message['content'], str)), 1)
                validate_tool_history(agent.messages)
                results = [block for message in agent.messages if isinstance(message['content'], list)
                           for block in message['content'] if block.get('type') == 'tool_result']
                self.assertEqual(len(results), 200)
                self.assertEqual(results[-1]['tool_use_id'], 'call-200')
                # The same execution cannot gain another 200 rounds via run(None).
                self.assertEqual(agent.run().stop_reason, 'max_iterations:200')
                self.assertEqual(agent.client.calls, 200)

    def test_resume_at_184_warns_then_stops_at_200(self):
        for guard in (True, False):
            with self.subTest(guard=guard):
                agent = self.agent(100, guard=guard)
                saved = agent.export_execution_state()
                saved['round_budget'] = {'scope_id': 'resumed-execution', 'rounds': 184}
                if guard:
                    saved['state'].update(scope_id='resumed-execution', rounds=184)
                agent.restore_execution_state(saved)
                result = agent.run()
                self.assertEqual(result.iterations, 16)
                self.assertEqual(agent.client.calls, 16)
                self.assertTrue(all(agent.client.reminders))
                self.assertEqual(agent.export_execution_state()['round_budget']['rounds'], 200)

    def test_final_answer_on_round_200_is_success(self):
        agent = self.agent(0)
        saved = agent.export_execution_state()
        saved['round_budget'] = {'scope_id': 'almost-finished', 'rounds': 199}
        saved['state'].update(scope_id='almost-finished', rounds=199)
        agent.restore_execution_state(saved)
        result = agent.run()
        self.assertEqual(result.stop_reason, 'end_turn')
        self.assertEqual(result.final_text, 'finished')
        self.assertEqual(result.iterations, 1)

    def test_compaction_cannot_hide_the_wrap_up_reminder(self):
        agent = self.agent(0, guard=False)
        saved = {'round_budget': {'scope_id': 'near-end', 'rounds': 184}}
        agent.restore_execution_state(saved)
        calls = []
        prepare = agent.context.prepare_before_model_call
        def compact_once(messages, **kwargs):
            result = prepare(messages, **kwargs)
            calls.append(True)
            if len(calls) == 1:
                return [message for message in result
                        if not (isinstance(message['content'], str) and '运行时提醒：收尾提醒' in message['content'])]
            return result
        with patch.object(agent.context, 'prepare_before_model_call', side_effect=compact_once):
            result = agent.run()
        self.assertEqual(result.stop_reason, 'end_turn')
        self.assertEqual(len(calls), 2)  # Reinjection rechecks the complete request budget.
        self.assertEqual(agent.client.reminders, [True])


if __name__ == '__main__':
    unittest.main()
