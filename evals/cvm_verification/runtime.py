"""Explicit host attestation for the existing narrow journey checking tool."""
import json
from pathlib import Path
from codeagent.cvm.verification import TrustedVerificationAdapter, VerificationDefinition
from codeagent.tools import ToolDefinition
from codeagent.tools.base import ToolOutput
from evals.agent_adapter import append_record
from evals.context_journey.runtime import JourneyObserver, run_checks
from evals.evidence import hashes

BLOCKED_ERRORS = {'fixture_environment_unavailable', 'checker_process_error', 'unknown_suite'}


def structured_result(output):
    if output.process_running:
        return 'running', {}
    if not output.source_complete or output.storage_error:
        return 'unknown', {}
    value = getattr(output, 'verified_check_result', None)
    if not isinstance(value, dict):
        return 'unknown', {}
    if value.get('error_type') == 'checker_timeout':
        return 'timeout', {}
    if value.get('error_type') in BLOCKED_ERRORS:
        return 'blocked', {}
    if value.get('error_type'):
        # The checker ran, but loading/calling the submitted function raised.
        # This is a failed check, not an unavailable testing environment.
        return 'failed', {}
    total, passed = value.get('checks_total'), value.get('checks_passed')
    if type(total) is not int or type(passed) is not int or not 0 <= passed <= total or total == 0:
        return 'unknown', {}
    result = 'passed' if passed == total and value.get('passed') is True and output.status == 'success' else 'failed'
    return result, {'collected': total, 'passed': passed}


class VerificationObserver(JourneyObserver):
    def tests(self, suite):
        result = ({'suite': suite, 'passed': False, 'error_type': 'fixture_environment_unavailable'}
                  if self.seed['case_id'] == 'V08' else run_checks(self.workspace, suite))
        append_record(self.trial / 'agent-tests.jsonl', {'phase': self.phase, **result,
                      'source_hashes': {k: v for k, v in hashes(self.workspace).items() if k.startswith('src/')}})
        blocked = result.get('error_type') in BLOCKED_ERRORS
        output = ToolOutput(json.dumps(result, ensure_ascii=False),
                            status='blocked' if blocked else 'success' if result['passed'] else 'error',
                            exit_code=None if blocked or result.get('error_type') == 'checker_timeout' else 0 if result['passed'] else 1)
        # Trusted structured data, distinct from model-visible prose and JSON text.
        output.verified_check_result = result
        return output


def register_tools(agent, observer):
    adapter = TrustedVerificationAdapter(tuple(
        VerificationDefinition(command=f'host:run_project_tests:{suite}', inputs=(f'src/{suite}.py',),
                               source_kind='host', source_ref='cvm-evaluation-v1')
        for suite in ('parser', 'exporter', 'totals')),
        select=lambda args: f"host:run_project_tests:{args.get('suite', '')}", read_result=structured_result)
    agent.tools.register_handler(ToolDefinition('run_project_tests',
        '运行本练习固定检查；suite 为 parser/exporter/totals。CVM 中 host:run_project_tests:parser 表示调用本工具 suite=parser，以此类推。不能运行任意命令。',
        {'type': 'object', 'properties': {'suite': {'type': 'string', 'enum': ['parser', 'exporter', 'totals']}},
         'required': ['suite']}), observer.tests, verification=adapter)
    agent.hooks.observe('PreToolUse', observer.before)
    agent.hooks.observe('PostToolUse', observer.after)
