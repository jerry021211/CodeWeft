"""Frozen-engine paired trials with durable API and cost guards."""
import os
from pathlib import Path
import random
import subprocess
import sys
import time
from urllib.parse import urlsplit

from evals.context_suite.runner import new_directory
from evals.evidence import hashes, read_json, seal, snapshot, write_json
from evals.runner import ROOT, source_metadata
from evals.cvm_verification.budget import Budget
from evals.cvm_verification.cases import TITLES, VERSION, material
from evals.cvm_verification.grading import grade, write_report


def plan(*, mode='offline', cases=None, repeats=3, max_api_calls=12, max_tokens=4096,
         timeout=180, budget_cny=5, order_seed=20261013, **ignored):
    selected = list(TITLES) if cases is None else list(cases)
    if not selected or len(selected) != len(set(selected)) or set(selected) - TITLES.keys():
        raise ValueError('Select unique V01..V08')
    if mode not in {'offline', 'live'} or not 1 <= repeats <= 3 or not 1 <= max_api_calls <= 24 or not 128 <= max_tokens <= 8192:
        raise ValueError('Invalid mode or bounded trial/call/token count')
    if not 10 <= timeout <= 600 or not 0 < budget_cny <= 20:
        raise ValueError('Invalid timeout or cost limit')
    random.Random(order_seed).shuffle(selected)
    schedule = []
    for repeat in range(1, repeats + 1):
        for index, case in enumerate(selected):
            variants = ('shadow', 'active') if (repeat + index) % 2 else ('active', 'shadow')
            schedule.extend(dict(case=case, variant=v, repeat=repeat) for v in variants)
    return dict(version=VERSION, mode=mode, schedule=schedule, trials=len(schedule), repeats=repeats,
                max_api_calls=max_api_calls, max_tokens=max_tokens, timeout=timeout, budget_cny=budget_cny,
                max_total_api_calls=len(schedule) * max_api_calls, order_seed=order_seed)


def run_suite(*, output=ROOT / 'eval-results', experiment=None, **options):
    settings = (experiment.plan if experiment else plan)(**options)
    make_material = experiment.material if experiment else material
    grader = experiment.grade if experiment else grade
    reporter = experiment.write_report if experiment else write_report
    mode = settings['mode']
    env = dict(os.environ)
    # This local evaluation does not send traces to an additional external service.
    env.update(LANGSMITH_TRACING='false', LANGCHAIN_TRACING_V2='false', SUMMARIZATION_API_KEY='')
    if mode == 'live':
        from codeagent.config import EnvironmentConfig
        configured = EnvironmentConfig.from_env()
        if (configured.model_protocol != 'anthropic' or urlsplit(configured.base_url or '').hostname != 'api.deepseek.com'
                or configured.model_id not in {'deepseek-v4-flash', 'deepseek-flash'} or not configured.api_key):
            raise ValueError('This verified price guard currently supports the configured official DeepSeek Flash Anthropic endpoint only')
        env.update(API_KEY=configured.api_key, BASE_URL=configured.base_url, MODEL_ID=configured.model_id)
        model = configured.model_id
    else:
        model = 'offline-cvm-script'
    root = new_directory(Path(output), f'{"engineering-ab" if experiment else "cvm"}-{mode}')
    budget = Budget(root / 'budget.db')
    budget.initialize(settings['budget_cny'], settings['max_total_api_calls'])
    engine = root / 'engine-snapshot'
    frozen = snapshot(ROOT, engine)
    write_json(root / 'source-manifest.json', {**source_metadata(), 'engine_snapshot': frozen})
    write_json(root / 'suite.json', {**settings, 'model': model, 'context_mode': 'off',
               'contract': 'per_arm' if experiment else 'shadow', 'evidence': 'shadow'})
    print(str(root), flush=True)
    results = []
    for index, item in enumerate(settings['schedule'], 1):
        trial = root / f"trial-{index:03d}-{item['case']}-{item['variant']}-r{item['repeat']}"
        trial.mkdir()
        seed, gold = make_material(item['case'])
        workspace = trial / 'workspace'
        for relative, body in seed['workspace_files'].items():
            path = workspace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding='utf-8')
        if experiment:
            experiment.initialize_workspace(workspace)
        write_json(trial / 'seed.json', seed)
        write_json(trial / 'gold.json', gold)
        profile = dict(suite='context-journey-v1', mode=mode, model=model, summary_model=model,
                       context={'mode': 'off'}, max_iterations=12, max_tokens=settings['max_tokens'],
                       max_api_calls=settings['max_api_calls'], max_total_tokens=200000,
                       allowed_writes=['src/parser.py', 'src/exporter.py', 'src/totals.py'])
        if experiment:
            profile.update(max_iterations=settings['max_api_calls'], allowed_writes=gold['targets'])
        write_json(trial / 'manifest.json', {'case_id': item['case'], 'variant': item['variant'], 'repeat': item['repeat'],
                   'profile': profile, 'initial_files': hashes(workspace), 'input_sha256': seed['input_sha256'], 'engine_sha256': frozen['sha256']})
        child_env = {**env, 'PYTHONPATH': str(engine), 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONIOENCODING': 'utf-8', 'CODEAGENT_DATA_DIR': str(trial / 'runtime')}
        started = time.monotonic()
        with (trial / 'stdout.txt').open('w', encoding='utf-8') as stdout, (trial / 'stderr.txt').open('w', encoding='utf-8') as stderr:
            proc = subprocess.Popen([sys.executable, '-P', '-B', '-m',
                                    'evals.engineering_ab.worker' if experiment else 'evals.cvm_verification.worker', '--trial', str(trial)],
                                    cwd=workspace, env=child_env, stdout=stdout, stderr=stderr)
            try:
                code = proc.wait(timeout=settings['timeout'])
                execution = read_json(trial / 'worker-result.json') if (trial / 'worker-result.json').exists() else {'execution_status': 'worker_crash'}
                if code:
                    execution.update(execution_status='worker_crash', returncode=code)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                execution = {'execution_status': 'timeout'}
        execution['duration_ms'] = round((time.monotonic() - started) * 1000, 3)
        write_json(trial / 'execution.json', execution)
        result = grader(trial, execution, engine)
        results.append(result)
        write_json(root / 'partial-results.json', results)
        reporter(root, results, settings, budget.status())
        print(f"{index}/{settings['trials']} {item['case']}/{item['variant']}/r{item['repeat']}: success={result['automatic_success']}, advice={result['advice_delivered']}, status={execution['execution_status']}", flush=True)
        if mode == 'live' and (budget.status()['reserved_or_settled_cny'] >= settings['budget_cny']
                              or 'cvm_evaluation_reservation' in execution.get('stop_reason', '')):
            break
    reporter(root, results, settings, budget.status())
    seal(root)
    return root
