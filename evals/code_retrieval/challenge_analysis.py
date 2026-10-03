"""Read-only audit and cost breakdown for the three-arm Agent experiment."""
import argparse
from collections import Counter
import json
from pathlib import Path
import statistics
import random


def read(path):return json.loads(path.read_text(encoding='utf-8'))
def lines(path):return [json.loads(s) for s in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []


def embedded_answer(text):
    """Secondary audit only: extract a JSON object without correcting its evidence."""
    decoder=json.JSONDecoder()
    for index,char in enumerate(text):
        if char!='{':continue
        try:value,_=decoder.raw_decode(text[index:])
        except ValueError:continue
        if isinstance(value,dict) and value.get('status') in ('found','not_found','insufficient_evidence') and isinstance(value.get('results'),list):
            return value
    return None


def evidence_audit(base,rows,m):
    from .challenge import complete_evidence
    from .field_benchmark import valid_quote
    from evals.evidence import file_hash
    cases={c['id']:c for c in m['cases']};sources={s['id']:Path(s['root']) for s in m['sources']};out=[]
    for row in rows:
        if row['kind']=='repair':
            out.append(dict(id=row['id'],arm=row['arm'],primary_success=row['success'],secondary_success=row['success'],repair=True));continue
        answer=embedded_answer(row.get('final_text',''));case=cases[row['case_id']];root=sources[row['source']]
        evidence=answer.get('results',[]) if answer else []
        try:
            for entry in evidence:
                path=(root/entry.get('path','')).resolve()
                if path.is_relative_to(root.resolve()) and path.is_file():entry['content_hash']=file_hash(path)
            success=bool(answer and answer['status']=='not_found' and not evidence) if row['kind']=='absent' else bool(answer and answer['status']=='found' and complete_evidence(case,evidence,root))
            authentic=sum(valid_quote(root,e) for e in evidence[:5])
        except (TypeError,ValueError,KeyError):success=False;authentic=None
        out.append(dict(id=row['id'],arm=row['arm'],primary_success=row['success'],secondary_success=success and not row['changed_files'],
            extracted_json=answer is not None,returned=len(evidence),authentic=authentic))
    return out


def analyze(base,prefix=''):
    report=read(base/(prefix+'report.json'));rows=read(base/(prefix+'priced-results.json'));m=read(base/'manifest.json')
    ledger=lines(base/'embedding-ledger.jsonl');starts={r['id']:r for r in ledger if r['event']=='start'}
    ends={r['id']:r for r in ledger if r['event']=='end'}
    cold_cost=report['cold_embedding_cost_cny'];arms=[];schema_checks=[];failed=[]
    for arm in 'ABC':
        group=[r for r in rows if r['arm']==arm];events=[];search_runs=0;vector_runs=0
        for row in group:
            trial=base/'trials'/row['id'];current=lines(trial/'events.jsonl');events.extend(current)
            schemas=read(trial/'tools.json');names={s['name'] for s in schemas}
            schema_checks.append(dict(id=row['id'],search_present='search_code' in names,
                correct=('search_code' in names)==(arm!='A'),lsp_absent='lsp' not in names))
            searches=[r['payload'] for r in current if r['type']=='code_search.completed']
            search_runs+=bool(searches);vector_runs+=any(r.get('retrieval_backend')=='hybrid' for r in searches)
            if not row['success']:
                if row['kind']=='repair':reason='hidden_contract_or_scope'
                elif not row.get('answer'):reason='final_answer_not_parseable'
                elif row['kind']=='absent':reason='not_found_contract_not_satisfied'
                elif row.get('metrics',{}).get('source_valid',0)<row.get('metrics',{}).get('returned',0):reason='quote_or_line_mismatch'
                else:reason='missing_required_evidence_or_status'
                failed.append(dict(id=row['id'],reason=reason,outcome=row['outcome'],kind=row['kind']))
        main=next(r for r in report['summaries'] if r['arm']==arm and r['kind']=='all')
        u=main['usage'];n=len(group);successes=main['successes']
        total_input=u['input_tokens']+u['cache_read_input_tokens']+u['cache_creation_input_tokens']
        searches=[r['payload'] for r in events if r['type']=='code_search.completed']
        group_phases={prefix+r['id'] for r in group for prefix in ('agent:','setup:')}
        embedding_tokens=sum((ends.get(i,{}).get('input_tokens') or 0) for i,start in starts.items() if start['phase'] in group_phases)
        cold=cold_cost if arm=='C' else 0
        totals=main['total_cost_cny']+cold
        arms.append(dict(arm=arm,runs=n,successes=successes,mean_seconds=main['mean_seconds'],
            mean_seconds_including_trial_setup=main['mean_seconds_with_setup'],
            mean_success_seconds=statistics.mean(r['duration_ms']/1000 for r in group if r['success']) if successes else None,
            mean_tokens=main['mean_tokens'],mean_input_miss=(u['input_tokens']+u['cache_creation_input_tokens'])/n,
            mean_embedding_tokens=embedding_tokens/n,
            mean_input_hit=u['cache_read_input_tokens']/n,mean_output=u['output_tokens']/n,
            cache_input_ratio=u['cache_read_input_tokens']/total_input if total_input else None,
            mean_chat_cost_cny=main['mean_chat_cost_cny'],mean_warm_total_cost_cny=main['mean_total_cost_cny'],
            mean_cost_with_cold_amortized_cny=totals/n,cost_per_success_including_cold_cny=totals/successes if successes else None,
            total_chat_cost_cny=main['mean_chat_cost_cny']*n,total_warm_cost_cny=main['total_cost_cny'],total_with_cold_cny=totals,
            mean_calls=main['mean_calls'],search_using_runs=search_runs,hybrid_using_runs=vector_runs,
            search_backends=dict(Counter(r.get('retrieval_backend') for r in searches)),
            vector_statuses=dict(Counter(r.get('vector_status') for r in searches)),
            rewrite_calls=sum(r.get('rewrite_calls',0) for r in searches),
            search_incomplete=sum(not r.get('scan_complete',False) for r in searches),
            tool_calls=dict(Counter(r['payload'].get('name',r['payload'].get('tool_name','unknown')) for r in events if r['type']=='tool.started'))))
    cost_complete=all(r['trace']['usage_complete'] for r in rows) and len(starts)==len(ends) and all(r.get('input_tokens') is not None for r in ends.values())
    interrupted=[]
    for trial in (base/'interrupted-attempts').glob('*'):
        requests=lines(trial/'model-requests.jsonl');responses=lines(trial/'model-responses.jsonl')
        interrupted.append(dict(trial=trial.name,requests=len(requests),responses=len(responses),usage_unknown=len(requests)>len(responses)))
    cost_complete=cost_complete and not any(r['usage_unknown'] for r in interrupted)
    strata=[]
    for field in ('source','kind','repeat'):
        for value in sorted({r[field] for r in rows}):
            for arm in 'ABC':
                group=[r for r in rows if r[field]==value and r['arm']==arm]
                if group:
                    strata.append(dict(field=field,value=value,arm=arm,runs=len(group),
                        successes=sum(r['success'] for r in group),
                        mean_seconds=statistics.mean(r['duration_ms']/1000 for r in group),
                        mean_tokens=statistics.mean(r['tokens_total'] for r in group),
                        mean_warm_cost_cny=statistics.mean(r['total_cost_cny'] for r in group)))
    secondary=evidence_audit(base,rows,m)
    secondary_by={r['id']:r for r in secondary};secondary_pairs=[]
    for before,after in [('A','B'),('B','C'),('A','C')]:
        values=[]
        for case in m['cases']:
            differences=[]
            for repeat in (1,2):
                x=secondary_by.get(f"{case['id']}-{before}-{repeat}");y=secondary_by.get(f"{case['id']}-{after}-{repeat}")
                if x and y:differences.append(int(y['secondary_success'])-int(x['secondary_success']))
            if differences:values.append(statistics.mean(differences))
        if values:
            rng=random.Random(m['seed']);samples=sorted(statistics.mean(rng.choices(values,k=len(values))) for _ in range(2000))
            secondary_pairs.append(dict(before=before,after=after,delta=statistics.mean(values),ci95=[samples[50],samples[1949]],independent_tasks=len(values)))
    result=dict(arms=arms,strata=strata,failed_trials=failed,schema_checks=schema_checks,
        secondary_json_extraction_audit=secondary,
        secondary_successes={arm:sum(r['secondary_success'] for r in secondary if r['arm']==arm) for arm in 'ABC'},
        secondary_success_paired=secondary_pairs,
        interrupted_attempts=interrupted,
        reduced_plan_complete=set(r['id'] for r in lines(base/'results.jsonl'))==set((report.get('reduced_execution_plan') or {}).get('trial_ids',[])),
        complete_trial_set=len(rows)==len(m['schedule']),schema_correct=all(r['correct'] and r['lsp_absent'] for r in schema_checks),
        billing_complete=cost_complete,embedding_failed_or_unknown=[dict(id=i,phase=r['phase'],status=ends.get(i,{}).get('status','missing'),known_input_tokens=ends.get(i,{}).get('input_tokens')) for i,r in starts.items() if ends.get(i,{}).get('input_tokens') is None],
        abandoned_primary_build_cost_cny=report.get('abandoned_primary_embedding_cost_cny',0),
        readiness_probe_cost_cny=(report.get('embedding_model_amendment') or {}).get('readiness_probe_cost_cny',0),
        total_known_cost_cny=sum(a['total_with_cold_cny'] for a in arms)+report.get('abandoned_primary_embedding_cost_cny',0)+(report.get('embedding_model_amendment') or {}).get('readiness_probe_cost_cny',0),
        caveat='Known usage is priced at Flash off-peak rates. Timeouts without usage are unknown, not zero-cost. Cold build cost amortized over the selected C trials is an explicit scenario, not a universal per-task cost.')
    (base/(prefix+'analysis.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    render(base,result,report,prefix)
    print('Audit saved:',base/(prefix+'analysis.json'))


def render(base,a,r,prefix=''):
    out=['# Three-arm real Agent results','',
        'A: read/glob/grep. B: A + lexical search_code. C: B + qwen3.7-text-embedding-flash.',
        'Reduced from 108 to 60 trials at user request. First-repeat balanced cohort (54 trials) is the primary comparison; six later attempts are retained separately. Failed completed attempts remain in denominators.',
        '', '| Arm | Success | Agent seconds | Seconds incl. trial setup | Tokens | Chat CNY/task | Warm total CNY/task | With cold amortized CNY/task |',
        '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    for x in a['arms']:
        out.append(f"| {x['arm']} | {x['successes']}/{x['runs']} | {x['mean_seconds']:.2f} | {x['mean_seconds_including_trial_setup']:.2f} | {x['mean_tokens']:.0f} | {x['mean_chat_cost_cny']:.6f} | {x['mean_warm_total_cost_cny']:.6f} | {x['mean_cost_with_cold_amortized_cny']:.6f} |")
    out += ['', '## Token and tool audit','',
        '| Arm | Input miss/task | Input hit/task | Output/task | Input cache hit | Search-using trials | Hybrid-using trials |',
        '| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for x in a['arms']:
        out.append(f"| {x['arm']} | {x['mean_input_miss']:.0f} | {x['mean_input_hit']:.0f} | {x['mean_output']:.0f} | {100*(x['cache_input_ratio'] or 0):.1f}% | {x['search_using_runs']} | {x['hybrid_using_runs']} |")
    out += ['', '## Task strata','', '| Group | Value | Arm | Success | Seconds | Tokens | Warm CNY/task |', '| --- | --- | --- | ---: | ---: | ---: | ---: |']
    for x in a['strata']:
        out.append(f"| {x['field']} | {x['value']} | {x['arm']} | {x['successes']}/{x['runs']} | {x['mean_seconds']:.2f} | {x['mean_tokens']:.0f} | {x['mean_warm_cost_cny']:.6f} |")
    out += ['', '## Paired differences (after minus before)','', '| Pair | Metric | Mean delta | 95% task-cluster bootstrap interval |', '| --- | --- | ---: | --- |']
    for x in r['paired']:
        out.append(f"| {x['before']} -> {x['after']} | {x['metric']} | {x['delta']:.6f} | [{x['ci95'][0]:.6f}, {x['ci95'][1]:.6f}] |")
    out += ['', '## Accounting and limitations','',
        f"- Secondary JSON extraction audit successes (same source anchors, no evidence correction): {a['secondary_successes']}. This post-hoc diagnostic does not replace the frozen primary score.",
        f"- Complete trial set: {a['complete_trial_set']}; tool schema audit: {a['schema_correct']}.",
        f"- Reduced plan complete: {a['reduced_plan_complete']}; interrupted unpriced attempts: {a['interrupted_attempts']}.",
        f"- Flash cold build known cost: CNY {r['cold_embedding_cost_cny']:.6f}.",
        f"- Abandoned original-model build known cost: CNY {a['abandoned_primary_build_cost_cny']:.6f}; retained separately.",
        f"- Total known standardized cost: CNY {a['total_known_cost_cny']:.6f}; billing complete: {a['billing_complete']}.",
        f"- Embedding requests without usage: {len(a['embedding_failed_or_unknown'])}; not assumed free.",
        '- Prices: DeepSeek Flash off-peak CNY 1/M input miss, 0.02/M cache hit, 4/M output; Beijing synchronous embedding primary 0.5/M, Flash 0.125/M.',
        '- Agent wall time excludes external hidden verifiers and cold construction. Trial setup and cold costs are separately reported.',
        '- Only 18 independent tasks on 3 previously inspected repositories. Artificial repairs; no claim of SWE-bench or production-wide success.',
        '- Java contract checks compile real domain classes with dependency stubs; TypeScript uses real transpiled modules, not full-project type checking.',
        '- Original cold-build attempts had timeouts and incomplete outer timing. This is a warm-index Agent comparison, not proof of default cold-start reliability.',
        '- Cloud cache cannot be cleared by the harness. Balanced ordering reduces but does not remove cache and service-load confounding.',
        '- Trial J1-C-1 used recovered agent-event timing after a concurrent JSONL write fragment; original log retained, model calls not repeated. Subsequent trace writes were serialized.',
        '', '## Failed attempts','']
    out += [f"- {x['id']}: {x['reason']} ({x['outcome']})" for x in a['failed_trials']]
    (base/(prefix+'results-summary.md')).write_text('\n'.join(out)+'\n',encoding='utf-8')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--base',type=Path,required=True)
    analyze(p.parse_args().base.resolve())
