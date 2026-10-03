"""Three-arm real Agent experiment: no search_code / lexical / hybrid.

Commands prepare, preflight, run, report. No network is used by prepare/preflight/report.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
from dataclasses import replace
import difflib
import itertools
import json
from pathlib import Path
import random
import shutil
import statistics
import subprocess
import sys
import time

from anthropic import Anthropic
from codeagent.agent import Agent, AgentConfig
from codeagent.config import EnvironmentConfig
from codeagent.context import ContextConfig, ContextManager
from codeagent.events import EventEmitter, UsageTracker
from codeagent.events.sink import CallbackEventSink
from codeagent.hooks.loop_guard import LoopGuardConfig
from codeagent.prompts import PromptRuntime, PromptMode
from codeagent.recovery import RecoveryConfig, RecoveryRuntime
from codeagent.tools import ToolRegistry
from codeagent.tools.base import ToolDefinition
from codeagent.tools.edit import EditFileTool
from codeagent.tools.grep import GrepTool
from codeagent.tools.glob_tool import GlobTool
from codeagent.tools.read import ReadFileTool
from codeagent.tools.search_code import SearchCodeTool
from codeagent.tools.workspace import WorkspaceGuard
from codeagent.code_search.vector import VectorIndex
from codeagent.code_intelligence.models import QueryContext
from evals.agent_adapter import EvidenceSDK, append_record
from evals.evidence import read_json, write_json, file_hash, snapshot
from .worker import EvaluationClient
from .field_benchmark import MeteredProvider, append, score, corpus_hashes, quantile, valid_quote
from .field_agent import records, trace_summary
from .scoring import parse_answer
from .challenge_cases import cases
from .challenge_verify import verify_repair

SEED=20261004


def prepare(base, prior):
    base.mkdir(parents=True,exist_ok=False)
    prior_suite=read_json(prior/'suite.json')
    sources=[]
    for old in prior_suite['sources']:
        if old['kind']!='repository_authored':continue
        source=Path(old['root']);target=base/'corpora'/old['id']
        if corpus_hashes(source)!=old['hashes']:raise ValueError('Prior frozen corpus changed')
        for relative in old['hashes']:
            path=target/relative;path.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source/relative,path)
        sources.append(dict(id=old['id'],root=str(target),hashes=corpus_hashes(target),
            origin=old['source_revision'],scope=old['scope'],language=old['language']))
    tasks=cases({s['id']:Path(s['root']) for s in sources})
    schedule=[];permutations=list(itertools.permutations('ABC'))
    for repeat in (1,2):
        ordered=list(tasks);random.Random(SEED).shuffle(ordered)
        for index,case in enumerate(ordered):
            for arm in permutations[(index+3*(repeat-1))%6]:
                schedule.append(dict(case_id=case['id'],arm=arm,repeat=repeat,id=f"{case['id']}-{arm}-{repeat}"))
    engine=snapshot(Path.cwd(),base/'frozen-engine')
    manifest=dict(seed=SEED,repeats=2,sources=sources,cases=tasks,schedule=schedule,engine=engine,
        arms={'A':'read/glob/grep, no search_code','B':'A + syntax lexical search_code','C':'B + real embedding'},
        limits=dict(model_requests=8,tools=24,tokens=80000,seconds=120,max_output_tokens=3000,
                    embedding_http_requests=700,embedding_characters=4000000),
        protocol='Fresh Agent and restored source per trial. Same per-project workspace path. Balanced arm order. No forced search_code call. LSP absent in all arms. Warm indexes; build/setup costs separate. No tuning. Two repetitions do not create new independent tasks.',
        limitations='New task formulations on previously inspected repositories; not project-held-out or independently human-reviewed. Repair defects are controlled mutations, not historical bug issues. Java dependency stubs and TypeScript behavioral transpilation are limited verifiers.',
        prices=dict(currency='CNY',per_million=dict(input_miss=1,input_hit=.02,output=4,embedding=.5),
            basis='DeepSeek Flash off-peak standardized estimate as requested; not account bill',
            verified_date='2026-10-03',sources=['https://api-docs.deepseek.com/zh-cn/quick_start/pricing/','https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api']))
    write_json(base/'manifest.json',manifest);write_json(base/'manifest-seal.json',dict(sha256=file_hash(base/'manifest.json')))
    print('Frozen:',len(tasks),'tasks,',len(schedule),'trials',flush=True)


def manifest(base):
    if file_hash(base/'manifest.json')!=read_json(base/'manifest-seal.json')['sha256']:raise ValueError('Manifest changed')
    m=read_json(base/'manifest.json')
    for s in m['sources']:
        if corpus_hashes(Path(s['root']))!=s['hashes']:raise ValueError('Source changed')
    for relative,digest in m['engine']['files'].items():
        if relative.startswith('codeagent/') and file_hash(Path.cwd()/relative)!=digest:raise ValueError('Production engine changed')
    return m


def restore(base, source, case=None):
    root=base/'workspaces'/source['id'];root.mkdir(parents=True,exist_ok=True)
    if not (root/'.git').exists():subprocess.run(['git','init','-q',str(root)],check=True,capture_output=True)
    # This function only touches explicitly created evaluation workspaces.
    if not root.resolve().is_relative_to((base/'workspaces').resolve()):raise ValueError('Invalid restore root')
    extra=set(corpus_hashes(root))-set(source['hashes'])
    if extra:raise ValueError('Unexpected files in evaluation workspace: '+repr(sorted(extra)))
    for relative in source['hashes']:
        target=root/relative;target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes((Path(source['root'])/relative).read_bytes())
    if case and case['kind']=='repair':
        change=case['mutation'];path=root/change['path'];text=path.read_text(encoding='utf-8')
        if text.count(change['old'])!=1:raise ValueError('Mutation mismatch')
        path.write_text(text.replace(change['old'],change['new']),encoding='utf-8')
    return root


def preflight(base):
    m=manifest(base);sources={s['id']:s for s in m['sources']};results=[]
    for case in m['cases']:
        if case['kind']!='repair':continue
        root=restore(base,sources[case['source']])
        good=verify_repair(root,case,hidden=True)
        root=restore(base,sources[case['source']],case)
        bad=verify_repair(root,case,hidden=True)
        row=dict(case_id=case['id'],original=good,mutant=bad,valid=good['ok'] and not bad['ok'] and bool(bad.get('checks')))
        results.append(row);print(case['id'],row['valid'],good,bad,flush=True)
    if (base/'preflight.json').exists():
        attempt=1+len(list(base.glob('preflight-attempt-*.json')))
        shutil.copyfile(base/'preflight.json',base/f'preflight-attempt-{attempt}.json')
    write_json(base/'preflight.json',results)
    if not all(r['valid'] for r in results):raise ValueError('Verifier preflight failed; do not run paid trials')


class Checks:
    definition=ToolDefinition(name='run_checks',description='运行当前任务公开的行为契约检查，返回通过数和失败项。隐藏验收在任务结束后单独执行。',input_schema={'type':'object','properties':{},'additionalProperties':False})
    def __init__(self,root,case):self.root,self.case=root,case
    def run(self):
        r=verify_repair(self.root,self.case,hidden=False)
        return json.dumps({k:v for k,v in r.items() if k!='details'},ensure_ascii=False)


def complete_evidence(case,evidence,root):
    valid=[r for r in evidence[:5] if valid_quote(root,r)]
    return bool(case['targets']) and all(
        all(any(r['path']==t['path'] and r['line']<=line<=r['end_line'] for r in valid)
            for line in t['anchors']) for t in case['targets'])


def build(env,root,trial,registry,limits):
    sdk=Anthropic(api_key=env.api_key,base_url=env.base_url,max_retries=0,timeout=45)
    recorder=EvidenceSDK(sdk,trial,max_calls=limits['model_requests'])
    emitter=EventEmitter(CallbackEventSink(lambda e:append_record(trial/'events.jsonl',e.to_dict())))
    tracker=UsageTracker()
    client=EvaluationClient(sdk_client=recorder,base_url=env.base_url,stream=False,event_emitter=emitter,usage_tracker=tracker)
    agent=Agent(client=client,tools=registry,config=AgentConfig(model=env.model_id,max_tokens=limits['max_output_tokens'],max_iterations=limits['model_requests'],
        loop_guard=LoopGuardConfig(max_model_calls=limits['model_requests'],max_tool_calls=limits['tools'],max_total_tokens=limits['tokens'],max_active_seconds=limits['seconds'])),
        context=ContextManager(config=ContextConfig(mode='off',context_window_tokens=100000,transcript_dir=trial/'runtime/transcripts',tool_output_dir=trial/'runtime/outputs')),
        prompt_runtime=PromptRuntime(workspace=root),prompt_mode=PromptMode.NORMAL,allow_subagents=False,event_emitter=emitter,usage_tracker=tracker,
        recovery_runtime=RecoveryRuntime(RecoveryConfig(max_retries=1,side_query_max_retries=0,max_continuations=0,escalated_max_tokens=limits['max_output_tokens'])))
    agent.tools=agent.tools.copy_without({'compact'})
    return agent,recorder


def warm(tool,provider,phase,base,*,vectors):
    start=time.monotonic();provider.phase=phase;before=provider.requests
    tool.service.index.sync(lambda:None);tool.service.index.close_snapshot()
    row=dict(phase=phase,duration_ms=0,embedding_requests=0)
    if vectors:
        deadline=time.monotonic()+900
        context=QueryContext(remaining_seconds=lambda:deadline-time.monotonic())
        attempts=[];failures=0
        while True:
            context.check()
            # Setup only: use the existing max_chunks parameter so one setup
            # slice fits one HTTP batch. Agent search itself remains unchanged.
            index=VectorIndex(tool.service.index,provider,max_chunks=min(20,provider.config.batch_size))
            try:
                index.build('.',context.check,context.remaining_seconds)
                attempts.append(dict(status='ok',stats=dict(index.stats)))
                if not index.stats['pending']:break
            except TimeoutError:
                failures+=1
                attempts.append(dict(status='timeout',stats=dict(index.stats)))
                # Keep paid attempts and partial progress; do not alter model,
                # embeddings, ranking, or the production 10-second deadline.
                append(base/'setup-timeouts.jsonl',dict(phase=phase,attempt=len(attempts),stats=dict(index.stats)))
                if failures==(10 if phase.startswith('cold:') else 3):raise
        row.update(vectors=dict(index.stats),attempts=attempts)
        if index.stats['pending']:raise ValueError('Incomplete prebuild')
    row.update(duration_ms=(time.monotonic()-start)*1000,embedding_requests=provider.requests-before)
    append(base/'setup.jsonl',row)
    return row


def run(base):
    reduced=read_json(base/'reduced-execution-plan.json') if (base/'reduced-execution-plan.json').exists() else None
    if (base/'user-stopped.json').exists() and not (reduced and reduced.get('resume_authorized')):raise ValueError('User stopped this evaluation; do not resume without a new request')
    m=manifest(base)
    if not all(r['valid'] for r in read_json(base/'preflight.json')):raise ValueError('Preflight not passed')
    env=EnvironmentConfig.from_env();limits=m['limits']
    amendment=read_json(base/'embedding-model-amendment.json') if (base/'embedding-model-amendment.json').exists() else None
    if amendment and env.embedding_config.model!=amendment['model']:raise ValueError('Embedding model differs from sealed amendment')
    vector_arm=amendment['index_namespace'] if amendment else 'C'
    if env.model_id not in ('deepseek-flash','deepseek-v4-flash'):raise ValueError('Expected configured DeepSeek Flash')
    provider=MeteredProvider(env.embedding_config,base/'embedding-ledger.jsonl',max_requests=limits['embedding_http_requests'],max_characters=limits['embedding_characters'])
    write_json(base/'provider.json',dict(chat_model=env.model_id,embedding_model=provider.model,dimensions=provider.dimensions))
    sources={s['id']:s for s in m['sources']};by_case={c['id']:c for c in m['cases']}
    if not (base/'cold-ready.json').exists():
        for s in m['sources']:
            root=restore(base,s)
            for arm in ('B','C'):
                tool=SearchCodeTool(root,base/'indexes'/s['id']/(vector_arm if arm=='C' else arm),embedding_provider=provider if arm=='C' else None)
                row=warm(tool,provider,'cold:'+s['id']+':'+arm,base,vectors=arm=='C')
                print('Cold ready',s['id'],arm,round(row['duration_ms']/1000,2),flush=True)
        write_json(base/'cold-ready.json',dict(ready=True))
    done={r['id'] for r in records(base/'results.jsonl')}
    for number,item in enumerate(m['schedule'],1):
        if reduced and item['id'] not in reduced['trial_ids']:continue
        if item['id'] in done:continue
        case=by_case[item['case_id']];arm=item['arm'];root=restore(base,sources[case['source']],case)
        trial=base/'trials'/item['id'];trial.mkdir(parents=True,exist_ok=False)
        initial=corpus_hashes(root);guard=WorkspaceGuard(root);registry=ToolRegistry()
        for tool in (ReadFileTool(workspace_guard=guard),GrepTool(workspace_guard=guard),GlobTool(workspace_guard=guard)):registry.register(tool)
        setup=dict(duration_ms=0,embedding_requests=0)
        if case['kind']=='repair':registry.register(EditFileTool(workspace_guard=guard));registry.register(Checks(root,case))
        if arm!='A':
            search=SearchCodeTool(root,base/'indexes'/case['source']/(vector_arm if arm=='C' else arm),embedding_provider=provider if arm=='C' else None)
            setup=warm(search,provider,'setup:'+item['id'],base,vectors=arm=='C')
            registry.register(search)
        agent,recorder=build(env,root,trial,registry,limits)
        write_json(trial/'tools.json',agent.tools.schemas())
        if case['kind']=='repair':
            instruction='在当前代码中定位并修复下面的回归。可以使用公开检查工具验证；不要改动无关行为，最终简短说明修改。问题：'
        else:
            instruction='只读检查当前源码，使用可用工具自行决定检索方式。提供准确的源码证据，不猜测未实现功能。最终返回 JSON：{"status":"found|not_found|insufficient_evidence","results":[{"path":"相对路径","line":整数,"end_line":整数,"quote":"不超过20行的连续真实源码"}]}，最多5条证据。问题：'
        provider.phase='agent:'+item['id'];embed_before=provider.requests
        started=time.monotonic();row={**item,'source':case['source'],'kind':case['kind'],'setup':setup,'success':False}
        try:
            response=agent.run(instruction+case['query'])
            row.update(outcome=response.stop_reason,final_text=response.final_text)
            if case['kind']!='repair':
                answer=parse_answer(response.final_text);evidence=(answer or {}).get('results',[])
                for entry in evidence:
                    p=(root/entry.get('path','')).resolve()
                    if p.is_relative_to(root) and p.is_file():entry['content_hash']=file_hash(p)
                metrics=score(case,{'results':evidence},[],root)
                row.update(answer=answer,metrics=metrics)
                row['success']=bool((answer or {}).get('status')=='not_found' and not evidence) if case['kind']=='absent' else bool((answer or {}).get('status')=='found' and complete_evidence(case,evidence,root))
        except Exception as exc:row.update(outcome='error',error_type=type(exc).__name__)
        finally:recorder.close()
        row['duration_ms']=(time.monotonic()-started)*1000
        row['embedding_requests']=provider.requests-embed_before
        final=corpus_hashes(root);changed=[p for p in set(initial)|set(final) if initial.get(p)!=final.get(p)]
        row['changed_files']=sorted(changed)
        if case['kind']=='repair':
            check_start=time.monotonic();verification=verify_repair(root,case,hidden=True)
            row.update(verification=verification,verification_ms=(time.monotonic()-check_start)*1000)
            row['success']=verification['ok'] and changed==[case['mutation']['path']]
            expected=(Path(sources[case['source']]['root'])/case['mutation']['path']).read_text(encoding='utf-8').replace(case['mutation']['old'],case['mutation']['new'])
            actual=(root/case['mutation']['path']).read_text(encoding='utf-8')
            (trial/'patch.diff').write_text(''.join(difflib.unified_diff(expected.splitlines(True),actual.splitlines(True),fromfile=case['mutation']['path'],tofile=case['mutation']['path'])),encoding='utf-8')
        else:row['success']=row['success'] and not changed
        write_json(trial/'result-before-trace.json',row)
        row['trace']=trace_summary(trial)
        write_json(trial/'result.json',row);append(base/'results.jsonl',row)
        print(number,'/',len(m['schedule']),item['id'],row['outcome'],'pass',row['success'],'seconds',round(row['duration_ms']/1000,2),'calls',row['trace']['model_requests'],flush=True)
    manifest(base)
    report(base)


def report(base,selected_ids=None,prefix=''):
    m=manifest(base);rows=records(base/'results.jsonl');summaries=[];prices=m['prices']['per_million']
    if selected_ids is not None:rows=[r for r in rows if r['id'] in selected_ids]
    ledger=records(base/'embedding-ledger.jsonl');starts={r['id']:r for r in ledger if r['event']=='start'}
    amendment=read_json(base/'embedding-model-amendment.json') if (base/'embedding-model-amendment.json').exists() else None
    abandoned_cost=0
    costs=defaultdict(float);embed_usage=defaultdict(int)
    for r in ledger:
        if r['event']!='end':continue
        phase=starts[r['id']]['phase'];tokens=r.get('input_tokens') or 0
        rate=amendment['price_per_million'] if amendment and r['id']>amendment['previous_last_request'] else prices['embedding']
        cost=tokens*rate/1e6
        if amendment and r['id']<=amendment['previous_last_request']:abandoned_cost+=cost
        else:costs[phase]+=cost
        embed_usage[phase]+=tokens
    for row in rows:
        u=row['trace']['usage'];row['chat_cost_cny']=chat_cost(u,prices)
        row['embedding_cost_cny']=costs['agent:'+row['id']]
        row['setup_embedding_cost_cny']=costs['setup:'+row['id']]
        row['total_cost_cny']=row['chat_cost_cny']+row['embedding_cost_cny']+row['setup_embedding_cost_cny']
        row['tokens_total']=sum(u.values())
    for arm in 'ABC':
        for kind in ['all','evidence','repair','absent']:
            group=[r for r in rows if r['arm']==arm and (kind=='all' or r['kind']==kind or kind=='evidence' and r['kind'] in ('exact','semantic','cross_file'))]
            if not group:continue
            n=len(group);s=sum(r['success'] for r in group);usage={k:sum(r['trace']['usage'][k] for r in group) for k in group[0]['trace']['usage']}
            total=sum(r['total_cost_cny'] for r in group)
            summaries.append(dict(arm=arm,kind=kind,runs=n,successes=s,success_rate=s/n,
                mean_seconds=statistics.mean(r['duration_ms']/1000 for r in group),p95_seconds=quantile([r['duration_ms']/1000 for r in group],.95),
                mean_tokens=statistics.mean(r['tokens_total'] for r in group),usage=usage,
                mean_chat_cost_cny=sum(r['chat_cost_cny'] for r in group)/n,mean_embedding_cost_cny=sum(r['embedding_cost_cny']+r['setup_embedding_cost_cny'] for r in group)/n,
                total_cost_cny=total,mean_total_cost_cny=total/n,cost_per_success_cny=total/s if s else None,
                mean_calls=statistics.mean(r['trace']['model_requests'] for r in group),
                mean_seconds_with_setup=statistics.mean((r['duration_ms']+r['setup']['duration_ms'])/1000 for r in group),
                usage_complete=all(r['trace']['usage_complete'] for r in group)))
    paired=[];by=defaultdict(dict)
    for row in rows:by[row['case_id'],row['repeat']][row['arm']]=row
    for a,b in [('A','B'),('B','C'),('A','C')]:
        for metric in ('success','duration_ms','tokens_total','total_cost_cny'):
            per_case=defaultdict(list)
            for (case_id,repeat),data in by.items():
                if a in data and b in data:per_case[case_id].append(float(data[b][metric])-float(data[a][metric]))
            values=[statistics.mean(v) for v in per_case.values()];rng=random.Random(SEED);samples=[]
            if values:
                for _ in range(2000):samples.append(statistics.mean(rng.choices(values,k=len(values))))
                paired.append(dict(before=a,after=b,metric=metric,independent_tasks=len(values),delta=statistics.mean(values),ci95=[quantile(samples,.025),quantile(samples,.975)],note='Task-cluster bootstrap, only three repositories; not project generalization.'))
    all_responses=[r for p in (base/'trials').glob('*/model-responses.jsonl') if selected_ids is None or p.parent.name in selected_ids for r in records(p)]
    result=dict(expected=len(m['schedule']),actual=len(rows),summaries=summaries,paired=paired,
        reduced_execution_plan=read_json(base/'reduced-execution-plan.json') if (base/'reduced-execution-plan.json').exists() else None,
        cold_embedding_cost_cny=sum(v for k,v in costs.items() if k.startswith('cold:')),
        abandoned_primary_embedding_cost_cny=abandoned_cost,
        embedding_model_amendment=amendment,
        cold_builds=[r for r in records(base/'setup.jsonl') if r['phase'].startswith('cold:')],
        embedding=dict(requests=len(starts),known_input_tokens=sum(embed_usage.values()),cost_cny=sum(costs.values())+abandoned_cost,failures=sum(r.get('status')=='failed' for r in ledger)),
        chat=dict(requests=len(all_responses),actual_models=dict(Counter(r.get('model','error') for r in all_responses)),known_cost_cny=sum(r['chat_cost_cny'] for r in rows)),
        note='All prices standardized to verified DeepSeek Flash off-peak as requested. Warm execution excludes verifier and cold builds; per-trial setup and cold cost reported separately. Failed trials remain in denominators. Two repetitions are not independent tasks.')
    write_json(base/(prefix+'priced-results.json'),rows);write_json(base/(prefix+'report.json'),result)
    print('Report saved',base/(prefix+'report.json'),flush=True)


def chat_cost(usage,prices):
    return ((usage['input_tokens']+usage['cache_creation_input_tokens'])*prices['input_miss']
        +usage['cache_read_input_tokens']*prices['input_hit']+usage['output_tokens']*prices['output'])/1e6


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8',errors='replace')
    p=argparse.ArgumentParser();p.add_argument('operation',choices=['prepare','preflight','run','report']);p.add_argument('--base',type=Path,required=True)
    p.add_argument('--prior',type=Path,default=Path('eval-results/retrieval-real-20261003'))
    a=p.parse_args();base=a.base.resolve()
    if a.operation=='prepare':prepare(base,a.prior.resolve())
    else:globals()[a.operation](base)
