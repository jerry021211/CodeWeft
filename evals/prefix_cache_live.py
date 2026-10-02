"""Bounded, frozen-source before/after trials using the existing journey Agent.

No production code is switched in-place. The baseline restores only the three
request/prompt/context implementation files from the recorded Git revision.
"""
from __future__ import annotations

import argparse
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlsplit
from uuid import uuid4

from codeagent.context.observation import fingerprint
from evals.context_journey.cases import material
from evals.context_journey.grading import grade, rows
from evals.context_suite.runner import credentials
from evals.context_suite.request_kinds import is_summary_request
from evals.context_suite.token_report import trial_tokens
from evals.evidence import hashes, read_json, seal, snapshot, write_json

ROOT = Path(__file__).resolve().parents[1]
BASELINE_FILES = ('codeagent/agent.py', 'codeagent/context/manager.py', 'codeagent/prompts/runtime.py')
RATES = {'input_tokens': Decimal('1'), 'cache_read_input_tokens': Decimal('.02'),
         'output_tokens': Decimal('4')}


def cost(values):
    """Messages fields are disjoint. Missing usage is unknown, never free."""
    if any(type(values.get(k)) is not int or values[k] < 0 for k in (*RATES, 'cache_creation_input_tokens')):
        return None
    if values['cache_creation_input_tokens']:
        return None  # No screenshot rate for cache creation.
    return float(sum(Decimal(values[k]) * price for k, price in RATES.items()) / 1_000_000)


def stability(requests):
    baselines, changes = {}, []
    for request in requests:
        kind = 'context_summary' if is_summary_request(request) else 'main'
        prior = baselines.get(kind)
        if prior:
            prefix = 0
            for a, b in zip(prior['messages'], request['messages']):
                if fingerprint(a)['hash'] != fingerprint(b)['hash']:
                    break
                prefix += 1
            system_changed = fingerprint(prior.get('system')) != fingerprint(request.get('system'))
            tools_changed = fingerprint(prior.get('tools')) != fingerprint(request.get('tools'))
            changes.append({'request_index': request['request_index'], 'kind': kind,
                'history_rewritten': prefix < len(prior['messages']),
                'first_changed_message': prefix if prefix < len(prior['messages']) else None,
                'system_changed': system_changed,
                'tools_changed': tools_changed,
                'append_only': prefix == len(prior['messages']) and len(request['messages']) > prefix
                    and not system_changed and not tools_changed})
        baselines[kind] = request
    main = [c for c in changes if c['kind'] == 'main']
    positions = [c['first_changed_message'] for c in main if c['history_rewritten']]
    return {'main_comparisons': len(main), 'history_rewrites': len(positions),
            'earliest_changed_message': min(positions) if positions else None,
            'system_changes': sum(c['system_changed'] for c in main),
            'append_only': sum(c['append_only'] for c in main), 'changes': changes}


def review_quality(result, seed):
    """Retain the original grade; narrowly audit a rubric/prompt mismatch.

    T01 limits edited files but its delivery prompt does not prohibit additional
    pure-function checks. Extra tests still cost money and are not erased.
    """
    decision = {'task_success': result['task_success'], 'original_automatic_success': result['task_success'],
                'rubric_correction': False, 'reason': None}
    delivery = next((p['prompt'] for p in seed['phases'] if p['id'] == 'deliver'), '')
    extras = result.get('forbidden_attempts', [])
    if (result['case_id'] == 'T01' and result['failed_checks'] == ['no_forbidden_attempts']
            and delivery == '现在按最终约定实现订单导出，使用工具验证，交付并简要说明实际验证范围。'
            and extras and all(x['phase'] == 'deliver' and x['name'] == 'run_project_tests'
                and x['input'].get('suite') in {'parser', 'totals'}
                and x['reason'] == 'test_outside_current_authorization' for x in extras)):
        decision.update(task_success=True, rubric_correction=True,
            reason='T01 原文仅限制修改文件，没有禁止额外只读功能检查；所有功能、文件范围和完成检查均通过。保留原自动评分及额外测试费用。')
    return decision


def report(root):
    results = []
    for trial in sorted(root.glob('trial-*')):
        if not (trial / 'execution.json').exists():
            continue
        result = read_json(trial / 'result.json')
        review = review_quality(result, read_json(trial / 'seed.json'))
        tokens = trial_tokens(root, result, read_json(root / 'plan.json')['mode'])
        stable = stability(rows(trial / 'model-requests.jsonl'))
        total = tokens['totals']
        input_parts = [total.get(k) for k in ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')]
        denominator = sum(input_parts) if all(v is not None for v in input_parts) else None
        events = rows(trial / 'events.jsonl')
        responses = rows(trial / 'model-responses.jsonl')
        entry = {**result, 'quality_review': review, 'tokens': tokens, 'stability': stable,
                 'offpeak_cny': cost(total) if tokens['complete'] else None,
                 'cost_by_kind': {k: cost(v) if tokens['complete'] else None for k,v in tokens['by_kind'].items()},
                 'total_input': denominator,
                 'cache_hit_rate': total.get('cache_read_input_tokens', 0) / denominator if denominator else None,
                 'returned_models': sorted({r['model'] for r in responses if r.get('model')}),
                 'compactions': sum(e['type']=='context.compacted' and e['payload'].get('status')=='written' for e in events),
                 'cleanup_boundaries': sum(e['type']=='context.request_projected' and bool(e['payload'].get('cleanup_boundary')) for e in events),
                 'effective_policies': sorted({e['payload'].get('cache_policy', 'legacy_before_change') for e in events if e['type']=='context.request_projected'})}
        write_json(trial / 'cost-analysis.json', entry)
        results.append(entry)
    groups = {}
    for variant in ('before', 'after'):
        subset = [r for r in results if r['variant'] == variant]
        if not subset:
            continue
        known = all(r['offpeak_cny'] is not None for r in subset)
        groups[variant] = {'trials': len(subset), 'successes': sum(r['task_success'] for r in subset),
            'reviewed_successes': sum(r['quality_review']['task_success'] for r in subset),
            'cost_by_kind': {kind: sum(r['cost_by_kind'][kind] for r in subset) if known else None
                             for kind in ('main', 'context_summary')},
            'requests_by_kind': {kind: sum(r['tokens']['by_kind'][kind]['requests'] for r in subset)
                                 for kind in ('main', 'context_summary')},
            'all_materials_read': sum(r['coverage']['all_batches_read'] for r in subset),
            'offpeak_cny': sum(r['offpeak_cny'] for r in subset) if known else None,
            'tokens': {k: sum(r['tokens']['totals'][k] for r in subset) if known else None for k in ('input_tokens','cache_read_input_tokens','cache_creation_input_tokens','output_tokens')},
            'seconds': sum(r['execution']['worker_wall_ms'] for r in subset)/1000,
            'history_rewrites': sum(r['stability']['history_rewrites'] for r in subset),
            'compactions': sum(r['compactions'] for r in subset)}
        g=groups[variant]
        denominator=sum(g['tokens'][k] for k in ('input_tokens','cache_read_input_tokens','cache_creation_input_tokens')) if known else 0
        g['cache_hit_rate']=g['tokens']['cache_read_input_tokens']/denominator if denominator else None
    pairs=[]
    for case, repeat in sorted({(r['case_id'], r['repeat']) for r in results}):
        pair={r['variant']:r for r in results if r['case_id']==case and r['repeat']==repeat}
        if set(pair)=={'before','after'}:
            b,a=pair['before'],pair['after']
            bm, am = (read_json(root/r['trial_directory']/'manifest.json') for r in (b,a))
            controls = {'same_material': bm['seed_history_sha256'] == am['seed_history_sha256'],
                'same_initial_files': bm['initial_files'] == am['initial_files'],
                'same_runtime_profile': bm['profile'] == am['profile'],
                'same_tools': all((root/r['trial_directory']/'tool-schemas.json').exists() for r in (b,a))
                    and fingerprint(read_json(root/b['trial_directory']/'tool-schemas.json')) ==
                        fingerprint(read_json(root/a['trial_directory']/'tool-schemas.json')),
                'same_returned_models': b['returned_models'] == a['returned_models']}
            pairs.append({'case':case,'repeat':repeat,'both_passed':b['task_success'] and a['task_success'],
                'comparison_controls': controls,
                'reviewed_both_passed':b['quality_review']['task_success'] and a['quality_review']['task_success'],
                'both_materials_read':b['coverage']['all_batches_read'] and a['coverage']['all_batches_read'],
                'before_cny':b['offpeak_cny'],'after_cny':a['offpeak_cny'],
                'reduction':1-a['offpeak_cny']/b['offpeak_cny'] if all(controls.values()) and b['offpeak_cny'] and a['offpeak_cny'] is not None else None})
    eligible = [p for p in pairs if p['reviewed_both_passed'] and p['both_materials_read'] and p['reduction'] is not None]
    comparison = {'completed_pairs': len(pairs), 'both_completed_eligible_pairs': len(eligible),
        'equal_quality_acceptance_met': bool(pairs) and len(eligible) == len(pairs),
        'raw_cost_reduction_including_failures': None, 'actual_account_debit_verified': False}
    if set(groups) == {'before', 'after'} and groups['before']['offpeak_cny'] and groups['after']['offpeak_cny'] is not None:
        comparison['raw_cost_reduction_including_failures'] = 1 - groups['after']['offpeak_cny'] / groups['before']['offpeak_cny']
    write_json(root/'report.json',{'groups':groups,'pairs':pairs,'comparison':comparison,'trials':results,'actual_billed_cny':None,
        'pricing':'user screenshot offpeak Flash CNY / 1M: miss=1, hit=.02, output=4',
        'scope':'synthetic continuous coding / audit tasks; autonomous tool trajectories may diverge'})
    lines=['# DeepSeek 长任务真实成本对照','',
        f'双方完成且材料读完的有效配对：{len(eligible)}/{len(pairs)}；全部配对的等质验收：{comparison["equal_quality_acceptance_met"]}。',
        '按用户截图的 Flash 闲时价折算：未命中输入 ¥1、命中输入 ¥0.02、输出 ¥4／百万 tokens。实际账单扣款未读取。',
        'before 仅将 Agent、ContextManager、PromptRuntime 三个文件恢复为基线提交版本；其余代码、工具、任务材料和设置统一。',
        '每场独立会话，从空历史开始；真实模型自主调用工具，工具轨迹可分叉。固定输入文件相同，不把自主轨迹当作逐字相同请求。',
        '所有请求（含摘要和失败尝试）保留证据；缺少 usage 不算零费用。命中率按输入总量加权。',
        '每场主模型 system 开头加独立等长随机标记，减轻组间缓存预热；标记在同一场内不变。辅助摘要保留自然请求；服务端缓存是否实际保留以 usage 为准。',
        '任务复核保留原自动评分。T01 额外运行只读测试不违反任务原文的文件修改范围，单独记录评分修正；不会豁免功能失败、错误字段、未完成阶段或越界修改。','',
        '| 场次 | 任务复核（原自动） | 背景完整读取 | 主/摘要请求 | 闲时费用 ¥ | 命中率 | 历史改写 | 压缩 | 秒 |',
        '|---|---|---|---:|---:|---:|---:|---:|---:|']
    for r in results:
        t=r['tokens']['by_kind']
        money=f"{r['offpeak_cny']:.6f}" if r['offpeak_cny'] is not None else '未知'
        hit=f"{r['cache_hit_rate']:.1%}" if r['cache_hit_rate'] is not None else '未知'
        lines.append(f"| {r['case_id']}/{r['variant']}/r{r['repeat']} | {r['quality_review']['task_success']}（{r['task_success']}） | {r['coverage']['all_batches_read']} | {t['main']['requests']}/{t['context_summary']['requests']} | {money} | {hit} | {r['stability']['history_rewrites']} | {r['compactions']} | {r['execution']['worker_wall_ms']/1000:.1f} |")
    lines+=['','原始源码快照、材料、请求、响应 usage、阶段验收及错误均保存在同目录。report.json 包含按调用类型拆分的费用和最早改写位置。',
        '自动功能/字段验收不等于全面人工质量审查；小样本不保证其他真实项目同样省钱。']
    lines += ['', '**全部场次汇总（保留失败成本，不能当作等质节省率）**', '',
        '| 版本 | 任务复核通过 | 未命中输入 | 命中输入 | 输出 | 加权命中率 | 主调用费用 ¥ | 摘要费用 ¥ | 总费用 ¥ |',
        '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for variant, group in groups.items():
        if group['offpeak_cny'] is None:
            lines.append(f'| {variant} | {group["reviewed_successes"]}/{group["trials"]} | 用量不完整 | — | — | — | — | — | 未知 |')
            continue
        t = group['tokens']
        lines.append(f'| {variant} | {group["reviewed_successes"]}/{group["trials"]} | {t["input_tokens"]:,} | {t["cache_read_input_tokens"]:,} | {t["output_tokens"]:,} | {group["cache_hit_rate"]:.2%} | {group["cost_by_kind"]["main"]:.6f} | {group["cost_by_kind"]["context_summary"]:.6f} | {group["offpeak_cny"]:.6f} |')
    lines += ['', '**双方都完成任务且材料读完的配对**', '',
        '| 案例 | 旧版 ¥ | 新版 ¥ | 费用减少比例（负数为增加） |', '|---|---:|---:|---:|']
    for pair in eligible:
        lines.append(f'| {pair["case"]}/r{pair["repeat"]} | {pair["before_cny"]:.6f} | {pair["after_cny"]:.6f} | {pair["reduction"]:.2%} |')
    if not eligible:
        lines += ['| 无完整有效配对 | — | — | 不计算 |']
    lines += ['', '其余配对仍计入上方全部用量；不能把未完成任务的较低费用解释为节省。', '', '**验收差异与限制**', '']
    for r in results:
        if r['failed_checks']:
            lines.append(f'- {r["trial_directory"]}：原自动检查失败 {", ".join(r["failed_checks"])}。'
                         + (r['quality_review']['reason'] or '未豁免，任务验收失败。'))
    lines += ['', '这次 before 还包含对应提交的提示词装配文本差异，不是只改变缓存策略的单因素消融。',
              '两个任务是小型纯函数编码/日志练习，背景材料为合成数据；未覆盖完整大型仓库工程质量、真实重启后的计费或 Pro 模型。']
    (root/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return results


def prepare(root, mode, repeats, cases, batches, log_chars, baseline):
    if not 1 <= repeats <= 3 or not cases or len(set(cases)) != len(cases) or len(cases) > 3:
        raise ValueError('Use 1..3 repeats and 1..3 distinct cases; keep the matrix bounded.')
    if len(cases) * repeats * 2 > 12:
        raise ValueError('At most 12 trials per experiment.')
    root.mkdir(parents=True,exist_ok=False)
    env=credentials(mode)
    if mode == 'live' and (env['MODEL_ID'] not in {'deepseek-flash', 'deepseek-v4-flash', 'deepseek-v4-flash-vision-exp'}
            or urlsplit(env.get('BASE_URL') or '').hostname != 'api.deepseek.com'):
        raise ValueError('This fixed CNY price table is only for the official DeepSeek Flash endpoint.')
    revision=subprocess.check_output(['git','rev-parse',baseline],cwd=ROOT,text=True).strip()
    snapshots={}
    for variant in ('before','after'):
        engine=root/('engine-'+variant)
        snapshot(ROOT,engine)
        if variant=='before':
            for name in BASELINE_FILES:
                data=subprocess.check_output(['git','show',f'{revision}:{name}'],cwd=ROOT)
                (engine/name).write_bytes(data)
        snapshots[variant]=hashes(engine)
    changed=[k for k in snapshots['after'] if snapshots['after'][k]!=snapshots['before'].get(k)]
    assert sorted(changed)==sorted(BASELINE_FILES),changed
    write_json(root/'source-manifest.json',{'baseline_revision':revision,'changed_files':changed,'snapshots':snapshots})
    schedule=[]
    for repeat in range(1,repeats+1):
        for ci,case in enumerate(cases):
            order=('before','after') if (repeat+ci)%2 else ('after','before')
            schedule.extend((case,variant,repeat) for variant in order)
    write_json(root/'plan.json',{'mode':mode,'schedule':schedule,'batches':batches,'log_chars':log_chars,
        'max_calls_per_trial':48,'max_tokens':8000,'trial_timeout_seconds':900,
        'offpeak_budget_cny':15,'reasoning':'provider default; unchanged in both arms'})
    for index,(case,variant,repeat) in enumerate(schedule,1):
        trial=root/f'trial-{index:03d}-{case}-{variant}-r{repeat}'
        seed,gold=material(case,batches=batches,log_chars=log_chars)
        write_json(trial/'seed.json',seed)
        write_json(trial/'gold.json',gold)
        workspace=trial/'workspace'
        for relative,content in seed['workspace_files'].items():
            path=workspace/relative
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(content,encoding='utf-8')
        profile={'suite':'context-journey-v1','mode':mode,'model':env.get('MODEL_ID','offline'),
            'summary_model':env.get('MODEL_ID','offline'), 'context':{},
            'max_iterations':8,'max_tokens':8000,'max_api_calls':48,'max_total_tokens':0,
            'allowed_writes':list(seed['workspace_files'])+['reports/candidate.json'],
            'tracing':{'enabled':False},'provider_host':urlsplit(env.get('BASE_URL') or '').hostname,
            'sdk_retries':0,'recovery_retries':1,'wall_timeout_seconds':900}
        write_json(trial/'manifest.json',{'case_id':case,'variant':variant,'repeat':repeat,'scale':'production',
            'profile':profile,'seed_history_sha256':seed['input_sha256'],'initial_files':hashes(workspace),
            'engine_sha256':hashlib.sha256(json.dumps(snapshots[variant],sort_keys=True).encode()).hexdigest(),
            'cache_isolation_nonce':uuid4().hex})
    return root


def worker(trial):
    """Patch evaluation boundaries only, leaving both frozen Agent loops intact."""
    from evals.agent_adapter import EvidenceSDK, EvaluationCallLimit
    from codeagent.prompts.runtime import PromptRuntime
    from codeagent.prompts.models import PromptAssemblyResult
    from evals.context_journey.worker import execute
    spec=read_json(trial/'manifest.json')
    original=PromptRuntime.assemble
    def assemble(self, **kwargs):
        result=original(self,**kwargs)
        text='[Evaluation session '+spec['cache_isolation_nonce']+']\n'+result.system_prompt
        return PromptAssemblyResult(text,result.trace,hashlib.sha256(text.encode()).hexdigest())
    PromptRuntime.assemble=assemble
    original_call=EvidenceSDK._record_call
    spent=Decimal('0')
    remaining=Decimal(os.getenv('PREFIX_EVAL_REMAINING_CNY','15'))
    def bounded(self,payload,invoke):
        nonlocal spent
        # Conservative reservation: every request may fill the 1M context;
        # absent output limit also reserves 1M output (above advertised 384K).
        reserve=Decimal(1048576 + 4*(payload.get('max_tokens') or 1048576))/1_000_000
        if spec['profile']['mode']=='live' and spent+reserve>remaining:
            raise EvaluationCallLimit('budget_exceeded:offpeak_cost_reserve')
        response=original_call(self,payload,invoke)
        if spec['profile']['mode']=='live':
            usage=response.usage.model_dump()
            value=cost(usage)
            if value is None:
                raise EvaluationCallLimit('unknown_usage_or_price')
            spent+=Decimal(str(value))
        return response
    EvidenceSDK._record_call=bounded
    execute(trial)


def run(root, limit=None):
    plan=read_json(root/'plan.json')
    env=credentials(plan['mode'])
    env.update(LANGSMITH_TRACING='false',LANGCHAIN_TRACING_V2='false')
    completed=report(root)
    if any(r['offpeak_cny'] is None for r in completed) and plan['mode']=='live':
        raise RuntimeError('Previous live trial has incomplete billing evidence; inspect before continuing.')
    spent=sum(r['offpeak_cny'] or 0 for r in completed)
    dispatched=0
    for trial in sorted(root.glob('trial-*')):
        if (trial/'execution.json').exists():
            continue
        if limit is not None and dispatched>=limit:
            break
        spec=read_json(trial/'manifest.json')
        engine=root/('engine-'+spec['variant'])
        child={**env,'PYTHONPATH':str(engine),'PYTHONDONTWRITEBYTECODE':'1','PYTHONIOENCODING':'utf-8',
            'CODEAGENT_DATA_DIR':str(trial/'runtime'),'PREFIX_EVAL_REMAINING_CNY':str(15-spent)}
        start=time.monotonic()
        print('START '+trial.name,flush=True)
        with (trial/'stdout.txt').open('w',encoding='utf-8') as out,(trial/'stderr.txt').open('w',encoding='utf-8') as err:
            process=subprocess.Popen([sys.executable,'-P','-B','-m','evals.prefix_cache_live','worker',str(trial)],
                cwd=trial/'workspace',env=child,stdout=out,stderr=err)
            try:
                code=process.wait(timeout=plan['trial_timeout_seconds'])
                execution=read_json(trial/'worker-result.json') if (trial/'worker-result.json').exists() else {'execution_status':'worker_crash'}
                if code:
                    execution.update(execution_status='worker_crash',returncode=code)
            except subprocess.TimeoutExpired:
                process.kill();process.wait()
                execution={'execution_status':'timeout'}
        execution['worker_wall_ms']=round((time.monotonic()-start)*1000,3)
        write_json(trial/'execution.json',execution)
        grade(trial,execution)
        results=report(root)
        last=next(r for r in results if r['trial_directory']==trial.name)
        print(json.dumps({'trial':trial.name,'pass':last['task_success'],'cost':last['offpeak_cny'],
            'calls':last['execution'].get('api_calls'),'all_read':last['coverage']['all_batches_read']},ensure_ascii=False),flush=True)
        if plan['mode']=='live' and last['offpeak_cny'] is None:
            raise RuntimeError('Incomplete billing evidence; stop matrix, preserve failed attempt.')
        spent+=last['offpeak_cny'] or 0
        dispatched+=1
    report(root)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','run','worker','report','seal'))
    parser.add_argument('root',type=Path)
    parser.add_argument('--mode',choices=('offline','live'),default='offline')
    parser.add_argument('--repeats',type=int,default=2)
    parser.add_argument('--cases',nargs='+',default=['T01','T06'])
    parser.add_argument('--batches',type=int,default=8)
    parser.add_argument('--log-chars',type=int,default=60000)
    parser.add_argument('--baseline',default='512b627')
    parser.add_argument('--limit',type=int)
    args=parser.parse_args();root=args.root.resolve()
    if args.action=='prepare':
        prepare(root,args.mode,args.repeats,args.cases,args.batches,args.log_chars,args.baseline)
    elif args.action=='run': run(root,args.limit)
    elif args.action=='worker': worker(root)
    elif args.action=='report': report(root)
    else: seal(root)


if __name__=='__main__':
    main()
