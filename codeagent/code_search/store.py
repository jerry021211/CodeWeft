"""Versioned SQLite storage. No parsing or provider calls inside write transactions."""
import json
import sqlite3
from .chunks import encoded

SOURCE_DATABASE = 'source-v2.sqlite3'


def connect(directory, workspace):
    directory.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(directory / SOURCE_DATABASE, timeout=.25, check_same_thread=False)
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('BEGIN IMMEDIATE')
        with conn:
            conn.execute('CREATE TABLE IF NOT EXISTS identity (workspace TEXT)')
            row = conn.execute('SELECT workspace FROM identity').fetchone()
            if row and row[0] != str(workspace):
                raise sqlite3.DatabaseError('Index belongs to a different workspace')
            if not row:
                conn.execute('INSERT INTO identity VALUES (?)', (str(workspace),))
            conn.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER)')
            conn.execute("INSERT OR IGNORE INTO meta VALUES ('generation',0)")
            conn.execute('CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, hash TEXT, parser TEXT)')
            conn.execute('CREATE TABLE IF NOT EXISTS chunks (id INTEGER PRIMARY KEY, path TEXT, payload TEXT)')
            conn.execute('CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path)')
            conn.execute('CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(symbol,path,comments,body)')
        return conn
    except BaseException:
        conn.close()
        raise


def replace_file(conn, relative, digest=None, parser=None, documents=()):
    conn.execute('DELETE FROM search WHERE rowid IN (SELECT id FROM chunks WHERE path=?)', (relative,))
    conn.execute('DELETE FROM chunks WHERE path=?', (relative,))
    conn.execute('DELETE FROM files WHERE path=?', (relative,))
    if digest is None:
        return
    conn.execute('INSERT INTO files VALUES (?,?,?)', (relative, digest, parser))
    for doc in documents:
        row = conn.execute('INSERT INTO chunks(path,payload) VALUES (?,?)', (relative, json.dumps(doc, ensure_ascii=False)))
        conn.execute('INSERT INTO search(rowid,symbol,path,comments,body) VALUES (?,?,?,?,?)',
                     (row.lastrowid, encoded(doc['symbol'] + ' ' + doc.get('search_signature', doc['signature'])), encoded(relative),
                      encoded(doc['comments']), encoded(doc['body'])))
