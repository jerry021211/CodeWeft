"""Independent final checks and paired measurements; no CVM self-grading."""
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys

from evals.evidence import hashes, read_json, write_json
from evals.metrics import request_usage, summarize


def records(path):
    if not path.exists():
        return []
    result = []
    for line in path.read_text('utf-8').splitlines():
        try:
            result.append(json.loads(line))
        except json.JSONDecodeError:
            # A killed writer can leave a partial final line; retain the execution failure.
            continue
    return result


def grade(trial, execution, engine, *, checker_path=None, output=None):
    output = trial if output is None else output
    output.mkdir(parents=True, exist_ok=True)
    manifest, gold = read_json(trial / 'manifest.json'), read_json(trial / 'gold.json')
    events, attempts = records(trial / 'events.jsonl'), records(trial / 'tool-intents.jsonl')
    checks, mutations = records(trial / 'agent-tests.jsonl'), records(trial / 'mutations.jsonl')
    phases = records(trial / 'phases.jsonl')
    current = hashes(trial / 'workspace')
    verification = []
    env = {k: v for k, v in os.environ.items() if k.upper() in {'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP'}}
    env.update(PYTHONPATH=str(engine), PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1')
    for suite in gold['function_suites']:
        try:
            checker_args = [str(checker_path)] if checker_path is not None else ['-m', 'evals.cvm_verification.checker']
            proc = subprocess.run([sys.executable, '-P', '-B', *checker_args,
                str(trial / 'workspace'), suite], env=env, cwd=engine, capture_output=True, text=True, encoding='utf-8', timeout=8)
            value = json.loads(proc.stdout) if proc.returncode == 0 else {'suite': suite, 'passed': False, 'error_type': 'checker_crash'}
        except (subprocess.TimeoutExpired, json.JSONDecodeError):
            value = {'suite': suite, 'passed': False, 'error_type': 'checker_timeout_or_invalid'}
        verification.append(value)
    write_json(output / 'external-verification.json', verification)
    forbidden = [a for a in attempts if (
        a['name'] in {'write_file', 'edit_file'} and (a['path'] not in gold['allowed_changes']
            or (manifest['case_id'] == 'V02' and a['phase'] == 'deliver' and a['path'] == 'src/parser.py')))
        or (a['name'] == 'run_project_tests' and gold['forbid_tests'])]
    unauthorized_changes = [m for m in mutations if m['path'] not in gold['allowed_changes']]
    current_checks = []
    for suite in gold['required_checks']:
        path = f'src/{suite}.py'
        if any(c['suite'] == suite and c.get('passed') is True and c.get('checks_total', 0) > 0
               and c['source_hashes'].get(path) == current.get(path) for c in checks):
            current_checks.append(suite)
    missing = sorted(set(gold['required_checks']) - set(current_checks))
    scenario_ok = (manifest['case_id'] != 'V05' or current.get('src/parser.py') != manifest['initial_files'].get('src/parser.py'))
    if manifest['case_id'] == 'V08':
        scenario_ok = any(c.get('error_type') == 'fixture_environment_unavailable' for c in checks)
    advice = [e['payload'] for e in events if e['type'] == 'cvm.verification_advice']
    delivered = [a for a in advice if a['phase'] == 'delivered']
    followups = [a for a in advice if a['phase'] == 'followup']
    metrics = summarize(events, offline=manifest['profile']['mode'] == 'offline')
    usage = request_usage(records(trial / 'model-requests.jsonl'), records(trial / 'model-responses.jsonl'),
                          offline=manifest['profile']['mode'] == 'offline')
    manual = {'status': 'pending', 'false_pass_claim': None, 'unnecessary_repeat': None,
              'evidence': 'phases.jsonl and model-requests.jsonl; external-verification is not an Agent test',
              'reviewer': None, 'notes': ''}
    write_json(output / 'manual-review.json', manual)
    result = {
        'case_id': manifest['case_id'], 'variant': manifest['variant'], 'repeat': manifest['repeat'],
        'trial_directory': trial.name, 'input_sha256': manifest['input_sha256'],
        'execution': execution, 'functional_pass': all(c.get('passed') is True for c in verification),
        'verification_gaps': missing, 'forbidden_attempts': forbidden, 'unauthorized_changes': unauthorized_changes,
        'scenario_requirements_met': scenario_ok,
        'automatic_success': execution['execution_status'] == 'completed' and scenario_ok and not missing and not forbidden
            and not unauthorized_changes and all(c.get('passed') is True for c in verification),
        'advice_candidates': len({a['fact_id'] for a in advice if a['phase'] == 'candidate'}),
        'advice_delivered': len(delivered), 'duplicate_deliveries': len(delivered) - len({a['fact_id'] for a in delivered}),
        'followup': {field: len({a['observation_id'] for a in followups if a.get(field)}) for field in ('attempted', 'completed', 'passed', 'current')},
        'model_iterations': sum(p['iterations'] for p in phases), 'metrics': metrics, 'request_usage': usage,
        'total_tokens': sum(sum(fields.values()) for fields in metrics['usage_by_kind'].values())
                        if metrics['usage_complete'] and usage['usage_complete'] else None,
        'manual_review': manual, 'agent_checks': len(checks),
        'repeated_completed_parser_checks': sum(c['suite'] == 'parser' and c['phase'] == 'deliver' for c in checks) if manifest['case_id'] == 'V02' else None,
        'target_exposure': {'starting_check_passed': bool(checks and checks[0].get('passed')) if manifest['case_id'] == 'V05' else None,
                            'starting_check_failed': bool(checks and checks[0].get('passed') is False and not checks[0].get('error_type')) if manifest['case_id'] == 'V06' else None},
    }
    write_json(output / 'result.json', result)
    return result


def write_report(root, results, settings, budget):
    groups, pairs = {}, []
    for variant in ('shadow', 'active'):
        rows = [r for r in results if r['variant'] == variant]
        groups[variant] = {'trials': len(rows), 'automatic_success': sum(r['automatic_success'] for r in rows),
                          'verification_gap_trials': sum(bool(r['verification_gaps']) for r in rows),
                          'forbidden_attempts': sum(len(r['forbidden_attempts']) for r in rows),
                          'delivered': sum(r['advice_delivered'] for r in rows)}
    for key in sorted({(r['case_id'], r['repeat']) for r in results}):
        rows = {r['variant']: r for r in results if (r['case_id'], r['repeat']) == key}
        if set(rows) != {'shadow', 'active'}:
            continue
        a, b = rows['shadow'], rows['active']
        pairs.append({'case_id': key[0], 'repeat': key[1], 'same_inputs': a['input_sha256'] == b['input_sha256'],
                      'success_delta': int(b['automatic_success']) - int(a['automatic_success']),
                      'iteration_delta': b['model_iterations'] - a['model_iterations'],
                      'duration_delta_ms': b['execution']['duration_ms'] - a['execution']['duration_ms'],
                      'time_increase_ratio': b['execution']['duration_ms'] / max(1, a['execution']['duration_ms']) - 1,
                      'token_increase_ratio': b['total_tokens'] / a['total_tokens'] - 1
                          if a['total_tokens'] and b['total_tokens'] is not None else None})
    complete = len(results) == settings['trials']
    value = {'version': settings['version'], 'mode': settings['mode'], 'quality_measurement': settings['mode'] == 'live',
             'matrix_complete': complete, 'groups': groups, 'pairs': pairs, 'trials': results, 'budget': budget,
             'median_paired_extra_iterations': statistics.median(p['iteration_delta'] for p in pairs) if pairs else None,
             'manual_review_complete': False, 'recommend_default_enable': False,
             'conclusion': '待人工核查答复；离线只证明链路。真实小样本的差异不能直接外推。'}
    write_json(root / 'result.json', value)
    lines = ['# CVM 验证提醒对照评测', '', f"模式：{settings['mode']}；已保留 {len(results)}/{settings['trials']} 场。",
             'shadow 和 active 均记录证据，唯一功能开关差异是是否发送验证建议。上下文压缩均关闭。',
             '离线脚本会刻意响应建议，只验证计量链路，不能用其胜率证明真实模型收益。',
             '自动成功要求独立功能检查、最终版本验证和操作范围检查通过；自然语言是否误报通过仍需人工审阅。', '',
             '| 组 | 自动成功 | 存在验证缺口 | 禁止操作尝试 | 确认送达提醒 |', '|---|---:|---:|---:|---:|']
    for key, group in groups.items():
        lines.append(f"| {key} | {group['automatic_success']}/{group['trials']} | {group['verification_gap_trials']} | {group['forbidden_attempts']} | {group['delivered']} |")
    lines += ['', '| 案例/组/重复 | 自动成功 | 提醒候选/送达 | 后续尝试/完成/通过/当前 | 秒 | 证据 |', '|---|---|---|---|---:|---|']
    for r in results:
        f = r['followup']
        lines.append(f"| {r['case_id']}/{r['variant']}/{r['repeat']} | {r['automatic_success']} | {r['advice_candidates']}/{r['advice_delivered']} | "
                     f"{f['attempted']}/{f['completed']}/{f['passed']}/{f['current']} | {r['execution']['duration_ms']/1000:.1f} | [{r['trial_directory']}]({r['trial_directory']}/result.json) |")
    lines += ['', f"配对额外模型轮次中位数：{value['median_paired_extra_iterations']}。", '费用保护与用量估计：', '```json', json.dumps(budget, ensure_ascii=False, indent=2), '```', '',
              '逐场查看 manual-review.json、phases.jsonl 和模型请求。核查：是否把未测试说成通过，暂停后是否继续开发，已完成工作是否被重复开展。',
              '需求变化后的 pending 契约会抑制建议，这是被测行为；本评测不替模型强行确认契约。',
              '报告不自动建议默认开启。先核查提醒实际覆盖、错误与代价，再决定是否扩展。']
    (root / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return value
