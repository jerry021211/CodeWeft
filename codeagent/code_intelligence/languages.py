"""Syntax adapters; unconfigured grammars retain explicit text fallback."""
from __future__ import annotations
import ast
from dataclasses import dataclass
import hashlib
import importlib
import importlib.metadata
import json
from pathlib import Path
from functools import cached_property
from .models import CodeChunk, CodeEntity, DocumentSnapshot


@dataclass(frozen=True)
class Language:
    name: str
    extensions: tuple[str, ...]
    module: str = ''
    factory: str = 'language'

    @cached_property
    def fingerprint(self):
        try:
            version = importlib.metadata.version(self.module.replace('_', '-')) if self.module else 'native'
        except importlib.metadata.PackageNotFoundError:
            version = 'unavailable'
        return f'{self.name}:{self.module}:{self.factory}:{version}:analysis-v2.3'


LANGUAGES = (
    Language('python', ('.py', '.pyi')),
    Language('java', ('.java',), 'tree_sitter_java'),
    Language('typescript', ('.ts', '.mts', '.cts'), 'tree_sitter_typescript', 'language_typescript'),
    Language('tsx', ('.tsx',), 'tree_sitter_typescript', 'language_tsx'),
    Language('javascript', ('.js', '.jsx', '.mjs', '.cjs'), 'tree_sitter_javascript'),
    Language('go', ('.go',)), Language('rust', ('.rs',)), Language('csharp', ('.cs',)),
    Language('kotlin', ('.kt', '.kts')), Language('cpp', ('.c', '.h', '.cc', '.cpp', '.hpp')),
    Language('ruby', ('.rb',)), Language('php', ('.php',)), Language('swift', ('.swift',)),
    Language('vue', ('.vue',)), Language('svelte', ('.svelte',)), Language('sql', ('.sql',)),
    Language('xml', ('.xml',)), Language('yaml', ('.yaml', '.yml')), Language('json', ('.json',)),
    Language('toml', ('.toml',)), Language('markdown', ('.md', '.mdx')), Language('shell', ('.sh', '.ps1')),
)


def language_for(path):
    suffix = Path(path).suffix.lower()
    return next((language for language in LANGUAGES if suffix in language.extensions), None)


class Analysis:
    def __init__(self, snapshot, parser, quality, check):
        self.snapshot, self.parser, self.quality, self.check = snapshot, parser, quality, check
        self.lines = snapshot.text.splitlines()
        self.documents, self.identities = [], {}

    def add(self, name, kind, definition, start, end, signature='', container='', boundaries=()):
        self.check()
        if end < start or not self.lines:
            return
        key = json.dumps([self.snapshot.path, self.snapshot.language, name, kind, signature], ensure_ascii=False)
        occurrence = self.identities.get(key, 0)
        self.identities[key] = occurrence + 1
        identity = hashlib.sha256((key + ':' + str(occurrence)).encode()).hexdigest()
        entity = CodeEntity(identity, name, kind, signature, definition, container)
        cursor = start
        while cursor <= end:
            stop = end + 1 if end - cursor < 60 else max(
                (b for b in boundaries if cursor < b <= cursor + 60), default=min(cursor + 60, end + 1))
            document = CodeChunk(entity, cursor, stop - 1, self.parser, self.quality).document(self.snapshot)
            document['entity_end_line'] = end
            self.documents.append(document)
            cursor = stop

    def fallback(self):
        for start in range(1, len(self.lines) + 1, 40):
            self.add('<text>', 'parse_fallback' if self.quality != 'text' else 'text',
                     start, start, min(start + 39, len(self.lines)))


def python_analysis(result):
    try:
        tree = ast.parse(result.snapshot.text)
    except (SyntaxError, ValueError, RecursionError):
        result.quality = 'fallback'
        result.fallback()
        return

    def visit(node, parents=()):
        result.check()
        named = isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        scope = (*parents, node.name) if named else parents
        if named:
            start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
            is_class = isinstance(node, ast.ClassDef)
            signature = (node.name + '(' + ','.join(ast.unparse(b) for b in node.bases) + ')'
                         if is_class else node.name + '(' + ast.unparse(node.args) + ')')
            end = node.end_lineno
            if is_class:
                methods = [min([s.lineno, *(d.lineno for d in s.decorator_list)])
                           for s in node.body if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
                if methods:
                    end = min(methods) - 1
            result.add('.'.join(scope), 'class' if is_class else 'function', node.lineno,
                       start, end, signature, '.'.join(parents), [s.lineno for s in node.body])
            if not is_class:
                search_signature = '\n'.join(result.lines[node.lineno - 1:node.body[0].lineno])[:1500]
                for doc in reversed(result.documents):
                    if doc['symbol'] != '.'.join(scope):
                        break
                    doc['search_signature'] = search_signature
            if is_class:
                for child in node.body:
                    targets = child.targets if isinstance(child, ast.Assign) else [child.target] if isinstance(child, ast.AnnAssign) else []
                    for target in targets:
                        if isinstance(target, ast.Name):
                            result.add('.'.join((*scope, target.id)), 'field', child.lineno,
                                       child.lineno, child.end_lineno, target.id, '.'.join(scope))
        for child in ast.iter_child_nodes(node):
            visit(child, scope)
    visit(tree)
    for statement in tree.body:
        if not isinstance(statement, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            result.add('<module>', 'module', statement.lineno, statement.lineno, statement.end_lineno)


def tree_analysis(result, language):
    try:
        from tree_sitter import Language as Grammar, Parser
        grammar = Grammar(getattr(importlib.import_module(language.module), language.factory)())
        parser = Parser(grammar)
    except (ImportError, AttributeError, ValueError):
        result.parser, result.quality = 'text', 'fallback'
        result.fallback()
        return
    raw = result.snapshot.text.encode('utf-8')
    result.check()
    tree = parser.parse(raw)
    result.check()
    result.quality = 'partial' if tree.root_node.has_error else 'syntax'
    kinds = {'class_declaration': 'class', 'class': 'class', 'interface_declaration': 'interface',
             'enum_declaration': 'enum', 'record_declaration': 'record', 'annotation_type_declaration': 'annotation',
             'method_declaration': 'method', 'method_definition': 'method', 'method_signature': 'method',
             'constructor_declaration': 'constructor', 'compact_constructor_declaration': 'constructor',
             'function_declaration': 'function', 'generator_function_declaration': 'function',
             'type_alias_declaration': 'type', 'public_field_definition': 'field',
             'property_signature': 'field', 'enum_constant': 'field'}
    containers = {'class', 'interface', 'enum', 'record', 'annotation'}

    def text(node):
        return raw[node.start_byte:node.end_byte].decode('utf-8') if node else ''

    def visit(node, parents=(), in_callable=False):
        result.check()
        kind = kinds.get(node.type)
        name_node, value = node.child_by_field_name('name'), node.child_by_field_name('value')
        if node.type == 'variable_declarator' and name_node:
            kind = ('function' if value and value.type in ('arrow_function', 'function_expression', 'generator_function')
                    else 'field' if node.parent and node.parent.type == 'field_declaration' else 'variable')
            if kind == 'variable' and in_callable:
                kind = None
        scope = parents
        if kind and name_node:
            scope = (*parents, text(name_node))
            body = node.child_by_field_name('body')
            if value and kind == 'function':
                body = value.child_by_field_name('body')
            header_end = body.start_byte if body else node.end_byte
            signature = ' '.join(raw[node.start_byte:header_end].decode('utf-8').split())[:1500]
            start, end = node.start_point.row + 1, node.end_point.row + (node.end_point.column > 0)
            if kind in containers and body:
                end = body.start_point.row + 1
            if node.parent and node.parent.type == 'export_statement':
                start = node.parent.start_point.row + 1
            sibling = node.prev_named_sibling
            if sibling and sibling.type in ('comment', 'block_comment', 'line_comment') and sibling.end_point.row >= start - 2:
                start = sibling.start_point.row + 1
            result.add('.'.join(scope), kind, name_node.start_point.row + 1, start, max(start, end), signature,
                       '.'.join(parents), [c.start_point.row + 1 for c in body.named_children] if body else [])
        for child in node.named_children:
            visit(child, scope if kind in containers or kind in ('method', 'function', 'constructor') else parents,
                  in_callable or kind in ('method', 'function', 'constructor'))
    visit(tree.root_node)
    covered = set()
    for doc in result.documents:
        covered.update(range(doc['start_line'], doc['end_line'] + 1))
    cursor = 1
    while cursor <= len(result.lines):
        if cursor in covered or not result.lines[cursor - 1].strip():
            cursor += 1
            continue
        end = cursor
        while end < len(result.lines) and end + 1 not in covered and end - cursor < 39:
            end += 1
        result.add('<module>', 'parse_fallback' if tree.root_node.has_error else 'module', cursor, cursor, end)
        cursor = end + 1


def analyze(path, raw, check=lambda: None):
    language = language_for(path)
    if language is None:
        return []
    result = Analysis(DocumentSnapshot.read(path, raw, language.name),
                      'python_ast' if language.name == 'python' else 'tree_sitter' if language.module else 'text',
                      'syntax' if language.name == 'python' or language.module else 'text', check)
    if language.name == 'python':
        python_analysis(result)
    elif language.module:
        tree_analysis(result, language)
    else:
        result.fallback()
    for doc in result.documents:
        doc['parser_fingerprint'] = language.fingerprint
    return result.documents
