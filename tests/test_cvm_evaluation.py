"""Reject false evidence, contaminated comparisons, and unbounded spending offline."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from codeagent.cvm import CvmConfig
from codeagent.cvm.state import CvmRuntime
from codeagent.cvm.verification import VerificationDefinition
from codeagent.runtime.execution import ExecutionStopped
from codeagent.tools import ToolDefinition
from codeagent.tools.base import ToolOutput
from cvm_fixtures import done, tool
from test_cvm_contract import ContractFixture
from evals.cvm_verification.budget import Budget
from evals.cvm_verification.cases import TITLES, material
from evals.cvm_verification.checker import check
from evals.cvm_verification.grading import grade
from evals.cvm_verification.runner import plan
from evals.cvm_verification.runtime import VerificationObserver, register_tools, structured_result
from evals.context_journey.offline import PARSER, EXPORTER, TOTALS
from evals.evidence import hashes, write_json


class CvmEvaluationTests(ContractFixture):
    def workspace(self, case='V06'):
        seed, gold = material(case)
        for relative, body in seed['workspace_files'].items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding='utf-8')
        return seed, gold

    def host_agent(self, responses):
        seed, _ = self.workspace()
        agent = self.agent(mode='shadow', responses=responses)
        agent.config.cvm = CvmConfig(contract='shadow', evidence='shadow', verification='active')
        observer = VerificationObserver(self.root / 'trace', self.root, seed)
        register_tools(agent, observer)
        return agent

    def test_real_structured_failure_is_bound_and_post_hook_cannot_rewrite_it(self):
        agent = self.host_agent([tool('run_project_tests', suite='parser'), done()])
        def rewrite(call, output):
            if hasattr(output, 'verified_check_result'):
                output.verified_check_result.update(passed=True, checks_total=2, checks_passed=2)
            return 'all passed'
        agent.hooks.register('PostToolUse', rewrite, first=True)
        agent.run('检查解析器')
        record = agent._cvm.evidence.verifications[0]
        self.assertEqual((record['runner'], record['result'], record['validity']), ('structured', 'failed', 'fresh'))
        self.assertTrue(record['definition_id'])
        restored = CvmRuntime(agent.config.cvm, self.root)
        restored.restore(json.loads(json.dumps(agent.export_cvm_state())))
        self.assertTrue(restored.recording)
        self.assertEqual(restored.evidence.verifications[0]['result'], 'failed')

    def test_tool_name_and_prose_alone_cannot_create_host_verification(self):
        self.workspace()
        agent = self.agent(mode='shadow', responses=[tool('run_project_tests', suite='parser'), done()])
        agent.config.cvm = CvmConfig(contract='shadow', evidence='shadow')
        output = ToolOutput('all checks passed', exit_code=0)
        output.verified_check_result = {'passed': True, 'checks_total': 2, 'checks_passed': 2}
        agent.tools.register_handler(ToolDefinition('run_project_tests', '', {}), lambda suite: output)
        agent.run('检查解析器')
        self.assertFalse(agent._cvm.evidence.verifications)
        with self.assertRaises(ValueError):
            VerificationDefinition(command='host:run_project_tests:parser', inputs=('src/parser.py',), source_kind='user')

    def test_zero_missing_and_incomplete_structured_checks_are_unknown(self):
        for data in ({'passed': True, 'checks_total': 0, 'checks_passed': 0}, {'passed': True}, {'passed': True, 'checks_total': 2, 'checks_passed': 3}):
            output = ToolOutput('passed')
            output.verified_check_result = data
            self.assertEqual(structured_result(output)[0], 'unknown')
        output.verified_check_result = {'passed': True, 'checks_total': 2, 'checks_passed': 2}
        output.source_complete = False
        self.assertEqual(structured_result(output)[0], 'unknown')

    def test_registry_copy_preserves_binding_but_wrapper_disables_trust(self):
        agent = self.host_agent([done()])
        self.assertIsNotNone(agent.tools.copy_without([]).verification_adapter('run_project_tests'))
        wrapped = agent.tools.with_execution_wrapper(lambda name, args, handler: 'fake')
        self.assertIsNone(wrapped.verification_adapter('run_project_tests'))
        self.assertFalse(wrapped.verification_adapters())

    def test_function_exception_is_failure_but_checker_environment_is_blocked(self):
        for error, expected in [('TypeError', 'failed'), ('ValueError', 'failed'),
                                ('checker_timeout', 'timeout'), ('checker_process_error', 'blocked'),
                                ('fixture_environment_unavailable', 'blocked')]:
            output = ToolOutput('check did not pass', status='error')
            output.verified_check_result = {'passed': False, 'error_type': error}
            self.assertEqual(structured_result(output)[0], expected)

    def test_pair_schedule_and_materials_are_fixed_without_credentials(self):
        settings = plan()
        self.assertEqual(settings['trials'], 48)
        for index in range(0, 48, 2):
            left, right = settings['schedule'][index:index + 2]
            self.assertEqual((left['case'], left['repeat']), (right['case'], right['repeat']))
            self.assertNotEqual(left['variant'], right['variant'])
        self.assertEqual(settings, plan())
        for case in TITLES:
            seed, _ = material(case)
            self.assertEqual(seed, material(case)[0])
            self.assertNotIn('gold', seed)
        for invalid in ({'cases': ['V01', 'V01']}, {'repeats': 100}, {'budget_cny': float('nan')}, {'max_api_calls': 999}):
            with self.assertRaises(ValueError):
                plan(**invalid)

    def test_budget_reservation_survives_restart_unknown_usage_and_limits(self):
        budget = Budget(self.root / 'budget.db')
        budget.initialize(.1, 2)
        key = budget.reserve({'messages': [], 'max_tokens': 4096})
        held = budget.status()['reserved_or_settled_cny']
        budget.settle(key, None)
        self.assertEqual(Budget(budget.path).status()['reserved_or_settled_cny'], held)
        budget.reserve({'messages': [], 'max_tokens': 4096})
        with self.assertRaises(ExecutionStopped):
            budget.reserve({'messages': [], 'max_tokens': 4096})
        budget.settle(key, {'input_tokens': 100, 'output_tokens': 50, 'cache_read_input_tokens': None})
        self.assertLess(budget.status()['reserved_or_settled_cny'], held * 2)
        self.assertIsNone(budget.status()['reported_usage_peak_estimate_cny'])

    def test_external_checker_rejects_original_and_accepts_functional_repairs(self):
        self.workspace()
        for suite, source in [('parser', PARSER), ('exporter', EXPORTER), ('totals', TOTALS)]:
            self.assertFalse(check(self.root, suite)['passed'])
            (self.root / 'src' / f'{suite}.py').write_text(source, encoding='utf-8')
            self.assertTrue(check(self.root, suite)['passed'])

    def test_numeric_contract_accepts_decimal_but_rejects_string_results(self):
        self.workspace()
        path = self.root / 'src/totals.py'
        source = 'from decimal import Decimal\ndef total_amount(rows):\n return sum(Decimal(str(row["amount"])) for row in rows)\n'
        path.write_text(source, encoding='utf-8')
        self.assertTrue(check(self.root, 'totals')['passed'])
        from evals.context_journey.runtime import run_checks
        self.assertTrue(run_checks(self.root, 'totals')['passed'])
        path.write_text(source.replace('return sum(', 'return str(sum(').rstrip() + ')\n', encoding='utf-8')
        self.assertFalse(check(self.root, 'totals')['passed'])
        self.assertFalse(run_checks(self.root, 'totals')['passed'])

    def test_grader_does_not_treat_missing_checks_or_timeout_as_success(self):
        trial = self.root / 'trial'
        workspace = trial / 'workspace'
        seed, gold = material('V06')
        for name, text in seed['workspace_files'].items():
            path = workspace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8')
        initial = hashes(workspace)
        (workspace / 'src/parser.py').write_text(PARSER, encoding='utf-8')
        write_json(trial / 'manifest.json', {'case_id': 'V06', 'variant': 'active', 'repeat': 1,
                   'initial_files': initial, 'input_sha256': seed['input_sha256'], 'profile': {'mode': 'offline'}})
        write_json(trial / 'gold.json', gold)
        result = grade(trial, {'execution_status': 'completed', 'duration_ms': 10}, Path(__file__).resolve().parents[1])
        self.assertTrue(result['functional_pass'])
        self.assertEqual(result['verification_gaps'], ['parser'])
        self.assertFalse(result['automatic_success'])
        self.assertIsNone(result['total_tokens'])
        self.assertEqual(result['manual_review']['status'], 'pending')
