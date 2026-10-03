"""Deterministic external test sample; never executes downloaded source code."""
from __future__ import annotations
import ast
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import random
import subprocess
import textwrap
import tokenize
import httpx
from evals.evidence import write_json, file_hash

SEED = 20261003
DATASET = 'code-search-net/code_search_net'


def strip_documentation(code, language):
    """Blank comments/docstrings while preserving lines and executable literals."""
    code = textwrap.dedent(code)
    lines = code.splitlines(keepends=True)
    if language == 'python':
        tree = ast.parse(code)
        spans = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str):
                    value = node.body[0]
                    spans.append((value.lineno, value.end_lineno))
        for start, end in spans:
            for index in range(start - 1, end):
                lines[index] = '\n' if lines[index].endswith('\n') else ''
        code = ''.join(lines)
        # Token offsets are character offsets, unlike AST byte columns.
        comments = [t for t in tokenize.generate_tokens(io.StringIO(code).readline) if t.type == tokenize.COMMENT]
        lines = code.splitlines(keepends=True)
        for token in reversed(comments):
            row, col = token.start
            end = token.end[1]
            lines[row - 1] = lines[row - 1][:col] + ' ' * (end - col) + lines[row - 1][end:]
        return ''.join(lines)
    from tree_sitter import Language, Parser
    import importlib
    module = importlib.import_module('tree_sitter_' + language)
    raw = code.encode('utf-8')
    root = Parser(Language(module.language())).parse(raw).root_node
    pending, spans = [root], []
    while pending:
        node = pending.pop()
        if node.type in ('comment', 'line_comment', 'block_comment'):
            spans.append((node.start_byte, node.end_byte))
        else:
            pending.extend(node.named_children)
    for start, end in sorted(spans, reverse=True):
        raw = raw[:start] + bytes(10 if b == 10 else 32 for b in raw[start:end]) + raw[end:]
    return raw.decode('utf-8')


def public_sample(destination: Path, *, queries_per_language=20):
    """300 contiguous test rows per language at three prespecified offsets.

    This is a derived small candidate-pool experiment, NOT an official full score.
    Selection/filtering happen before retrieval; all exclusions are retained.
    """
    destination.mkdir(parents=True, exist_ok=False)
    with httpx.Client(timeout=45, follow_redirects=True) as client:
        metadata = client.get('https://huggingface.co/api/datasets/' + DATASET)
        metadata.raise_for_status()
        metadata = metadata.json()
    write_json(destination / 'upstream-metadata.json', metadata)
    tasks = [(lang, offset) for lang in ('python', 'java', 'javascript') for offset in (17, 317, 617)]
    def fetch(task):
        language, offset = task
        response = httpx.get('https://datasets-server.huggingface.co/rows', params={
            'dataset': DATASET, 'config': language, 'split': 'test', 'offset': offset, 'length': 100}, timeout=60)
        response.raise_for_status()
        data = response.json()
        if data.get('partial') or any(row.get('truncated_cells') for row in data['rows']):
            raise ValueError('Partial public sample')
        write_json(destination / 'raw' / f'{language}-{offset}.json', data)
        return language, data['rows']
    with ThreadPoolExecutor(max_workers=3) as pool:
        pages = list(pool.map(fetch, tasks))
    sources, excluded = [], []
    for language in ('python', 'java', 'javascript'):
        root = destination / language
        root.mkdir()
        eligible, hashes = [], set()
        for _, rows in [p for p in pages if p[0] == language]:
            for item in rows:
                row, ordinal = item['row'], item['row_idx']
                try:
                    code = strip_documentation(row['func_code_string'], language)
                    digest = hashlib.sha256(code.encode()).hexdigest()
                    query = row['func_documentation_string'].strip()
                    if digest in hashes or not 25 <= len(query) <= 1000 or not 30 <= len(code) <= 12000:
                        raise ValueError('prespecified length or duplicate filter')
                    hashes.add(digest)
                    suffix = {'python': 'py', 'java': 'java', 'javascript': 'js'}[language]
                    path = f'samples/s{ordinal:06d}.{suffix}'
                    if language == 'java':
                        code = 'class Sample {\n' + code + '\n}\n'
                    target = root / path
                    target.parent.mkdir(exist_ok=True)
                    target.write_text(code, encoding='utf-8')
                    eligible.append(dict(id=f'public-{language}-{ordinal}', query=query, language=language,
                        category='public_docstring', targets=[dict(path=path, start=1, end=len(code.splitlines()), anchors=[])],
                        source_url=row['func_code_url'], original_symbol=row['func_name'], repository=row['repository_name']))
                except (ValueError, SyntaxError, tokenize.TokenError, IndentationError) as exc:
                    excluded.append(dict(language=language, row=ordinal, reason=str(exc)[:140]))
        rng = random.Random(SEED)
        cases = rng.sample(eligible, min(queries_per_language, len(eligible)))
        write_json(destination / f'{language}-candidates.json', eligible)
        subprocess.run(['git','init','-q',str(root)],check=True,capture_output=True)
        sources.append(dict(id='public-' + language, root=str(root.resolve()), language=language,
            kind='public_proxy_subset', cases=cases, source_revision=metadata['sha'], candidate_files=len(eligible)))
    after = httpx.get('https://huggingface.co/api/datasets/' + DATASET, timeout=30).json()
    if after['sha'] != metadata['sha']:
        raise ValueError('Dataset revision changed during acquisition')
    write_json(destination / 'exclusions.json', excluded)
    return sources
