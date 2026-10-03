"""Recompute final assessment from retained evidence without network requests."""
import argparse
from collections import Counter, defaultdict
from pathlib import Path
import statistics

from evals.evidence import file_hash, read_json, write_json
from .field_agent import records
from .field_benchmark import quantile, verify


def run(base):
    suite = verify(base)
    rows = records(base / 'run-v1/queries.jsonl')
    agents = records(base / 'agent-retrieval-v1/results.jsonl')
    edits = records(base / 'agent-edits-v2/results.jsonl')
    lookup = {c['id']: c for s in suite['sources'] for c in s['cases']}
    groups = defaultdict(list)
    for row in rows:
        groups[row['arm'], row['category'] == 'exact'].append(row)
    performance = []
    for (arm, exact), group in groups.items():
        performance.append(dict(arm=arm, exact=exact, trials=len(group),
            p50_ms=quantile([r['duration_ms'] for r in group], .5),
            p95_ms=quantile([r['duration_ms'] for r in group], .95)))
    repeats = defaultdict(list)
    for row in rows:
        repeats[row['case_id'], row['arm']].append(row['metrics'])
    agent_groups = []
    for arm in ('B', 'C'):
        group = [r for r in agents if r['arm'] == arm]
        positive = [r for r in group if lookup[r['case_id']]['targets']]
        negative = [r for r in group if not lookup[r['case_id']]['targets']]
        agent_groups.append(dict(arm=arm, trials=len(group), positives=len(positive), negatives=len(negative),
            strict_successes=sum(r.get('metrics', {}).get('strict_complete5', 0) for r in positive),
            correct_abstentions=sum(r.get('correct_abstention', False) for r in negative),
            false_positives=sum(r.get('false_positive', False) for r in negative),
            errors=sum(r.get('outcome') == 'error' for r in group),
            model_requests=sum(r['trace']['model_requests'] for r in group),
            tool_calls=sum(r['trace']['tool_calls'] for r in group),
            mean_duration_ms=statistics.mean(r['duration_ms'] for r in group) if group else None))
    ledger = records(base / 'run-v1/provider-ledger.jsonl')
    phases = defaultdict(list)
    for row in ledger:
        phases['cold_build' if row['phase'].endswith(':cold_build') else
               'agent' if row['phase'].startswith('agent:') else
               'integrity' if row['phase'].startswith('integrity:') else 'query'].append(row)
    embedding = []
    for phase, group in phases.items():
        starts = [r for r in group if r['event'] == 'start']
        ends = [r for r in group if r['event'] == 'end']
        embedding.append(dict(phase=phase, requests=len(starts),
            tokens=sum(r.get('input_tokens') or 0 for r in ends),
            complete=len(starts) == len(ends) and all(r.get('input_tokens') is not None for r in ends),
            failures=sum(r['status'] != 'ok' for r in ends)))
    responses = [r for folder in ('agent-retrieval-v1', 'agent-edits-v1', 'agent-edits-v2')
                 for p in (base / folder).glob('*/model-responses.jsonl')
                 for r in records(p) if r.get('provider_response_id') != 'controlled-fault-injection']
    usage = {k: sum((r.get('usage') or {}).get(k) or 0 for r in responses) for k in
             ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')}
    total_input = usage['input_tokens'] + usage['cache_read_input_tokens'] + usage['cache_creation_input_tokens']
    prices = read_json(base / 'pricing-evidence.json')
    ep, cp = prices['embedding'], prices['chat']
    # Probe is recorded separately; do not lose it in the experiment subtotal.
    probe = read_json(base / 'provider-probe.json')
    probe_tokens = probe.get('input_tokens', probe.get('usage', {}).get('input_tokens', 0))
    embed_tokens = sum(r['tokens'] for r in embedding) + probe_tokens
    embed_cost = embed_tokens * ep['input_per_million_tokens'] / 1_000_000
    chat_cost = ((usage['input_tokens'] + usage['cache_creation_input_tokens']) * cp['cache_miss_input_per_million_tokens']
                 + usage['cache_read_input_tokens'] * cp['cache_hit_input_per_million_tokens']
                 + usage['output_tokens'] * cp['output_per_million_tokens']) / 1_000_000
    frozen = base / 'frozen-engine'
    unchanged = all(file_hash(p) == file_hash(Path.cwd() / p.relative_to(frozen))
                    for p in (frozen / 'codeagent').rglob('*.py'))
    result = dict(trials=len(rows), expected_trials=918, production_unchanged=unchanged,
        scan_complete=sum(r['payload'].get('scan_complete') is True for r in rows),
        source_changed=sum(bool(r['payload'].get('source_changed')) for r in rows),
        returned=sum(r['metrics']['returned'] for r in rows),
        valid_quotes=sum(r['metrics']['source_valid'] for r in rows),
        duplicate_results=sum(r['metrics']['duplicate_results'] for r in rows),
        repeat_groups=len(repeats), unstable_quality_groups=sum(any(m != group[0] for m in group) for group in repeats.values()),
        performance=performance, agent_groups=agent_groups,
        missing_evidence=[dict(case_id=r['case_id'], query=lookup[r['case_id']]['query'], metrics=r['metrics'])
            for r in rows if r['repeat']==1 and r['arm']=='C' and r['metrics']['positive'] and not r['metrics']['strict_complete5']],
        vector_only_gold_questions=sum(r['repeat']==1 and r['arm']=='C' and r['metrics']['vector_only_gold'] > 0 for r in rows),
        editing=[{k:r[k] for k in ('language','arm','outcome','verification','server_started','processes_reaped','io_threads_stopped','duration_ms','trace')} for r in edits],
        embedding_phases=embedding, chat_usage=usage, chat_actual_requests=len(responses),
        chat_actual_models=dict(Counter(r.get('model', 'error') for r in responses)),
        chat_usage_scope='Known usage from successful responses only; failed HTTP requests have no usage and are retained separately.',
        chat_usage_complete=bool(responses) and all(isinstance(r.get('usage'), dict) for r in responses),
        chat_cache_input_ratio=usage['cache_read_input_tokens']/total_input if total_input else None,
        estimated_list_cost_cny=dict(embedding=embed_cost, chat=chat_cost, total=embed_cost+chat_cost,
            embedding_probe_tokens=probe_tokens, note='Standard list estimate; not account bill. Missing usage would make this a known subtotal.'),
        integrity=read_json(base/'integrity-v1/report.json'),
        project_lsp=read_json(base/'real-project-lsp-v1/report.json'),
        maven_followup=read_json(base/'petclinic-maven-v2/report.json') if (base/'petclinic-maven-v2/report.json').exists() else None,
        focused_tests=read_json(base/'focused-tests-v2.json') if (base/'focused-tests-v2.json').exists() else None,
        real_lsp_tests=read_json(base/'real-lsp-tests.json') if (base/'real-lsp-tests.json').exists() else None)
    write_json(base/'assessment.json', result)
    print('Assessment saved:', base/'assessment.json')


if __name__ == '__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--base', type=Path, required=True)
    run(parser.parse_args().base.resolve())
