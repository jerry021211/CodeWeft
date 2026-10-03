"""Route fusion preserves matched evidence; diversity is applied at presentation."""
import re
from codeagent.memory.retrieval import terms


def exact_symbols(documents, query, in_scope):
    names = set(re.findall(r'[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*', query))
    declaration = bool(re.search(r'\b(class|field|interface|property|type|constant)\b|字段|属性|类定义|接口定义|常量', query, re.I))
    def explicit(doc):
        symbol = doc['symbol']
        return (doc['kind'] in ('function', 'method', 'constructor') or declaration
                or query.strip(' `()') in (symbol, symbol.rsplit('.', 1)[-1])
                or ('.' in symbol and symbol in names)
                or bool(re.search(r'[a-z][A-Z]|_', symbol.rsplit('.', 1)[-1])))
    return [d for _, d in documents if d['kind'] not in ('module', 'text', 'parse_fallback')
            and explicit(d) and in_scope(d['path']) and (d['symbol'] in names or d['symbol'].rsplit('.', 1)[-1] in names)]


def content_prior(path, query):
    lower = path.lower()
    artifact = (bool(re.search(r'(^|/)(eval-results|coverage|dist|build)(/|$)|(^|/)evals/results/', lower))
                or lower.endswith(('.min.js', '.map', 'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml')))
    return .1 if artifact and not re.search(r'benchmark|evaluation|report|评测|报告|构建产物|lockfile|锁文件', query, re.I) else 1.


def fuse(query, queries, exact, routes, vector_route, symbol_intent):
    wanted = set(terms(' '.join(queries)))
    scores, best = {}, {}
    weighted = [('exact', exact, 2.), *[('bm25', r, 1.2 if symbol_intent else 1.) for r in routes],
                ('vector', vector_route, .7 if symbol_intent else 1.4)]
    provenance = {}
    for origin, route, weight in weighted:
        seen = set()
        for rank, doc in enumerate(route, 1):
            key = doc['parent']
            quality = weight / (60 + rank)
            if origin == 'exact':
                quality += .1 if doc['start_line'] <= doc['definition_start_line'] <= doc['end_line'] else 0
            if origin == 'bm25':
                quality += len(wanted & set(terms(doc['body']))) * .02
            if origin == 'vector' and not symbol_intent:
                quality += .1 + doc.get('vector_score', 0)
            if key not in best or quality > best[key][0]:
                best[key] = (quality, dict(doc, evidence_origin=origin))
            provenance.setdefault(key, {})[origin] = min(rank, provenance.get(key, {}).get(origin, rank))
            if key not in seen:
                scores[key] = scores.get(key, 0.) + weight / (60 + rank)
                seen.add(key)
    test_intent = bool(re.search(r'test|spec|fixture|mock|测试|用例', query, re.I))
    doc_intent = bool(re.search(r'document|readme|文档|说明', query, re.I))
    declaration_intent = bool(re.search(r'\b(class|field|interface|property|type|constant)\b|字段|属性|接口|类型|常量|类定义', query, re.I))
    for key in scores:
        path = best[key][1]['path'].lower()
        scores[key] *= content_prior(path, query)
        if not declaration_intent and best[key][1]['kind'] in ('field', 'class', 'variable', 'interface', 'type'):
            scores[key] *= .7
        if not test_intent and re.search(r'(^|/)(tests?|fixtures?|__tests__)(/|_)|\.(test|spec)\.', path):
            scores[key] *= .8
        if not doc_intent and (path.startswith('docs/') or path.endswith('.md')):
            scores[key] *= .75
    exact_keys = {d['parent'] for d in exact}
    # A fully qualified match outranks a suffix-only match (e.g. the function
    # hash_schema versus State.hash_schema). Same-name overloads remain separate.
    names = set(re.findall(r'[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*', query))
    full_keys = {d['parent'] for d in exact if d['symbol'] in names}
    keys = sorted(scores, key=lambda k: (k not in full_keys, k not in exact_keys, -scores[k], k))
    return [dict(best[k][1], route_ranks=provenance[k]) for k in keys[:80]]
