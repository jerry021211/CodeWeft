"""Incremental source publication and consistent query snapshots."""
from __future__ import annotations
import hashlib
import json
import sqlite3
from pathlib import Path
from codeagent.code_intelligence.languages import analyze, language_for, Analysis
from codeagent.code_intelligence.models import DocumentSnapshot
from codeagent.memory.retrieval import terms
from codeagent.tools.search_files import search_files
from codeagent.tools.workspace import WorkspaceGuard
from .store import connect, replace_file
from .ranking import content_prior


class CodeIndex:
    def __init__(self, workspace, directory):
        self.guard, self.directory = WorkspaceGuard(workspace), Path(directory)
        self.documents, self.notes = [], []
        self.incomplete, self.updated, self.generation = False, 0, 0
        self.backend, self.snapshot = 'sqlite_fts5', None
        self.coverage = {}
        self.analysis_mode = 'syntax'

    def analyze(self, relative, raw, check):
        if self.analysis_mode == 'text':
            result = Analysis(DocumentSnapshot.read(relative, raw, language_for(relative).name), 'text', 'text', check)
            result.fallback()
            return result.documents
        return analyze(relative, raw, check)

    def close_snapshot(self):
        if self.snapshot is not None:
            self.snapshot.close()
            self.snapshot = None

    def _read(self, path, check):
        check()
        safe = self.guard.ensure_within(path)
        before = safe.stat()
        if before.st_size > 1024 * 1024:
            raise ValueError('file exceeds 1 MiB')
        raw = safe.read_bytes()
        after = safe.stat()
        check()
        if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
            raise ValueError('source changed while reading')
        return raw, (after.st_mtime_ns, after.st_size)

    def sync(self, check, *, force=False, force_paths=()):
        self.close_snapshot()
        check()
        inventory = search_files(self.guard.root, self.guard, check=check)
        self.notes, self.incomplete, self.updated = list(inventory.notes), inventory.incomplete, 0
        files = [p for p in inventory.paths if language_for(p)]
        present = {p.relative_to(self.guard.root).as_posix() for p in files}
        conn = None
        try:
            conn = connect(self.directory, self.guard.root)
            old = {path: (digest, parser) for path, digest, parser in conn.execute('SELECT path,hash,parser FROM files')}
            changes = {}
            # Hash and parse outside the write transaction, including same-mtime edits.
            for path in files:
                check()
                relative = path.relative_to(self.guard.root).as_posix()
                try:
                    raw, _ = self._read(path, check)
                    digest, parser = hashlib.sha256(raw).hexdigest(), language_for(path).fingerprint + ':' + self.analysis_mode
                    if not force and relative not in force_paths and old.get(relative) == (digest, parser):
                        continue
                    documents = self.analyze(relative, raw, check)
                    current, _ = self._read(path, check)
                    if hashlib.sha256(current).hexdigest() != digest:
                        raise ValueError('source changed while parsing')
                    changes[relative] = (digest, parser, documents)
                except (OSError, ValueError, UnicodeError) as exc:
                    self.incomplete = True
                    self.notes.append(f'Skipped {relative}: {type(exc).__name__}')
                    present.discard(relative)
            # Optimistic compare prevents an older parse overwriting another writer.
            conn.execute('BEGIN IMMEDIATE')
            current = {path: (digest, parser) for path, digest, parser in conn.execute('SELECT path,hash,parser FROM files')}
            deletes = old.keys() - present if not self.incomplete else set()
            for relative in sorted(set(changes) | set(deletes)):
                check()
                if current.get(relative) != old.get(relative):
                    desired = changes[relative][:2] if relative in changes else None
                    if current.get(relative) == desired:
                        # Another writer already published exactly this source /
                        # parser version (or the same deletion). It is complete.
                        continue
                    self.incomplete = True
                    self.notes.append(f'Concurrent index update: {relative}; retry on next search.')
                    continue
                replace_file(conn, relative, *changes[relative]) if relative in changes else replace_file(conn, relative)
                self.updated += relative in changes
            if changes or deletes:
                conn.execute("UPDATE meta SET value=value+1 WHERE key='generation'")
            conn.commit()
            # A read transaction pins FTS and all routes to the same generation.
            conn.execute('BEGIN')
            self.generation = conn.execute("SELECT value FROM meta WHERE key='generation'").fetchone()[0]
            self.documents = [(identifier, json.loads(payload)) for identifier, path, payload in
                              conn.execute('SELECT id,path,payload FROM chunks ORDER BY path,id') if path in present]
            self.snapshot, conn, self.backend = conn, None, 'sqlite_fts5'
        except (sqlite3.Error, OSError, ValueError) as exc:
            self.backend = 'python_fallback'
            self.notes.append(f'Index unavailable ({type(exc).__name__}); scanned current source without cache.')
            self.documents = []
            for path in files:
                check()
                try:
                    raw, _ = self._read(path, check)
                    for doc in self.analyze(path.relative_to(self.guard.root).as_posix(), raw, check):
                        self.documents.append((len(self.documents) + 1, doc))
                except (OSError, ValueError, UnicodeError):
                    self.incomplete = True
            self.updated = len(files)
        finally:
            if conn is not None:
                conn.close()
        fallback = {d['path'] for _, d in self.documents if d['parse_quality'] in ('fallback', 'partial')}
        if fallback:
            self.notes.append(f'Parser fallback/partial in {len(fallback)} file(s); inspect parse_quality.')
        self.coverage = dict(eligible_files=len(files), indexed_files=len({d['path'] for _, d in self.documents}),
                             chunks=len(self.documents), fallback_files=len(fallback))

    def rank(self, query, scope, check):
        check()
        allowed = {i: d for i, d in self.documents if self.in_scope(d['path'], scope)}
        tokens = list(dict.fromkeys(terms(query)))[:64]
        if not tokens:
            return []
        if self.snapshot is not None:
            try:
                match = ' OR '.join('"t' + token.encode('utf-8').hex() + '"' for token in tokens)
                rows = self.snapshot.execute('SELECT rowid,bm25(search,8,3,3,1) FROM search WHERE search MATCH ?', (match,))
                ranked = []
                for identifier, score in rows:
                    check()
                    if identifier in allowed:
                        ranked.append((score * content_prior(allowed[identifier]['path'], query), identifier))
                return [allowed[identifier] for _, identifier in sorted(ranked)[:80]]
            except sqlite3.Error:
                self.notes.append('FTS unavailable; used token overlap ranking.')
                self.backend = 'python_fallback'
        wanted, scores = set(tokens), []
        for doc in allowed.values():
            check()
            score = sum(weight * len(wanted & set(terms(doc[field]))) for field, weight in
                        (('symbol', 8), ('path', 3), ('comments', 3), ('body', 1)))
            if score:
                scores.append((score, doc))
        return [d for _, d in sorted(scores, key=lambda row: (-row[0], row[1]['parent'], row[1]['start_line']))[:80]]

    @staticmethod
    def in_scope(path, scope):
        return scope == '.' or path == scope or path.startswith(scope.rstrip('/') + '/')
