"""One bounded real LSP arm against the saved C baseline; never rerun A/B/C."""
from __future__ import annotations
import argparse
from collections import Counter
import difflib
import hashlib
import json
from pathlib import Path
import random
import sqlite3
import statistics
import sys
import time

from codeagent.config import EnvironmentConfig
from codeagent.lsp import LspConfig
from codeagent.tools import ToolRegistry
from codeagent.tools.edit import EditFileTool
from codeagent.tools.grep import GrepTool
from codeagent.tools.glob_tool import GlobTool
from codeagent.tools.read import ReadFileTool
from codeagent.tools.lsp import LspTool
from codeagent.tools.search_code import SearchCodeTool
from codeagent.tools.workspace import WorkspaceGuard
from evals.agent_adapter import append_record
from evals.evidence import read_json,write_json,file_hash
from .challenge import manifest,restore,warm,build,Checks,complete_evidence,chat_cost
from .challenge_verify import verify_repair
from .challenge_analysis import evidence_audit
from .field_agent import records,trace_summary
from .field_benchmark import MeteredProvider,corpus_hashes,score
from .scoring import parse_answer


class ObservedLsp(LspTool):
    def __init__(self,root,trial):
        super().__init__(root,LspConfig())
        self.processes={}
        original=self.service.execute
        def execute(operation,file_path,line=1,character=1,**kwargs):
            start=time.monotonic()
            try:
                value=original(operation,file_path,line,character,**kwargs)
                append_record(trial/'lsp-calls.jsonl',dict(operation=operation,path=file_path,
                    duration_ms=(time.monotonic()-start)*1000,result=value))
                return value
            except BaseException as exc:
                append_record(trial/'lsp-calls.jsonl',dict(operation=operation,path=file_path,
                    duration_ms=(time.monotonic()-start)*1000,error_type=type(exc).__name__))
                raise
            finally:
                for session in self.service.sessions.values():
                    process=session['rpc'].process;self.processes[process.pid]=process
        self.service.execute=execute


def prepare(base):
    m=manifest(base);out=base/'lsp-arm-v1'
    if (out/'plan.json').exists():raise ValueError('Plan already frozen')
    readiness=read_json(out/'preflight.json')
    if len(readiness)!=3 or not all(x['matched'] and x['released'] for x in readiness):raise ValueError('Real server readiness failed')
    originals={name:file_hash(base/name) for name in ('results.jsonl','report.json','balanced-report.json','balanced-analysis.json','manifest.json','embedding-ledger.jsonl')}
    copied={}
    for s in m['sources']:
        dest=out/'indexes'/s['id'];dest.mkdir(parents=True)
        for name in ('source-v2.sqlite3','vectors-v2.sqlite3'):
            source=base/'indexes'/s['id']/'C-flash'/name
            with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as old,sqlite3.connect(dest/name) as new:old.backup(new)
            copied[str(source)]=file_hash(source)
    schedule=[dict(c,arm='D',id=c['id'].replace('-C-','-D-')) for c in m['schedule'] if c['arm']=='C' and c['repeat']==1]
    plan=dict(schedule=schedule,baseline='C first repeat',trials=18,original_hashes=originals,original_index_hashes=copied,
        engine='same frozen production files as ABC',chat_model=read_json(base/'provider.json')['chat_model'],
        embedding_model='qwen3.7-text-embedding-flash',limits=m['limits'],
        lsp=dict(timeout_seconds=5,feedback_seconds=1.5,prewarm=False),
        protocol='Only add the production lsp tool and its existing PostToolUse feedback. Same prompts, tasks, budgets, workspace paths and source hashes. Fresh LSP owner per task, cold on demand; copied warm C vector caches; no new whole-corpus embedding build. No forced LSP use. No mocks or injected model actions.',
        limitations='Historical C control, not interleaved contemporaneous randomization. Same limited source scope lacks full build manifests/dependencies. Existing repair mutations are logic errors, generally invisible to type diagnostics. Full server readiness probes are separate from model trials.')
    write_json(out/'plan.json',plan);write_json(out/'plan-seal.json',dict(sha256=file_hash(out/'plan.json')))


def run(base):
    m=manifest(base);out=base/'lsp-arm-v1';plan=read_json(out/'plan.json')
    assert file_hash(out/'plan.json')==read_json(out/'plan-seal.json')['sha256']
    env=EnvironmentConfig.from_env()
    if env.model_id!=plan['chat_model'] or env.embedding_config.model!=plan['embedding_model']:raise ValueError('Configured models changed')
    provider=MeteredProvider(env.embedding_config,out/'embedding-ledger.jsonl',max_requests=100,max_characters=100000)
    sources={s['id']:s for s in m['sources']};cases={c['id']:c for c in m['cases']};done={r['id'] for r in records(out/'results.jsonl')}
    for number,item in enumerate(plan['schedule'],1):
        if item['id'] in done:continue
        case=cases[item['case_id']];root=restore(base,sources[case['source']],case)
        trial=out/'trials'/item['id'];trial.mkdir(parents=True,exist_ok=False)
        guard=WorkspaceGuard(root);registry=ToolRegistry();initial=corpus_hashes(root)
        for tool in (ReadFileTool(workspace_guard=guard),GrepTool(workspace_guard=guard),GlobTool(workspace_guard=guard)):registry.register(tool)
        if case['kind']=='repair':registry.register(EditFileTool(workspace_guard=guard));registry.register(Checks(root,case))
        search=SearchCodeTool(root,out/'indexes'/case['source'],embedding_provider=provider)
        setup=warm(search,provider,'setup:'+item['id'],out,vectors=True);registry.register(search)
        lsp=ObservedLsp(root,trial);registry.register(lsp)
        agent,recorder=build(env,root,trial,registry,m['limits']);write_json(trial/'tools.json',agent.tools.schemas())
        if case['kind']=='repair':
            instruction='在当前代码中定位并修复下面的回归。可以使用公开检查工具验证；不要改动无关行为，最终简短说明修改。问题：'
        else:
            instruction='只读检查当前源码，使用可用工具自行决定检索方式。提供准确的源码证据，不猜测未实现功能。最终返回 JSON：{"status":"found|not_found|insufficient_evidence","results":[{"path":"相对路径","line":整数,"end_line":整数,"quote":"不超过20行的连续真实源码"}]}，最多5条证据。问题：'
        provider.phase='agent:'+item['id'];before=provider.requests;started=time.monotonic()
        row={**item,'source':case['source'],'kind':case['kind'],'setup':setup,'success':False}
        try:
            response=agent.run(instruction+case['query']);row.update(outcome=response.stop_reason,final_text=response.final_text)
            if case['kind']!='repair':
                answer=parse_answer(response.final_text);evidence=(answer or {}).get('results',[])
                for entry in evidence:
                    path=(root/entry.get('path','')).resolve()
                    if path.is_relative_to(root) and path.is_file():entry['content_hash']=file_hash(path)
                row.update(answer=answer,metrics=score(case,{'results':evidence},[],root))
                row['success']=bool((answer or {}).get('status')=='not_found' and not evidence) if case['kind']=='absent' else bool((answer or {}).get('status')=='found' and complete_evidence(case,evidence,root))
        except Exception as exc:row.update(outcome='error',error_type=type(exc).__name__)
        finally:
            row['duration_ms']=(time.monotonic()-started)*1000
            recorder.close();close_start=time.monotonic();lsp.close()
            row['lsp_cleanup_ms']=(time.monotonic()-close_start)*1000
            row['lsp_processes']=[dict(pid=p.pid,exit_code=p.poll()) for p in lsp.processes.values()]
            row['processes_released']=all(p.poll() is not None for p in lsp.processes.values())
        row['embedding_requests']=provider.requests-before
        final=corpus_hashes(root);changed=sorted(p for p in set(initial)|set(final) if initial.get(p)!=final.get(p));row['changed_files']=changed
        if case['kind']=='repair':
            started=time.monotonic();verification=verify_repair(root,case,hidden=True)
            row.update(verification=verification,verification_ms=(time.monotonic()-started)*1000)
            row['success']=verification['ok'] and changed==[case['mutation']['path']]
            original=(Path(sources[case['source']]['root'])/case['mutation']['path']).read_text(encoding='utf-8').replace(case['mutation']['old'],case['mutation']['new'])
            actual=(root/case['mutation']['path']).read_text(encoding='utf-8')
            (trial/'patch.diff').write_text(''.join(difflib.unified_diff(original.splitlines(True),actual.splitlines(True))),encoding='utf-8')
        else:row['success']=row['success'] and not changed
        write_json(trial/'result-before-trace.json',row);row['trace']=trace_summary(trial)
        write_json(trial/'result.json',row);append_record(out/'results.jsonl',row)
        print(number,'/ 18',item['id'],row['outcome'],'pass',row['success'],'seconds',round(row['duration_ms']/1000,2),'feedback',row['trace']['feedback_visible'],'lsp',len(records(trial/'lsp-calls.jsonl')),flush=True)
    manifest(base)
    for name,digest in plan['original_hashes'].items():assert file_hash(base/name)==digest
    for path,digest in plan['original_index_hashes'].items():assert file_hash(Path(path))==digest
    summarize(base)


def summarize(base):
    out=base/'lsp-arm-v1';m=read_json(base/'manifest.json');new=records(out/'results.jsonl')
    old=[r for r in read_json(base/'balanced-priced-results.json') if r['arm']=='C']
    prices=m['prices']['per_million'];ledger=records(out/'embedding-ledger.jsonl');costs=Counter()
    for r in ledger:
        if r['event']=='end':costs[r['phase']]+=(r.get('input_tokens') or 0)*.125/1e6
    for row in new:
        row['chat_cost_cny']=chat_cost(row['trace']['usage'],prices)
        row['total_cost_cny']=row['chat_cost_cny']+costs['agent:'+row['id']]+costs['setup:'+row['id']]
        row['tokens_total']=sum(row['trace']['usage'].values())
    audits=evidence_audit(base,old+new,m);by_audit={r['id']:r for r in audits};summaries=[]
    for arm,group in [('C',old),('D',new)]:
        summaries.append(dict(arm=arm,runs=len(group),successes=sum(r['success'] for r in group),
            secondary_successes=sum(by_audit[r['id']]['secondary_success'] for r in group),
            repair_successes=sum(r['success'] for r in group if r['kind']=='repair'),
            mean_seconds=statistics.mean(r['duration_ms']/1000 for r in group),
            mean_with_setup_seconds=statistics.mean((r['duration_ms']+r['setup']['duration_ms'])/1000 for r in group),
            mean_with_cleanup_seconds=statistics.mean((r['duration_ms']+r.get('lsp_cleanup_ms',0))/1000 for r in group),
            mean_tokens=statistics.mean(r['tokens_total'] for r in group),
            mean_cost_cny=statistics.mean(r['total_cost_cny'] for r in group),
            total_cost_cny=sum(r['total_cost_cny'] for r in group),
            mean_calls=statistics.mean(r['trace']['model_requests'] for r in group),
            feedback_visible_tasks=sum(r['trace']['feedback_visible'] for r in group)))
    paired=[];by_old={r['case_id']:r for r in old}
    for metric in ('success','duration_ms','tokens_total','total_cost_cny'):
        values=[float(r[metric])-float(by_old[r['case_id']][metric]) for r in new];rng=random.Random(m['seed'])
        samples=sorted(statistics.mean(rng.choices(values,k=len(values))) for _ in range(2000))
        paired.append(dict(metric=metric,delta=statistics.mean(values),ci95=[samples[50],samples[1949]]))
    calls=[dict(trial=r['id'],**v) for r in new for v in records(out/'trials'/r['id']/'lsp-calls.jsonl')]
    statuses=Counter(v.get('result',{}).get('status',v.get('error_type')) for v in calls)
    observed=[]
    for r in new:
        trial=out/'trials'/r['id'];responses=records(trial/'model-responses.jsonl')
        tools=[b for v in responses for b in v.get('content',[]) if b.get('type')=='tool_use']
        events=records(trial/'events.jsonl')
        observed.append(dict(id=r['id'],model_tool_requests=dict(Counter(t['name'] for t in tools)),
            started_tools=dict(Counter(e['payload'].get('name') for e in events if e['type']=='tool.started')),
            feedback_visible=r['trace']['feedback_visible'],feedback_statuses=r['trace']['feedback_statuses'],
            feedback_error_count=r['trace']['feedback_error_count'],processes=r['lsp_processes']))
    plan=read_json(out/'plan.json')
    preserved=all(file_hash(base/name)==digest for name,digest in plan['original_hashes'].items()) and all(file_hash(Path(path))==digest for path,digest in plan['original_index_hashes'].items())
    schema_checks=[]
    for row in new:
        old_schemas={s['name']:s for s in read_json(base/'trials'/row['id'].replace('-D-','-C-')/'tools.json')}
        new_schemas={s['name']:s for s in read_json(out/'trials'/row['id']/'tools.json')}
        schema_checks.append(set(new_schemas)-set(old_schemas)=={'lsp'} and all(new_schemas.get(k)==v for k,v in old_schemas.items()))
    result=dict(summaries=summaries,paired=paired,secondary_audit=audits,lsp_calls=calls,lsp_statuses=dict(statuses),
        only_tool_schema_difference_lsp=all(schema_checks),
        agent_requested_lsp_calls=sum(x['model_tool_requests'].get('lsp',0) for x in observed),
        actual_diagnostic_tasks=len({c['trial'] for c in calls if c.get('result',{}).get('status')=='ok'}),
        observed=observed,all_processes_released=all(r['processes_released'] for r in new),
        chat_usage_complete=all(r['trace']['usage_complete'] for r in new),
        embedding_unknown=sum(r['event']=='end' and r.get('input_tokens') is None for r in ledger),
        original_results_and_indexes_preserved=preserved,
        caveat='Historical comparison with C, no contemporaneous interleaving. Real installed LSPs, on-demand cold start. Source-only corpus lacks complete dependency environments. No causal/generalized improvement claim.')
    write_json(out/'priced-results.json',new);write_json(out/'report.json',result)
    print('LSP comparison report saved',flush=True)


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8',errors='replace')
    p=argparse.ArgumentParser();p.add_argument('operation',choices=['prepare','run','summarize']);p.add_argument('--base',type=Path,required=True)
    a=p.parse_args();globals()[a.operation](a.base.resolve())
