"""Incremental SQLite FTS5 cache; source remains authoritative."""
from __future__ import annotations

import hashlib
from contextlib import closing
import json
import sqlite3
import time
from pathlib import Path

from codeagent.code_search.chunks import chunks, encoded
from codeagent.memory.retrieval import terms
from codeagent.tools.search_files import search_files
from codeagent.tools.workspace import WorkspaceGuard


class CodeIndex:
    def __init__(self, workspace: Path, directory: Path):
        self.guard = WorkspaceGuard(workspace)
        self.directory = Path(directory)
        self.documents = []
        self.notes = []
        self.incomplete = False
        self.updated = 0
        self.backend = "sqlite_fts5"

    def _read(self, path, check):
        check()
        safe = self.guard.ensure_within(path)
        before = safe.stat()
        if before.st_size > 1024 * 1024:
            raise ValueError("file exceeds 1 MiB")
        raw = safe.read_bytes()
        after = safe.stat()
        if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
            raise ValueError("source changed while reading")
        return raw, (after.st_mtime_ns, after.st_size)

    def sync(self, check, *, force=False):
        check()
        inventory = search_files(self.guard.root, self.guard)
        check()
        self.notes = list(inventory.notes)
        self.incomplete = inventory.incomplete
        files = [p for p in inventory.paths if p.suffix.lower() == ".py"]
        self.updated = 0
        connection = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.directory / "source-v1.sqlite3", timeout=0.25)
            connection.execute("CREATE TABLE IF NOT EXISTS identity (workspace TEXT)")
            identity = connection.execute("SELECT workspace FROM identity").fetchone()
            if identity and identity[0] != str(self.guard.root):
                raise sqlite3.DatabaseError("index belongs to a different workspace")
            if not identity:
                connection.execute("INSERT INTO identity VALUES (?)", (str(self.guard.root),))
            connection.execute("CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, stamp TEXT, hash TEXT, verified REAL)")
            connection.execute("CREATE TABLE IF NOT EXISTS chunks (id INTEGER PRIMARY KEY, path TEXT, payload TEXT)")
            connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(symbol, path, comments, body)")
            connection.commit()
            old = {row[0]: row[1:] for row in connection.execute("SELECT path,stamp,hash,verified FROM files")}
            present, changes = set(), {}
            now = time.time()
            for path in files:
                check()
                relative = path.relative_to(self.guard.root).as_posix()
                present.add(relative)
                try:
                    stat = self.guard.ensure_within(path).stat()
                    stamp = json.dumps([stat.st_mtime_ns, stat.st_size])
                    previous = old.get(relative)
                    if not force and previous and previous[0] == stamp and now - previous[2] < 60:
                        continue
                    raw, read_stamp = self._read(path, check)
                    digest = hashlib.sha256(raw).hexdigest()
                    parsed = chunks(relative, raw, check)
                    changes[relative] = (json.dumps(read_stamp), digest, parsed)
                except (OSError, ValueError, UnicodeError) as exc:
                    self.incomplete = True
                    self.notes.append(f"Skipped {relative}: {type(exc).__name__}")
                    present.discard(relative)
            # Parsing and filesystem I/O happen outside the write transaction.
            with connection:
                for relative in sorted((old.keys() - present) | changes.keys()):
                    check()
                    connection.execute("DELETE FROM search WHERE rowid IN (SELECT id FROM chunks WHERE path=?)", (relative,))
                    connection.execute("DELETE FROM chunks WHERE path=?", (relative,))
                    connection.execute("DELETE FROM files WHERE path=?", (relative,))
                    if relative not in changes:
                        continue
                    stamp, digest, parsed = changes[relative]
                    # Recheck before committing spans produced by a possibly concurrent edit.
                    raw, _ = self._read(self.guard.root / relative, check)
                    if hashlib.sha256(raw).hexdigest() != digest:
                        self.incomplete = True
                        self.notes.append(f"Source changed during indexing: {relative}")
                        continue
                    connection.execute("INSERT INTO files VALUES (?,?,?,?)", (relative, stamp, digest, now))
                    for doc in parsed:
                        row = connection.execute("INSERT INTO chunks(path,payload) VALUES (?,?)", (relative, json.dumps(doc, ensure_ascii=False)))
                        connection.execute("INSERT INTO search(rowid,symbol,path,comments,body) VALUES (?,?,?,?,?)",
                                           (row.lastrowid, encoded(doc['symbol'] + ' ' + doc['signature']), encoded(relative), encoded(doc['comments']), encoded(doc['body'])))
                    self.updated += 1
            self.documents = [(row[0], json.loads(row[1])) for row in connection.execute("SELECT id,payload FROM chunks ORDER BY id")]
            self.backend = "sqlite_fts5"
        except (sqlite3.Error, OSError, ValueError) as exc:
            self.backend = "python_fallback"
            self.notes.append(f"Index unavailable ({type(exc).__name__}); scanned current source without cache.")
            self.documents = []
            for path in files:
                check()
                try:
                    raw, _ = self._read(path, check)
                    for doc in chunks(path.relative_to(self.guard.root).as_posix(), raw, check):
                        self.documents.append((len(self.documents) + 1, doc))
                except (OSError, ValueError, UnicodeError):
                    self.incomplete = True
            self.updated = len(files)
        finally:
            if connection is not None:
                connection.close()
        fallbacks = sorted({d['path'] for _, d in self.documents if d['kind'] == 'parse_fallback'})
        if fallbacks:
            self.incomplete = True
            self.notes.append(f"AST parse fallback in {len(fallbacks)} file(s); results are text spans.")

    def rank(self, query, scope, check):
        check()
        allowed = {i: d for i, d in self.documents if self.in_scope(d['path'], scope)}
        tokens = list(dict.fromkeys(terms(query)))[:64]
        if not tokens:
            return []
        if self.backend == "sqlite_fts5":
            try:
                with closing(sqlite3.connect(self.directory / "source-v1.sqlite3", timeout=0.25)) as connection:
                    match = " OR ".join('"t' + token.encode('utf-8').hex() + '"' for token in tokens)
                    # Filter scope before the per-route limit, including small subdirectories.
                    rows = connection.execute("SELECT chunks.payload FROM search JOIN chunks ON chunks.id=search.rowid WHERE search MATCH ? ORDER BY bm25(search,8,3,3,1),search.rowid", (match,))
                    ranked = []
                    for (payload,) in rows:
                        check()
                        doc = json.loads(payload)
                        if self.in_scope(doc['path'], scope):
                            ranked.append(doc)
                            if len(ranked) == 20:
                                break
                    return ranked
            except sqlite3.Error:
                self.notes.append("FTS query unavailable; used token overlap ranking.")
        scores = []
        wanted = set(tokens)
        for _, doc in allowed.items():
            check()
            score = sum(weight * len(wanted & set(terms(doc[field]))) for field, weight in (("symbol", 8), ("path", 3), ("comments", 3), ("body", 1)))
            if score:
                scores.append((score, doc))
        return [d for _, d in sorted(scores, key=lambda x: (-x[0], x[1]['parent'], x[1]['start_line']))[:20]]

    @staticmethod
    def in_scope(path, scope):
        return scope == "." or path == scope or path.startswith(scope.rstrip('/') + '/')
