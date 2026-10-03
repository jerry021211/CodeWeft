"""Freeze manually annotated multilingual corpora independently of production parsers.

Annotations contain documents (id,path,symbol,start_line,end_line, optional
signature) and cases in the existing gold schema. This deliberately does not
generate relevance labels from the search implementation being evaluated.
"""
from collections import Counter
from pathlib import Path
import shutil
from codeagent.code_intelligence.languages import language_for
from codeagent.code_intelligence.models import source_text
from codeagent.tools.search_files import search_files
from codeagent.tools.workspace import WorkspaceGuard
from evals.evidence import file_hash, write_json, seal, snapshot
from .dataset import digest, read_json, write_jsonl
from . import VERSION


def prepare_polyglot(source, output, annotations, *, engine=None):
    source, output = source.resolve(), output.resolve()
    guard = WorkspaceGuard(source)
    inventory = search_files(source, guard)
    if inventory.incomplete:
        raise ValueError('Cannot freeze an incomplete source inventory')
    files = [p for p in inventory.paths if language_for(p)]
    allowed = {p.relative_to(source).as_posix(): p for p in files}
    specification = read_json(annotations)
    docs = []
    for item in specification['documents']:
        path = item['path']
        if path not in allowed:
            raise ValueError('Annotation path is ignored, unsupported or outside workspace: ' + path)
        lines = source_text(allowed[path].read_bytes(), path).splitlines()
        start, end = item['start_line'], item['end_line']
        if type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(lines):
            raise ValueError('Invalid annotated source span')
        docs.append({**item, '_id': item['id'], 'text': '\n'.join(lines[start - 1:end]),
                     'source_sha256': file_hash(allowed[path]), 'language': language_for(path).name})
    by_id = {d['_id']: d for d in docs}
    if len(by_id) != len(docs):
        raise ValueError('Duplicate annotated document id')
    cases = specification['cases']
    if len({c['id'] for c in cases}) != len(cases):
        raise ValueError('Duplicate case id')
    for case in cases:
        for group in case['groups']:
            for member in group['members']:
                doc = by_id.get(member['doc_id'])
                if doc is None or not member['evidence_lines'] or any(
                        not doc['start_line'] <= line <= doc['end_line'] for line in member['evidence_lines']):
                    raise ValueError('Invalid gold evidence anchor')
    output.mkdir(parents=True, exist_ok=False)
    target = output / 'target'
    for relative, path in allowed.items():
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    if (source / '.gitignore').is_file():
        shutil.copyfile(source / '.gitignore', target / '.gitignore')
    public = [{'_id': c['id'], 'text': c['query'], 'split': c['split'], 'category': c['category']} for c in cases]
    write_jsonl(output / 'corpus.jsonl', docs)
    write_jsonl(output / 'queries.jsonl', public)
    write_json(output / 'gold.json', {'version': VERSION, 'cases': cases})
    hashes = {p.relative_to(target).as_posix(): file_hash(p) for p in target.rglob('*') if p.is_file()}
    engine_snapshot = snapshot((engine or Path.cwd()).resolve(), output / 'baseline-engine')
    write_json(output / 'manifest.json', {'version': VERSION, 'source_root': str(source),
        'corpus_scope': 'explicit language registry, workspace-safe inventory', 'target_hashes': hashes,
        'corpus_hash': digest(hashes), 'queries_hash': digest(public), 'gold_hash': digest(cases),
        'baseline_engine': engine_snapshot, 'case_counts': dict(Counter(c['split'] for c in cases)),
        'annotation_status': specification.get('annotation_status', 'external_labels_not_independently_reviewed')})
    seal(output)
    return output
