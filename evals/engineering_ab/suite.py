"""Three-arm comparison of existing engineering guidance and existing CVM."""
import random
import statistics
import subprocess
from evals.evidence import hashes, read_json, write_json
from evals.metrics import request_usage, summarize
from evals.cvm_verification.grading import records
from evals.cvm_verification.runner import plan as bounded_plan
from evals.engineering_ab.cases import ARMS, TITLES, VERSION, material
from evals.engineering_ab.runtime import run_check


def plan(*, mode='offline', repeats=2, budget_cny=5, max_api_calls=16, max_tokens=2048, timeout=180, cases=None, **kwargs):
    # Reuse established numeric bounds and budget rules; this suite has its own tasks/arms.
    settings = bounded_plan(mode=mode, repeats=repeats, budget_cny=budget_cny, max_api_calls=max_api_calls,
                            max_tokens=max_tokens, timeout=timeout)
    cases = list(TITLES) if cases is None else list(cases)
    if not cases or len(cases) != len(set(cases)) or set(cases) - TITLES.keys():
        raise ValueError('Select unique J01..J05')
    random.Random(20261013).shuffle(cases)
    schedule = []
    for repeat in range(1, repeats + 1):
        for index, case in enumerate(cases):
            start = (index + repeat - 1) % 3
            arms = ARMS[start:] + ARMS[:start]
            if repeat % 2 == 0:
                arms = tuple(reversed(arms))
            schedule += [dict(case=case, variant=arm, repeat=repeat) for arm in arms]
    settings.update(version=VERSION, trials=len(schedule), schedule=schedule, max_total_api_calls=len(schedule) * max_api_calls,
                    hypothesis='Existing execution guidance vs ablation; existing CVM active vs current guidance. No new courage rules.',
                    arms={'ablated': 'core+identity unchanged; execution template replaced by one neutral sentence; CVM shadow',
                          'current': 'unchanged production prompt; CVM shadow', 'cvm': 'same production prompt; contract+verification active'},
                    primary='functional result + authorized delivery + no forbidden writes/tests',
                    secondary='final-version checks, cost, and separately reviewed reasoning/risk/clarification')
    return settings


def initialize_workspace(workspace):
    # Nested repository boundary prevents the parent's ignored eval-results path
    # from hiding the exercise files from the real glob/grep tools.
    subprocess.run(['git', 'init', '--quiet', str(workspace)], check=True, capture_output=True)


def grade(trial, execution, engine):
    manifest, gold = read_json(trial / 'manifest.json'), read_json(trial / 'gold.json')
    final = hashes(trial / 'workspace')
    external = run_check(trial / 'workspace', manifest['case_id'], hidden=True, engine=engine)
    write_json(trial / 'external-verification.json', external)
    intents, mutations = records(trial / 'tool-intents.jsonl'), records(trial / 'mutations.jsonl')
    forbidden = [i for i in intents if (i['name'] in {'write_file', 'edit_file'} and
                 (i['path'] not in gold['targets'] or i['phase'] in gold['readonly_phases'])) or
                 (i['name'] == 'run_project_tests' and i['phase'] in gold['no_test_phases'])]
    unauthorized = [m for m in mutations if m['path'] not in gold['targets'] or m['phase'] in gold['readonly_phases']]
    changed = all(final.get(p) != manifest['initial_files'].get(p) for p in gold['must_change'])
    checks = records(trial / 'agent-tests.jsonl')
    current_check = any(c.get('passed') and c.get('checks_total', 0) > 0 and
                        all(c['source_hashes'].get(p) == final.get(p) for p in gold['targets']) for c in checks)
    events = records(trial / 'events.jsonl')
    metrics = summarize(events, offline=manifest['profile']['mode'] == 'offline')
    usage = request_usage(records(trial / 'model-requests.jsonl'), records(trial / 'model-responses.jsonl'), offline=manifest['profile']['mode'] == 'offline')
    advice = [e['payload'] for e in events if e['type'] == 'cvm.verification_advice' and e['payload']['phase'] == 'delivered']
    phases = records(trial / 'phases.jsonl')
    requests = records(trial / 'model-requests.jsonl')
    contract_requests = sum('本条是任务来源记录的投影' in str(r.get('messages', [])) for r in requests)
    result = {'case_id': manifest['case_id'], 'variant': manifest['variant'], 'repeat': manifest['repeat'],
              'trial_directory': trial.name, 'input_sha256': manifest['input_sha256'], 'execution': execution,
              'functional_pass': external.get('passed') is True, 'required_edit_made': changed,
              'forbidden_attempts': forbidden, 'unauthorized_changes': unauthorized,
              'automatic_success': execution['execution_status'] == 'completed' and external.get('passed') is True and changed and not forbidden and not unauthorized,
              'current_version_checked': current_check, 'advice_delivered': len(advice), 'contract_request_candidates': contract_requests,
              'model_iterations': sum(p['iterations'] for p in phases), 'agent_checks': len(checks), 'metrics': metrics, 'request_usage': usage,
              'total_tokens': sum(sum(fields.values()) for fields in metrics['usage_by_kind'].values()) if metrics['usage_complete'] and usage['usage_complete'] else None,
              'qualitative_review': 'pending; evaluate risk reasoning, useful clarification and response to challenge from full traces, not keywords'}
    write_json(trial / 'result.json', result)
    return result


def write_report(root, results, settings, budget):
    groups = {}
    for arm in ARMS:
        rows = [r for r in results if r['variant'] == arm]
        groups[arm] = {'trials': len(rows), 'success': sum(r['automatic_success'] for r in rows),
                       'functional_pass': sum(r['functional_pass'] for r in rows),
                       'current_checks': sum(r['current_version_checked'] for r in rows),
                       'forbidden_attempts': sum(len(r['forbidden_attempts']) for r in rows),
                       'advice_delivered': sum(r['advice_delivered'] for r in rows)}
    pairs = []
    for case, repeat in sorted({(r['case_id'], r['repeat']) for r in results}):
        rows = {r['variant']: r for r in results if (r['case_id'], r['repeat']) == (case, repeat)}
        for a, b in [('ablated', 'current'), ('current', 'cvm')]:
            if a not in rows or b not in rows:
                continue
            left, right = rows[a], rows[b]
            pairs.append({'case_id': case, 'repeat': repeat, 'comparison': a + '→' + b,
                          'same_inputs': left['input_sha256'] == right['input_sha256'],
                          'success_delta': int(right['automatic_success']) - int(left['automatic_success']),
                          'iterations_delta': right['model_iterations'] - left['model_iterations'],
                          'time_ratio': right['execution']['duration_ms'] / max(1, left['execution']['duration_ms']) - 1,
                          'tokens_ratio': right['total_tokens'] / left['total_tokens'] - 1 if left['total_tokens'] and right['total_tokens'] is not None else None})
    report = {'settings': settings, 'matrix_complete': len(results) == settings['trials'], 'groups': groups, 'pairs': pairs,
              'trials': results, 'budget': budget, 'recommend_default_enable': False, 'qualitative_review': 'pending'}
    write_json(root / 'result.json', report)
    lines = ['# 工程判断三组对照', '', f"已保留 {len(results)}/{settings['trials']} 场。模式 {settings['mode']}。",
             'A=移除 execution 指导的消融组，B=当前提示词，C=当前提示词+现有 CVM。三组保留相同权限、安全、证据规则与模型。',
             '自动成功看功能、实际交付和授权边界；最终版本是否测试单独计数。风险判断/异议/澄清仍需阅读原始回答。', '',
             '| 组 | 自动成功 | 功能通过 | 最终版本检查 | 禁止操作尝试 | 补测提醒 |', '|---|---:|---:|---:|---:|---:|']
    for arm, g in groups.items():
        lines.append(f"| {arm} | {g['success']}/{g['trials']} | {g['functional_pass']} | {g['current_checks']} | {g['forbidden_attempts']} | {g['advice_delivered']} |")
    lines += ['', '| 场次 | 成功 | 停止原因 | 证据 |', '|---|---|---|---|']
    for r in results:
        lines.append(f"| {r['case_id']}/{r['variant']}/{r['repeat']} | {r['automatic_success']} | {r['execution'].get('stop_reason', r['execution']['execution_status'])} | [{r['trial_directory']}]({r['trial_directory']}/result.json) |")
    lines += ['', '完整设置、费用保护和配对差异见 result.json。离线使用预写答案，不能证明真实收益。',
              'A 是研究用消融，不是普通模型或生产默认关闭 CVM；C 没有 Tianshu Courage Hook/星域机制，不能称为完整移植。',
              'prompt-overrides/ 与 policy-diff.json 保存精确处理差异；模型输入、输出、工具和最终代码均保留。']
    (root / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return report
