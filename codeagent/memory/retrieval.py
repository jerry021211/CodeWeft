"""Rebuildable SQLite FTS5 index over authoritative Markdown memory files."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from codeagent.memory.access import MemoryWriteBlocked
from codeagent.memory.models import MemoryRecord

if TYPE_CHECKING:
    from codeagent.memory.store import MemoryStore

INDEX_NAME = ".retrieval-v1.sqlite3"
INDEX_VERSION = "1:cjk-bigram-code-v1"
_WORDS = re.compile(r"[A-Za-z0-9_./\\-]+|[\u3400-\u9fff]+")
_CAMEL = re.compile(r"([a-z0-9])([A-Z])|([A-Z])([A-Z][a-z])")
_STOP = set("a an the is are of for to in on and or with this that how what please can you me my be as".split())
_STOP.update({"如何", "什么", "怎么", "请问", "一下", "这个", "进行", "我们", "可以", "需要", "使用"})


def terms(text: str) -> list[str]:
    """Keep exact code identifiers plus components; CJK uses overlapping bigrams."""
    result = []
    for match in _WORDS.finditer(text):
        word = match.group()
        if "\u3400" <= word[0] <= "\u9fff":
            pieces = [word] if len(word) == 1 else [word[i:i + 2] for i in range(len(word) - 1)]
        else:
            whole = word.strip("./\\-").replace("\\", "/").casefold()
            split = _CAMEL.sub(lambda m: f"{m[1]} {m[2]}" if m[1] else f"{m[3]} {m[4]}", word)
            pieces = list(dict.fromkeys([whole, *re.findall(r"[a-z0-9]+", split.casefold())]))
        result.extend(piece for piece in pieces if piece and piece not in _STOP)
    return result


def query_terms(query: str) -> list[str]:
    unique = list(dict.fromkeys(terms(query)))
    return unique if len(unique) <= 128 else unique[:64] + unique[-64:]


def _encoded(text: str) -> str:
    # ASCII encoding ensures SQLite's own tokenizer cannot split code symbols.
    return " ".join("t" + item.encode("utf-8").hex() for item in terms(text))


def version(record: MemoryRecord) -> str:
    return hashlib.sha256(json.dumps(asdict(record), ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def excerpt(record: MemoryRecord, query: str, limit: int = 500) -> str:
    text = record.content
    folded = text.casefold()
    positions = [folded.find(term) for term in query_terms(query)]
    positions = [position for position in positions if position >= 0]
    start = max(0, min(positions) - 100) if positions else 0
    return ("…" if start else "") + text[start:start + limit] + ("…" if len(text) > start + limit else "")


@dataclass(frozen=True, slots=True)
class MemoryHit:
    record: MemoryRecord
    version: str
    score: float
    excerpt: str


@dataclass(slots=True)
class RetrievalResult:
    hits: list[MemoryHit] = field(default_factory=list)
    backend: str = "none"
    revision: int = 0
    files_scanned: int = 0
    files_read: int = 0
    reason: str = ""
    full_verification: bool = False


def _inventory(store: MemoryStore) -> dict[str, tuple[Path, str]]:
    files = {}
    # Resolve the directory once. scandir only yields immediate children, and
    # no-follow stat excludes links; resolving every child dominates Windows I/O.
    root = store.root.resolve()
    if not root.is_dir():
        return files
    with os.scandir(root) as entries:
        for entry in entries:
            if entry.name == "MEMORY.md" or not entry.name.endswith(".md") or entry.is_symlink():
                continue
            try:
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISREG(info.st_mode):
                    signature = json.dumps([info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_ino])
                    files[entry.name] = (root / entry.name, signature)
            except OSError:
                continue
    return files


def _schema(connection: sqlite3.Connection) -> None:
    connection.execute("CREATE TABLE IF NOT EXISTS index_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    row = connection.execute("SELECT value FROM index_meta WHERE key='version'").fetchone()
    if row is not None and row[0] != INDEX_VERSION:
        connection.execute("DROP TABLE IF EXISTS memory_fts")
        connection.execute("DROP TABLE IF EXISTS documents")
        connection.execute("DELETE FROM index_meta")
    connection.execute("CREATE TABLE IF NOT EXISTS documents(filename TEXT PRIMARY KEY, signature TEXT, version TEXT, record_json TEXT)")
    connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(filename UNINDEXED, name, description, body)")
    connection.execute("INSERT OR IGNORE INTO index_meta VALUES('version', ?)", (INDEX_VERSION,))
    connection.execute("INSERT OR IGNORE INTO index_meta VALUES('revision', '0')")
    connection.execute("INSERT OR IGNORE INTO index_meta VALUES('verified_at', '0')")


def _sync(connection, store, files, result, verify_seconds, check_cancelled):
    # One generation, including deletions, becomes visible atomically.
    connection.execute("BEGIN IMMEDIATE")
    try:
        _schema(connection)
        previous = dict(connection.execute("SELECT filename, signature FROM documents"))
        verified = float(connection.execute("SELECT value FROM index_meta WHERE key='verified_at'").fetchone()[0])
        now = time.time()
        result.full_verification = verify_seconds == 0 or now - verified >= verify_seconds or now < verified
        changed = False
        for filename in set(previous) - set(files):
            connection.execute("DELETE FROM documents WHERE filename=?", (filename,))
            connection.execute("DELETE FROM memory_fts WHERE filename=?", (filename,))
            changed = True
        for filename, (path, signature) in files.items():
            if check_cancelled:
                check_cancelled()
            if previous.get(filename) == signature and not result.full_verification:
                continue
            result.files_read += 1
            try:
                record = store._read_record(path)
            except (OSError, ValueError):
                connection.execute("DELETE FROM documents WHERE filename=?", (filename,))
                connection.execute("DELETE FROM memory_fts WHERE filename=?", (filename,))
                changed = True
                continue
            digest = version(record)
            old = connection.execute("SELECT version FROM documents WHERE filename=?", (filename,)).fetchone()
            if old is not None and old[0] == digest:
                if previous.get(filename) != signature:
                    connection.execute("UPDATE documents SET signature=? WHERE filename=?", (signature, filename))
                continue
            connection.execute("INSERT OR REPLACE INTO documents VALUES(?,?,?,?)",
                               (filename, signature, digest, json.dumps(asdict(record), ensure_ascii=False)))
            connection.execute("DELETE FROM memory_fts WHERE filename=?", (filename,))
            connection.execute("INSERT INTO memory_fts VALUES(?,?,?,?)",
                               (filename, _encoded(record.name + " " + filename), _encoded(record.description), _encoded(record.content)))
            changed = True
        if changed:
            connection.execute("UPDATE index_meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
        if result.full_verification:
            connection.execute("UPDATE index_meta SET value=? WHERE key='verified_at'", (str(now),))
        result.revision = int(connection.execute("SELECT value FROM index_meta WHERE key='revision'").fetchone()[0])
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _query(connection, query, limit):
    expression = " OR ".join('"t' + term.encode("utf-8").hex() + '"' for term in query_terms(query))
    rows = connection.execute(
        "SELECT d.record_json, d.version, -bm25(memory_fts, 0, 5, 3, 1) AS score "
        "FROM memory_fts JOIN documents d ON d.filename=memory_fts.filename "
        "WHERE memory_fts MATCH ? ORDER BY bm25(memory_fts, 0, 5, 3, 1), d.filename LIMIT ?",
        (expression, limit),
    ).fetchall()
    return [MemoryHit(record := MemoryRecord(**json.loads(raw)), digest, score, excerpt(record, query))
            for raw, digest, score in rows]


def _fallback(store, files, query, limit, result, check_cancelled):
    """Bounded-dependency BM25 fallback when disk or FTS5 is unavailable."""
    records = []
    for path, _ in files.values():
        if check_cancelled:
            check_cancelled()
        try:
            records.append(store._read_record(path))
        except (OSError, ValueError):
            continue
    result.files_read += len(files)
    result.backend = "python_bm25"
    query_set = query_terms(query)
    fields = [(Counter(terms(r.name + " " + r.filename)), Counter(terms(r.description)), Counter(terms(r.content))) for r in records]
    lengths = [sum(sum(c.values()) for c in item) for item in fields]
    average = sum(lengths) / len(lengths) if lengths else 1
    frequencies = {term: sum(any(term in field for field in item) for item in fields) for term in query_set}
    hits = []
    for record, item, length in zip(records, fields, lengths):
        score = 0.0
        for term in query_set:
            frequency = sum(weight * field[term] for weight, field in zip((5, 3, 1), item))
            if frequency:
                count = frequencies[term]
                idf = max(1e-6, math.log((len(records) - count + 0.5) / (count + 0.5)))
                score += idf * frequency * 2.2 / (frequency + 1.2 * (0.25 + 0.75 * length / (average or 1)))
        if score:
            hits.append(MemoryHit(record, version(record), score, excerpt(record, query)))
    result.hits = sorted(hits, key=lambda hit: (-hit.score, hit.record.filename))[:limit]
    return result


def retrieve(store: MemoryStore, query: str, *, limit: int = 50,
             allow_index_write: bool = True, verify_seconds: float = 60,
             check_cancelled: Callable[[], None] | None = None) -> RetrievalResult:
    result = RetrievalResult()
    if limit <= 0 or not query_terms(query):
        result.reason = "empty_query" if limit > 0 else "candidate_limit"
        return result
    with store.reading():
        files = _inventory(store)
        result.files_scanned = len(files)
        if not files:
            result.reason = "empty_store"
            return result
        cache = store.root / INDEX_NAME
        cache_safe = not cache.is_symlink() and store._is_inside_root(cache)
        if allow_index_write and cache_safe:
            try:
                with store.writing():
                    connection = sqlite3.connect(cache, timeout=1)
                    try:
                        _sync(connection, store, files, result, verify_seconds, check_cancelled)
                        result.hits = _query(connection, query, limit)
                        result.backend = "sqlite_fts5"
                        return result
                    finally:
                        connection.close()
            except MemoryWriteBlocked:
                result.reason = "read_only"
            except (sqlite3.DatabaseError, OSError, ValueError):
                # A corrupt derived file is never allowed to erase authoritative data.
                result.reason = "index_unavailable"
        else:
            result.reason = "read_only" if cache_safe else "unsafe_index_path"
        # Read-only work operates on a private snapshot; refresh never touches disk.
        connection = sqlite3.connect(":memory:")
        try:
            if cache_safe and cache.is_file():
                try:
                    disk = sqlite3.connect(cache.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
                    try:
                        deadline = time.monotonic() + 1
                        def progress(status, remaining, total):
                            if check_cancelled:
                                check_cancelled()
                            if time.monotonic() > deadline:
                                raise TimeoutError("Memory index snapshot timed out")
                        disk.backup(connection, pages=128, progress=progress, sleep=0.01)
                    finally:
                        disk.close()
                except sqlite3.DatabaseError:
                    connection.close()
                    connection = sqlite3.connect(":memory:")
            _sync(connection, store, files, result, verify_seconds, check_cancelled)
            result.hits = _query(connection, query, limit)
            result.backend = "sqlite_memory"
            return result
        except (sqlite3.DatabaseError, OSError, ValueError):
            result.reason = "fts_unavailable"
            return _fallback(store, files, query, limit, result, check_cancelled)
        finally:
            connection.close()
