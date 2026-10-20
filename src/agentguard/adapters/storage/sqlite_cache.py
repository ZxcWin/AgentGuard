"""Disposable same-host exact cache with atomic capacity and bounded leases."""
from __future__ import annotations

import math
from pathlib import Path
import re
import secrets
import sqlite3
from threading import RLock
from time import time


class SQLiteExactCache:
    """WAL requires a local disk. This store does not encrypt cached payloads."""
    def __init__(self, path: str | Path, *, max_entries: int = 256,
                 max_bytes: int = 16 * 1024 * 1024, clock=time):
        if type(max_entries) is not int or not 1 <= max_entries <= 10000 or (
            type(max_bytes) is not int or not 1 <= max_bytes <= 256 * 1024 * 1024
        ):
            raise ValueError("Invalid cache capacity")
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self._entry_ceiling, self._byte_ceiling = max_entries, max_bytes
        self._clock, self._lock = clock, RLock()
        self._db = sqlite3.connect(str(path), timeout=.05, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("""CREATE TABLE IF NOT EXISTS exact_cache (
            key TEXT PRIMARY KEY, value BLOB NOT NULL, expires REAL NOT NULL, accessed REAL NOT NULL)""")
        self._db.execute("CREATE INDEX IF NOT EXISTS cache_expiry ON exact_cache(expires)")
        self._db.execute("CREATE INDEX IF NOT EXISTS cache_lru ON exact_cache(accessed,key)")
        self._db.execute("""CREATE TABLE IF NOT EXISTS cache_leases (
            key TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL)""")
        self._db.execute("""CREATE TABLE IF NOT EXISTS cache_metadata (
            id INTEGER PRIMARY KEY CHECK(id=1), generation TEXT NOT NULL, observed REAL NOT NULL)""")
        self._db.execute("INSERT OR IGNORE INTO cache_metadata VALUES (1,?,0)", (secrets.token_hex(32),))
        self._db.commit()

    @staticmethod
    def _key(key):
        if type(key) is not str or re.fullmatch(r"[a-f0-9]{64}", key) is None:
            raise ValueError("Cache keys must be SHA-256 hex digests")

    def _now(self):
        now = self._clock()
        if type(now) not in {int, float} or not math.isfinite(now) or now < 0:
            raise ValueError("Invalid cache clock")
        observed = self._db.execute("SELECT observed FROM cache_metadata WHERE id=1").fetchone()[0]
        if now < observed:
            raise ValueError("Cache clock moved backwards")
        self._db.execute("UPDATE cache_metadata SET observed=? WHERE id=1", (now,))
        return now

    def generation(self):
        with self._lock:
            return self._db.execute("SELECT generation FROM cache_metadata WHERE id=1").fetchone()[0]

    def get(self, key):
        self._key(key)
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            now = self._now()
            row = self._db.execute("SELECT value,expires FROM exact_cache WHERE key=?", (key,)).fetchone()
            if row is None:
                return None
            if row[1] <= now:
                self._db.execute("DELETE FROM exact_cache WHERE key=?", (key,))
                return None
            self._db.execute("UPDATE exact_cache SET accessed=? WHERE key=?", (now,key))
            return bytes(row[0])

    def set(self, key, value, *, ttl_seconds):
        self._key(key)
        if type(value) is not bytes or type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 3600:
            raise ValueError("Cache requires bytes and a TTL between 1 and 3600 seconds")
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            now = self._now()
            self._db.execute("DELETE FROM exact_cache WHERE expires<=? OR key=?", (now,key))
            if len(value) > self.max_bytes:
                return
            self._db.execute("INSERT INTO exact_cache VALUES (?,?,?,?)", (key,value,now+ttl_seconds,now))
            count, size = self._db.execute("SELECT COUNT(*),COALESCE(SUM(length(value)),0) FROM exact_cache").fetchone()
            if count > self.max_entries or size > self.max_bytes:
                oldest = self._db.execute("SELECT key,length(value) FROM exact_cache ORDER BY accessed,key").fetchall()
                for item, length in oldest:
                    if count <= self.max_entries and size <= self.max_bytes:
                        break
                    self._db.execute("DELETE FROM exact_cache WHERE key=?", (item,))
                    count, size = count - 1, size - length

    def delete(self, key):
        self._key(key)
        with self._lock, self._db:
            self._db.execute("DELETE FROM exact_cache WHERE key=?", (key,))

    def clear(self):
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            self._db.execute("DELETE FROM exact_cache")
            self._db.execute("DELETE FROM cache_leases")
            self._db.execute("UPDATE cache_metadata SET generation=? WHERE id=1", (secrets.token_hex(32),))

    def claim(self, key, *, lease_seconds=30):
        self._key(key)
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 60:
            raise ValueError("Invalid cache lease duration")
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            now = self._now()
            self._db.execute("DELETE FROM cache_leases WHERE expires<=?", (now,))
            if self._db.execute("SELECT COUNT(*) FROM cache_leases").fetchone()[0] >= 1024:
                return None
            owner = secrets.token_hex(16)
            inserted = self._db.execute("INSERT OR IGNORE INTO cache_leases VALUES (?,?,?)", (key,owner,now+lease_seconds))
            return owner if inserted.rowcount else None

    def release(self, key, owner):
        self._key(key)
        with self._lock, self._db:
            self._db.execute("DELETE FROM cache_leases WHERE key=? AND owner=?", (key,owner))

    def prune(self):
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            now = self._now()
            deleted = self._db.execute("DELETE FROM exact_cache WHERE expires<=?", (now,)).rowcount
            self._db.execute("DELETE FROM cache_leases WHERE expires<=?", (now,))
            return deleted

    def stats(self):
        with self._lock:
            count, size = self._db.execute("SELECT COUNT(*),COALESCE(SUM(length(value)),0) FROM exact_cache").fetchone()
            return {"entries": count, "bytes": size,
                    "leases": self._db.execute("SELECT COUNT(*) FROM cache_leases").fetchone()[0]}

    def configure(self, *, max_entries, max_bytes):
        if type(max_entries) is not int or not 1 <= max_entries <= 10000 or (
            type(max_bytes) is not int or not 1 <= max_bytes <= 256 * 1024 * 1024
        ):
            raise ValueError("Invalid cache capacity")
        with self._lock:
            self.max_entries = min(max_entries, self._entry_ceiling)
            self.max_bytes = min(max_bytes, self._byte_ceiling)

    def close(self):
        with self._lock:
            self._db.close()
