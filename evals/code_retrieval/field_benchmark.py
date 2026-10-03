"""Bounded real-provider A/B/C retrieval experiment with sealed labels and raw evidence.

No LLM judges, no query rewriting, no execution of corpus source. A is a text
window baseline, B syntax lexical, C the same syntax engine plus real vectors.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import platform
import random
import statistics
import time
import sys

from codeagent.code_search.embedding import RemoteEmbeddingProvider
from codeagent.code_search.vector import VectorIndex
from codeagent.code_intelligence.models import QueryContext, source_text
from codeagent.config import _load_dotenv, embedding_config_from_env
from codeagent.runtime.execution import ExecutionStopped
from codeagent.tools.search_code import SearchCodeTool
from evals.evidence import write_json, read_json, file_hash, snapshot

SEED = 20261003


def append(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + '\n')


def corpus_hashes(root):
    return {p.relative_to(root).as_posix(): file_hash(p) for p in sorted(root.rglob('*'))
            if p.is_file() and '.git' not in p.relative_to(root).parts}


def freeze(base):
    sources = read_json(base / 'public-sources.json') + read_json(base / 'repository-sources.json')
    for source in sources:
        source['hashes'] = corpus_hashes(Path(source['root']))
        if not source['hashes']: raise ValueError('Empty corpus')
    engine = snapshot(Path.cwd(), base / 'frozen-engine')
    specification = dict(seed=SEED, repeats=3, top_k=5, sources=sources, engine=engine['sha256'],
        environment=dict(python=sys.version, platform=platform.platform()),
        labels='External paired docstrings + source-reviewed assistant-authored labels, not independent human review',
        limits=dict(http_requests=800, input_characters=6000000),
        protocol='No tuning on this set. Failures retained. No rewrite. Prebuilt C; build cost separate. Interleaved arms.',
        arms={'A':'text_windows_lexical','B':'syntax_lexical','C':'syntax_real_embedding'})
    write_json(base / 'suite.json', specification)
    write_json(base / 'suite-seal.json', {'sha256': file_hash(base / 'suite.json')})
    return specification


def verify(base):
    if file_hash(base / 'suite.json') != read_json(base / 'suite-seal.json')['sha256']:
        raise ValueError('Labels or manifest changed after freeze')
    suite = read_json(base / 'suite.json')
    for source in suite['sources']:
        if corpus_hashes(Path(source['root'])) != source['hashes']:
            raise ValueError('Corpus changed after freeze: ' + source['id'])
    return suite


class MeteredProvider(RemoteEmbeddingProvider):
    """Actual HTTP sub-batches, including failed attempts; no keys or bodies logged."""
    def __init__(self, config, ledger, *, max_requests, max_characters):
        super().__init__(config)
        self.ledger, self.phase = ledger, 'initial'
        self.max_requests, self.max_characters = max_requests, max_characters
        previous = [json.loads(line) for line in ledger.read_text(encoding='utf-8').splitlines()] if ledger.exists() else []
        starts = [r for r in previous if r['event'] == 'start']
        self.requests, self.characters = len(starts), sum(r['characters'] for r in starts)

    async def _embed_batch(self, texts, check, timeout):
        size = sum(len(text) for text in texts)
        if self.requests >= self.max_requests or self.characters + size > self.max_characters:
            raise ExecutionStopped('benchmark_provider_budget_exhausted')
        self.requests += 1
        self.characters += size
        identifier = self.requests
        append(self.ledger, dict(event='start', id=identifier, phase=self.phase, characters=size, texts=len(texts)))
        started = time.monotonic()
        try:
            values = await super()._embed_batch(texts, check, timeout)
            append(self.ledger, dict(event='end', id=identifier, phase=self.phase, status='ok',
                input_tokens=values.usage.get('input_tokens'), duration_ms=(time.monotonic()-started)*1000))
            return values
        except BaseException as exc:
            append(self.ledger, dict(event='end', id=identifier, phase=self.phase, status='failed',
                error_type=type(exc).__name__, http_status=getattr(getattr(exc,'response',None),'status_code',None),
                duration_ms=(time.monotonic()-started)*1000))
            raise


def span(item):
    return item.get('line', item.get('start_line', 0)), item.get('end_line', 0)


def matches(item, target, *, strict=False):
    start, end = span(item)
    if item.get('path') != target['path'] or start > target['end'] or end < target['start']:
        return False
    return not strict or not target['anchors'] or any(start <= n <= end for n in target['anchors'])


def valid_quote(root, item):
    try:
        path = (root / item['path']).resolve()
        if not path.is_relative_to(root.resolve()): return False
        raw = path.read_bytes()
        lines = source_text(raw, item['path']).splitlines()
        start, end = span(item)
        return (1 <= start <= end <= len(lines) and end-start < 20
                and item['quote'] == '\n'.join(lines[start-1:end])
                and hashlib.sha256(raw).hexdigest() == item['content_hash'])
    except (OSError, ValueError, KeyError, UnicodeError):
        return False


def score(case, payload, candidates, root):
    results = payload.get('results', [])[:5]
    targets = case['targets']
    validity = [valid_quote(root,item) for item in results]
    identities = [[i for i,t in enumerate(targets) if matches(item,t)] for item in results]
    strict = [[i for i,t in enumerate(targets) if valid and matches(item,t,strict=True)] for item,valid in zip(results,validity)]
    ranks, covered = [], set()
    for rank,found in enumerate(identities,1):
        if set(found)-covered: ranks.append(rank)
        covered.update(found)
    strict_covered = set(i for found in strict for i in found)
    candidate_covered = {i for i,t in enumerate(targets) if any(matches(d,t) for d in candidates[:80])}
    vector_only = {i for i,t in enumerate(targets) if any(matches(d,t) and 'vector' in d.get('route_ranks',{}) and 'bm25' not in d.get('route_ranks',{}) and 'exact' not in d.get('route_ranks',{}) for d in candidates)}
    ideal = sum(1/math.log2(i+1) for i in range(1,min(5,len(targets))+1))
    return dict(positive=bool(targets), hit1=int(bool(identities and identities[0])), hit5=int(bool(covered)),
        mrr5=1/ranks[0] if ranks else 0., ndcg5=sum(1/math.log2(r+1) for r in ranks)/ideal if ideal else None,
        strict_hit5=int(bool(strict_covered)), complete5=int(bool(targets) and len(covered)==len(targets)),
        strict_complete5=int(bool(targets) and len(strict_covered)==len(targets)),
        recall5=len(covered)/len(targets) if targets else None,
        candidate_recall80=len(candidate_covered)/len(targets) if targets else None,
        vector_only_gold=len(vector_only), returned=len(results), source_valid=sum(validity),
        absent_nonempty=bool(not targets and results), absence_proven=payload.get('absence_proven',False),
        duplicate_results=len(results)-len({r.get('entity_id',(r.get('path'),r.get('symbol'))) for r in results}))


def run(base):
    suite = verify(base)
    output = base / 'run-v1'
    output.mkdir(exist_ok=False)
    _load_dotenv()
    config = embedding_config_from_env()
    if not config.enabled: raise ValueError('Real embedding not enabled')
    provider = MeteredProvider(config, output/'provider-ledger.jsonl',
        max_requests=suite['limits']['http_requests'],max_characters=suite['limits']['input_characters'])
    write_json(output/'provider.json',dict(model=provider.model,dimensions=provider.dimensions,
        endpoint_hash=hashlib.sha256(config.base_url.encode()).hexdigest(), pricing='not_assumed'))
    for source in suite['sources']:
        root = Path(source['root'])
        tools = {}
        for arm in ('A','B','C'):
            tool = SearchCodeTool(root,output/'indexes'/source['id']/arm,embedding_provider=provider if arm=='C' else None)
            if arm=='A':tool.service.index.analysis_mode='text'
            tools[arm]=tool
            started=time.monotonic()
            tool.service.index.sync(lambda:None)
            tool.service.index.close_snapshot()
            append(output/'builds.jsonl',dict(source=source['id'],arm=arm,phase='lexical',duration_ms=(time.monotonic()-started)*1000,coverage=tool.service.index.coverage))
        provider.phase=source['id']+':cold_build'
        tool=tools['C']; before=provider.requests;start=time.monotonic()
        vector=VectorIndex(tool.service.index,tool.service.embedding_provider,max_chunks=20000)
        deadline=time.monotonic()+1800
        context=QueryContext(remaining_seconds=lambda:deadline-time.monotonic())
        try:
            vector.build('.',context.check,context.remaining_seconds)
            build_status='completed' if vector.stats['pending']==0 else 'partial'
        except ExecutionStopped:raise
        except Exception as exc:
            build_status=type(exc).__name__
        append(output/'builds.jsonl',dict(source=source['id'],arm='C',phase='embedding',status=build_status,
            duration_ms=(time.monotonic()-start)*1000,http_requests=provider.requests-before,usage=vector.stats))
        # Freeze warm retrieval: no automatic document builds in the quality phase.
        tool.service.max_vector_chunks=0
        print(source['id'],'built',build_status,vector.stats,flush=True)
        cases=list(source['cases']);random.Random(SEED).shuffle(cases)
        for repeat in range(suite['repeats']):
            for case_index,case in enumerate(cases):
                order=list('ABC');random.Random(SEED+case_index+repeat*100).shuffle(order)
                for arm in order:
                    tool=tools[arm];captured=[];original=tool.service.candidates
                    def observe(*args,**kwargs):
                        value=original(*args,**kwargs)
                        captured[:]=value[0]
                        return value
                    tool.service.candidates=observe
                    provider.phase=source['id']+':query:'+case['id']+':'+str(repeat+1)
                    began=time.monotonic();payload={};error=None
                    try:
                        payload=json.loads(tool.run(case['query'],top_k=5))
                    except ExecutionStopped:raise
                    except Exception as exc:error=type(exc).__name__
                    finally:tool.service.candidates=original
                    candidates=[{k:d.get(k) for k in ('path','symbol','start_line','end_line','route_ranks','parse_quality')} for d in captured]
                    record=dict(source=source['id'],kind=source['kind'],language=source['language'],case_id=case['id'],
                        category=case['category'],repeat=repeat+1,arm=arm,error=error,
                        duration_ms=(time.monotonic()-began)*1000,payload=payload,candidates=candidates,
                        metrics=score(case,payload,candidates,root))
                    append(output/'queries.jsonl',record)
            print(source['id'],'repeat',repeat+1,'complete',flush=True)
    verify(base)
    summarize(base)


def quantile(values,p):
    values=sorted(values)
    return values[min(len(values)-1,max(0,math.ceil(len(values)*p)-1))] if values else None


def summarize(base):
    suite=verify(base);output=base/'run-v1'
    rows=[json.loads(line) for line in (output/'queries.jsonl').read_text(encoding='utf-8').splitlines()]
    groups=defaultdict(list)
    for row in rows:
        for key in [row['source'],row['kind'],'category:'+row['category']]:groups[(key,row['arm'])].append(row)
    summaries=[]
    for (key,arm),items in sorted(groups.items()):
        positive=[r for r in items if r['metrics']['positive']]
        metrics={m:statistics.mean(r['metrics'][m] for r in positive) if positive else None
                 for m in ('hit1','hit5','mrr5','ndcg5','strict_hit5','complete5','strict_complete5','recall5','candidate_recall80')}
        summaries.append(dict(group=key,arm=arm,trials=len(items),questions=len({r['case_id'] for r in items}),
            positive_questions=len({r['case_id'] for r in positive}),metrics=metrics,
            p50_ms=quantile([r['duration_ms'] for r in items],.5),p95_ms=quantile([r['duration_ms'] for r in items],.95),
            errors=sum(bool(r['error']) for r in items),backends=dict(Counter(r['payload'].get('vector_status','error') for r in items)),
            source_valid=sum(r['metrics']['source_valid'] for r in items),returned=sum(r['metrics']['returned'] for r in items),
            absent_nonempty=sum(r['metrics']['absent_nonempty'] for r in items)))
    comparisons=[]
    for kind in ('public_proxy_subset','repository_authored'):
        selected=[r for r in rows if r['kind']==kind and r['metrics']['positive']]
        by_case=defaultdict(lambda:defaultdict(list))
        for r in selected:by_case[r['case_id']][r['arm']].append(r)
        for a,b in [('A','B'),('B','C')]:
            for metric in ('hit5','strict_hit5','mrr5','complete5'):
                values={id:statistics.mean(r['metrics'][metric] for r in data[b])-statistics.mean(r['metrics'][metric] for r in data[a]) for id,data in by_case.items() if a in data and b in data}
                # Correlated paraphrases / shared targets are resampled together.
                cluster=defaultdict(list)
                lookup={c['id']:(s,c) for s in suite['sources'] for c in s['cases']}
                for id,value in values.items():
                    source,case=lookup[id]
                    label=(source['id'],case.get('repository') or tuple(sorted({t['path'] for t in case['targets']})))
                    cluster[str(label)].append(value)
                clusters=list(cluster.values());rng=random.Random(SEED);samples=[]
                for _ in range(2000):
                    sample=[v for _ in clusters for v in rng.choice(clusters)]
                    samples.append(statistics.mean(sample))
                comparisons.append(dict(kind=kind,before=a,after=b,metric=metric,questions=len(values),clusters=len(clusters),
                    delta=statistics.mean(values.values()),ci95=[quantile(samples,.025),quantile(samples,.975)],
                    wins=sum(v>0 for v in values.values()),losses=sum(v<0 for v in values.values()),ties=sum(v==0 for v in values.values())))
    ledger=[json.loads(line) for line in (output/'provider-ledger.jsonl').read_text(encoding='utf-8').splitlines()]
    starts=[r for r in ledger if r['event']=='start'];ends=[r for r in ledger if r['event']=='end']
    usage=dict(requests=len(starts),successful=sum(r['status']=='ok' for r in ends),
        input_characters=sum(r['characters'] for r in starts),known_input_tokens=sum(r.get('input_tokens') or 0 for r in ends),
        complete=len(starts)==len(ends) and all(r.get('input_tokens') is not None for r in ends),cost_cny=None,
        cost_reason='No verified applicable embedding unit price; do not apply chat token prices')
    write_json(output/'report.json',dict(summaries=summaries,paired=comparisons,provider_usage=usage,
        expected_trials=sum(len(s['cases']) for s in suite['sources'])*3*3,actual_trials=len(rows),
        limitation='Derived public subset with docstring proxy labels; authored repository labels not independently reviewed. Retrieval does not classify absence. No official benchmark score.'))
    print('Report:',output/'report.json',usage,flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('operation',choices=['freeze','run','report'])
    parser.add_argument('--base',type=Path,required=True)
    args=parser.parse_args()
    {'freeze':freeze,'run':run,'report':summarize}[args.operation](args.base.resolve())
