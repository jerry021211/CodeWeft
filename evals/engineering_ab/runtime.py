"""Experiment-only adapters; no new judgment rules are added to production."""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys

from codeagent.cvm import CvmConfig
from codeagent.cvm.verification import TrustedVerificationAdapter, VerificationDefinition
from codeagent.tools.base import ToolDefinition, ToolOutput
from evals.agent_adapter import append_record
from evals.context_journey.runtime import JourneyObserver
from evals.cvm_verification.runtime import structured_result
from evals.evidence import hashes, write_json


def run_check(workspace, case, *, hidden=False, engine=None):
    engine = Path(engine) if engine is not None else Path(__file__).resolve().parents[2]
    env = {k: v for k, v in os.environ.items() if k.upper() in {'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP'}}
    env.update(PYTHONPATH=str(engine), PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
    try:
        proc = subprocess.run([sys.executable, '-P', '-B', '-m', 'evals.engineering_ab.checker', str(workspace), case,
                               *(['--hidden'] if hidden else [])], cwd=engine, env=env, capture_output=True, text=True, encoding='utf-8', timeout=8)
        return json.loads(proc.stdout) if proc.returncode == 0 else {'passed': False, 'error_type': 'checker_process_error'}
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        return {'passed': False, 'error_type': 'checker_timeout' if isinstance(error, subprocess.TimeoutExpired) else 'checker_process_error'}


class Observer(JourneyObserver):
    def tests(self, suite):
        if suite != 'engineering':
            return ToolOutput('Unknown suite', status='error', exit_code=1)
        result = run_check(self.workspace, self.seed['case_id'])
        append_record(self.trial / 'agent-tests.jsonl', {'phase': self.phase, 'suite': suite, **result,
                      'source_hashes': hashes(self.workspace)})
        output = ToolOutput(json.dumps(result), status='success' if result['passed'] else 'error', exit_code=0 if result['passed'] else 1)
        output.verified_check_result = result
        return output


def register(agent, observer):
    inputs = tuple(p for p in observer.seed['workspace_files'] if p.startswith('src/') or p == 'docs/contract.md')
    adapter = TrustedVerificationAdapter((VerificationDefinition(command='host:run_project_tests:engineering', inputs=inputs,
        source_kind='host', source_ref='engineering-ab-v1'),), lambda args: 'host:run_project_tests:engineering' if args.get('suite') == 'engineering' else '', structured_result)
    agent.tools.register_handler(ToolDefinition('run_project_tests', '运行此项目固定功能检查。suite=engineering；CVM 的 host:run_project_tests:engineering 对应这个调用。',
        {'type': 'object', 'properties': {'suite': {'type': 'string', 'enum': ['engineering']}}, 'required': ['suite']}), observer.tests, verification=adapter)
    agent.hooks.observe('PreToolUse', observer.before)
    agent.hooks.observe('PostToolUse', observer.after)


def configure(agent, manifest, trial):
    arm = manifest['variant']
    if arm not in {'ablated', 'current', 'cvm'}:
        raise ValueError('Unknown experiment arm')
    # Record facts in every arm. A/B keep CVM prompt projection inactive.
    agent.config.cvm = CvmConfig(contract='active' if arm == 'cvm' else 'shadow', evidence='shadow',
                                 verification='active' if arm == 'cvm' else 'shadow')
    original = agent.prompt_runtime._load_template('execution')
    override = trial / 'prompt-overrides'
    override.mkdir()
    selected = '根据用户当前请求使用可用工具处理任务。' if arm == 'ablated' else original
    (override / 'execution.md').write_text(selected, encoding='utf-8')
    agent.prompt_runtime.config = replace(agent.prompt_runtime.config, template_dir=override)
    write_json(trial / 'policy-diff.json', {'arm': arm, 'removed_execution': original if arm == 'ablated' else '',
               'effective_execution': selected, 'core': agent.prompt_runtime._load_template('core'),
               'identity': agent.prompt_runtime._load_template('identity'), 'cvm': agent.config.cvm.snapshot()})
