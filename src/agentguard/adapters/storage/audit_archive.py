"""Durable deduplicating destination for audit outbox delivery."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from threading import RLock


class SQLiteAuditArchive:
    """Acknowledges emit only after FULL synchronous commit; finite archive retention."""
    def __init__(self, path: str | Path, *, retention_days: int = 180):
        if type(retention_days) is not int or not 30 <= retention_days <= 3650:
            raise ValueError("Archive retention must be between 30 and 3650 days")
        self.retention_days = retention_days
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._db = sqlite3.connect(str(path), timeout=2, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("CREATE TABLE IF NOT EXISTS audit_archive (event_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS audit_arrival (event_id TEXT PRIMARY KEY, received_at TEXT NOT NULL)")
        self._db.execute("INSERT OR IGNORE INTO audit_arrival SELECT event_id,? FROM audit_archive", (datetime.now(timezone.utc).isoformat(),))
        self._db.commit()

    def emit(self, event):
        identifier = event.get("event_id")
        if not isinstance(identifier, str) or not 1 <= len(identifier) <= 256:
            raise ValueError("Archive requires a stable event_id")
        payload = json.dumps(event, sort_keys=True, ensure_ascii=True, allow_nan=False)
        if len(payload) > 65536:
            raise ValueError("Audit archive record exceeds 64 KiB")
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute("SELECT payload FROM audit_archive WHERE event_id=?", (identifier,)).fetchone()
            if row:
                if row[0] != payload:
                    raise ValueError("Conflicting audit archive event ID")
                return
            self._db.execute("INSERT INTO audit_archive VALUES (?,?)", (identifier, payload))
            self._db.execute("INSERT INTO audit_arrival VALUES (?,?)", (identifier, datetime.now(timezone.utc).isoformat()))

    def prune(self):
        cutoff = (datetime.now(timezone.utc)-timedelta(days=self.retention_days)).isoformat()
        with self._lock, self._db:
            cursor = self._db.execute("DELETE FROM audit_archive WHERE event_id IN (SELECT event_id FROM audit_arrival WHERE received_at<?)", (cutoff,))
            self._db.execute("DELETE FROM audit_arrival WHERE received_at<?", (cutoff,))
            return cursor.rowcount

    def close(self):
        with self._lock:
            self._db.close()
