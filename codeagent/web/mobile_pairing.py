"""Durable device identity and replay protection for the mobile link."""
import os
import sqlite3
import threading
from pathlib import Path


class PairingStore:
    def __init__(self, directory: Path | None = None):
        database = ":memory:"
        if directory is not None:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            database = str(directory / "pairing.sqlite3")
            os.close(os.open(database, os.O_CREAT | os.O_WRONLY, 0o600))
        self._protect = directory is not None and os.name == "nt"
        self._lock = threading.RLock()
        self._db = sqlite3.connect(database, check_same_thread=False)
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS device(slot INTEGER PRIMARY KEY CHECK(slot=1), secret BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS receipts(nonce BLOB PRIMARY KEY, expires REAL NOT NULL);
        """)
        self._cache = (None, None)

    def close(self):
        with self._lock:
            self._cache = (None, None)
            self._db.close()

    def read(self):
        with self._lock:
            row = self._db.execute("SELECT secret FROM device WHERE slot=1").fetchone()
            blob = row[0] if row else None
            if blob != self._cache[0]:
                key = blob
                if blob and self._protect:
                    import pywintypes
                    import win32crypt
                    try:
                        key = win32crypt.CryptUnprotectData(blob, None, None, None, 1)[1]
                    except pywintypes.error:
                        # A copied database from another Windows user needs a fresh pairing.
                        key = None
                self._cache = (blob, key)
            return self._cache[1]

    def replace(self, key):
        with self._lock, self._db:
            self._db.execute("DELETE FROM device")
            self._db.execute("DELETE FROM receipts")
            if key is not None:
                blob = key
                if self._protect:
                    import win32crypt
                    blob = win32crypt.CryptProtectData(key, "CodeWeft mobile pairing", None, None, None, 1)
                self._db.execute("INSERT INTO device VALUES (1, ?)", (blob,))
            self._cache = (None, None)

    def accept(self, key, nonce, now):
        # Acquire the SQLite write lock before checking the key. Rotation and
        # nonce admission must agree even if two workbenches share a data root.
        with self._lock, self._db:
            self._db.execute("DELETE FROM receipts WHERE expires < ?", (now,))
            if self.read() != key:
                return False
            inserted = self._db.execute("INSERT OR IGNORE INTO receipts VALUES (?, ?)", (nonce, now + 241))
            return inserted.rowcount == 1
