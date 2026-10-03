"""Resumable, leased embedding construction, separate from vector ranking."""
from contextlib import closing
import hashlib
import json
import sqlite3
import time
import uuid
from codeagent.code_search.embedding import normalized

VECTOR_DATABASE = 'vectors-v2.sqlite3'
INPUT_VERSION = 2


class VectorIndex:
    def __init__(self, index, provider, max_chunks=2000):
        self.index, self.provider, self.max_chunks = index, provider, max_chunks
        self.stats = dict(eligible=0, ready=0, pending=0, embedded=0, cache_hits=0,
                          requests=0, failed_requests=0, input_tokens=0, usage_complete=True)

    def _inputs(self, scope, check):
        keyed = {}
        for _, doc in self.index.documents:
            if not self.index.in_scope(doc['path'], scope):
                continue
            lines = doc['body'].split('\n')
            # Semantic evidence windows fit the public quote budget. Ranking keeps
            # the actual matched window instead of reselecting it with lexical words.
            for offset in range(0, len(lines), 20):
                check()
                part = dict(doc, start_line=doc['start_line'] + offset,
                            end_line=min(doc['start_line'] + offset + 19, doc['end_line']),
                            body='\n'.join(lines[offset:offset + 20]))
                text = '\n'.join((doc['path'], doc['symbol'], doc['signature'], part['body']))
                key = hashlib.sha256(text.encode()).hexdigest()
                keyed[key] = (part, text)
        return keyed

    def _connection(self):
        self.index.directory.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.index.directory / VECTOR_DATABASE, timeout=.25)
        try:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('CREATE TABLE IF NOT EXISTS vectors (identity TEXT, key TEXT, path TEXT, vector TEXT, PRIMARY KEY(identity,key))')
            conn.execute('CREATE TABLE IF NOT EXISTS jobs (identity TEXT,key TEXT,owner TEXT,expires REAL,PRIMARY KEY(identity,key))')
            conn.commit()
            return conn
        except BaseException:
            conn.close()
            raise

    def _profile(self):
        p = self.provider
        return json.dumps([str(self.index.guard.root), p.provider, p.model, p.dimensions, INPUT_VERSION])

    def _embed(self, texts, check, remaining):
        self.stats['requests'] += 1
        try:
            values = self.provider.embed(texts, check=check, timeout=min(10., remaining()))
        except BaseException:
            self.stats['usage_complete'] = False
            self.stats['failed_requests'] += 1
            raise
        usage = getattr(values, 'usage', None)
        if usage:
            self.stats['requests'] += max(0, usage.get('requests', 1) - 1)
        if usage is None or usage.get('input_tokens') is None:
            self.stats['usage_complete'] = False
        else:
            self.stats['input_tokens'] += usage['input_tokens']
        check()
        if len(values) != len(texts):
            raise ValueError('Embedding batch size mismatch')
        return [normalized(v, self.provider.dimensions) for v in values]

    def _cached(self, conn, identity, keyed):
        cached = {}
        for key, value in conn.execute('SELECT key,vector FROM vectors WHERE identity=?', (identity,)):
            if key in keyed:
                try:
                    cached[key] = normalized(json.loads(value), self.provider.dimensions)
                except (ValueError, TypeError):
                    pass  # Damaged entries are rebuilt; valid neighbors stay usable.
        return cached

    def build(self, scope, check, remaining, *, enabled=True):
        keyed, identity = self._inputs(scope, check), self._profile()
        owner = uuid.uuid4().hex
        with closing(self._connection()) as conn:
            cached = self._cached(conn, identity, keyed)
            self.stats.update(eligible=len(keyed), cache_hits=len(cached))
            missing = [key for key in keyed if key not in cached]
            processed = 0
            try:
                for offset in range(0, len(missing), 32):
                    check()
                    if not enabled or processed >= self.max_chunks:
                        break
                    # A short claim transaction prevents duplicate paid batches across
                    # processes. Expiring leases permit recovery after a crashed worker.
                    conn.execute('BEGIN IMMEDIATE')
                    batch = []
                    try:
                        now = time.time()
                        for key in missing[offset:offset + min(32, self.max_chunks - processed)]:
                            row = conn.execute('SELECT vector FROM vectors WHERE identity=? AND key=?', (identity, key)).fetchone()
                            if row:
                                try:
                                    normalized(json.loads(row[0]), self.provider.dimensions)
                                except (ValueError, TypeError):
                                    conn.execute('DELETE FROM vectors WHERE identity=? AND key=?', (identity, key))
                                else:
                                    continue
                            row = conn.execute('SELECT expires FROM jobs WHERE identity=? AND key=?', (identity, key)).fetchone()
                            if row and row[0] > now:
                                continue
                            conn.execute('INSERT OR REPLACE INTO jobs VALUES (?,?,?,?)', (identity, key, owner, now + 30))
                            batch.append(key)
                        conn.commit()
                    except BaseException:
                        conn.rollback()
                        raise
                    if not batch:
                        continue
                    values = self._embed([keyed[k][1] for k in batch], check, remaining)
                    valid = {}
                    for key in batch:
                        doc = keyed[key][0]
                        if doc['path'] not in valid:
                            try:
                                raw, _ = self.index._read(self.index.guard.root / doc['path'], check)
                                valid[doc['path']] = hashlib.sha256(raw).hexdigest() == doc['content_hash']
                            except (OSError, ValueError):
                                valid[doc['path']] = False
                    with conn:
                        for key, vector in zip(batch, values):
                            if valid[keyed[key][0]['path']]:
                                conn.execute('INSERT OR REPLACE INTO vectors VALUES (?,?,?,?)',
                                             (identity, key, keyed[key][0]['path'], json.dumps(vector)))
                            conn.execute('DELETE FROM jobs WHERE identity=? AND key=? AND owner=?', (identity, key, owner))
                    processed += len(batch)
                    self.stats['embedded'] += len(batch)
            finally:
                with conn:
                    conn.execute('DELETE FROM jobs WHERE owner=?', (owner,))
            cached = self._cached(conn, identity, keyed)
            # Remove obsolete content only after a complete source inventory. Other
            # model identities are kept for rollback, never used by this query.
            if scope == '.' and not self.index.incomplete:
                with conn:
                    for key, in conn.execute('SELECT key FROM vectors WHERE identity=?', (identity,)).fetchall():
                        if key not in keyed:
                            conn.execute('DELETE FROM vectors WHERE identity=? AND key=?', (identity, key))
            self.stats.update(ready=len(cached), pending=len(keyed) - len(cached))
            return keyed, cached

    def rank(self, query, scope, check, remaining, *, build=True):
        keyed, cached = self.build(scope, check, remaining, enabled=build)
        if not cached:
            return [], not keyed
        query_vector = self._embed([query], check, remaining)[0]
        scores = []
        for key, vector in cached.items():
            check()
            score = sum(a * b for a, b in zip(query_vector, vector))
            if score > 0:
                scores.append((score, key, dict(keyed[key][0], vector_score=score)))
        return [doc for _, _, doc in sorted(scores, key=lambda row: (-row[0], row[1]))[:80]], len(cached) == len(keyed)
