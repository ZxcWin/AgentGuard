"""Durable local metadata outbox, independent of lossy ordinary telemetry."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any, Mapping
from uuid import uuid4

from ...domain.events import operational_event


class SQLiteAuditStore:
    def __init__(self, path: str | Path, *, max_bytes: int = 256 * 1024**2,
                 retention_days: int = 180, timeout: float = 2) -> None:
        if type(max_bytes) is not int or not 65536 <= max_bytes <= 4 * 1024**3:
            raise ValueError("max_bytes must be between 64 KiB and 4 GiB")
        if type(retention_days) is not int or not 30 <= retention_days <= 3650:
            raise ValueError("audit retention must be between 30 and 3650 days")
        if type(timeout) not in (int, float) or not 0 < timeout <= 30:
            raise ValueError("audit timeout must be between 0 and 30 seconds")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_bytes, self.retention_days = max_bytes, retention_days
        self._lock = RLock()
        self._db = sqlite3.connect(str(path), timeout=timeout, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("""CREATE TABLE IF NOT EXISTS audit_outbox (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
            operation_id TEXT NOT NULL, created_at TEXT NOT NULL, kind TEXT NOT NULL,
            payload TEXT NOT NULL, size_bytes INTEGER NOT NULL, delivered_at TEXT,
            lease_id TEXT, lease_until TEXT, attempts INTEGER NOT NULL DEFAULT 0)""")
        self._db.execute("CREATE INDEX IF NOT EXISTS audit_pending ON audit_outbox(delivered_at,lease_until,seq)")
        self._db.execute("CREATE INDEX IF NOT EXISTS audit_operation ON audit_outbox(operation_id)")
        self._db.execute("CREATE TABLE IF NOT EXISTS audit_size (id INTEGER PRIMARY KEY CHECK(id=1), bytes INTEGER NOT NULL)")
        self._db.execute("INSERT OR IGNORE INTO audit_size SELECT 1,COALESCE(SUM(size_bytes),0) FROM audit_outbox")
        self._db.execute("CREATE TRIGGER IF NOT EXISTS audit_size_insert AFTER INSERT ON audit_outbox BEGIN UPDATE audit_size SET bytes=bytes+NEW.size_bytes WHERE id=1; END")
        self._db.execute("CREATE TRIGGER IF NOT EXISTS audit_size_delete AFTER DELETE ON audit_outbox BEGIN UPDATE audit_size SET bytes=bytes-OLD.size_bytes WHERE id=1; END")
        self._db.commit()

    def append(self, event: Mapping[str, Any]) -> str:
        value = operational_event(event)
        identifier = event.get("event_id", f"audit_{uuid4().hex}")
        operation = event.get("operation_id", identifier)
        kind = event.get("kind", "event")
        if any(not isinstance(item, str) or not 1 <= len(item) <= 256 for item in (identifier, operation, kind)):
            raise ValueError("Invalid audit identifiers")
        # Restrict additional audit metadata to typed operational fields.
        for key in ("kind", "operation_id", "actor_id", "target_version", "expected_version", "execution_may_have_completed"):
            item = event.get(key)
            if type(item) is str:
                value[key] = item[:256]
            elif type(item) in (int, bool) and abs(int(item)) < 2**63:
                value[key] = item
        value.update(event_id=identifier, operation_id=operation, kind=kind, schema_version=1)
        # Caller timestamps cannot forge the retention time (created_at).
        now = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True)
        size = len(payload.encode())
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute("SELECT payload FROM audit_outbox WHERE event_id=?", (identifier,)).fetchone()
            if row:
                if row[0] != payload:
                    raise ValueError("Audit event ID reused with different content")
                return identifier
            used = self._db.execute("SELECT bytes FROM audit_size WHERE id=1").fetchone()[0]
            if used + size > self.max_bytes:
                raise OSError("Audit outbox capacity exhausted")
            self._db.execute("INSERT INTO audit_outbox(event_id,operation_id,created_at,kind,payload,size_bytes) VALUES(?,?,?,?,?,?)",
                             (identifier, operation, now, kind, payload, size))
        return identifier

    def claim(self, *, limit: int = 100, lease_seconds: int = 30) -> tuple[str, list[dict[str, Any]]]:
        if type(limit) is not int or not 1 <= limit <= 1000 or type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
            raise ValueError("Invalid audit delivery bounds")
        lease = uuid4().hex
        now = datetime.now(timezone.utc)
        until = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            rows = self._db.execute("SELECT event_id,payload FROM audit_outbox WHERE delivered_at IS NULL "
                "AND (lease_until IS NULL OR lease_until<=?) ORDER BY seq LIMIT ?", (now.isoformat(), limit)).fetchall()
            for identifier, _ in rows:
                self._db.execute("UPDATE audit_outbox SET lease_id=?,lease_until=?,attempts=attempts+1 WHERE event_id=?", (lease, until, identifier))
        return lease, [json.loads(row[1]) for row in rows]

    def acknowledge(self, lease: str, event_id: str) -> bool:
        with self._lock, self._db:
            row = self._db.execute("UPDATE audit_outbox SET delivered_at=?,lease_id=NULL,lease_until=NULL "
                "WHERE event_id=? AND lease_id=? AND delivered_at IS NULL",
                (datetime.now(timezone.utc).isoformat(), event_id, lease))
            return row.rowcount == 1

    def release(self, lease: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE audit_outbox SET lease_id=NULL,lease_until=NULL WHERE lease_id=? AND delivered_at IS NULL", (lease,))

    def reconcile(self, operation: str, *, outcome: str, actor: str) -> None:
        if outcome not in {"confirmed_completed", "confirmed_not_executed", "uncertain_recorded"}:
            raise ValueError("Invalid reconciliation outcome")
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            intent = self._db.execute("SELECT 1 FROM audit_outbox WHERE operation_id=? AND kind='intent'", (operation,)).fetchone()
            result = self._db.execute("SELECT 1 FROM audit_outbox WHERE operation_id=? AND kind='result'", (operation,)).fetchone()
            if not intent or result:
                raise ValueError("Operation is unknown or already has a result")
        self.append({"event": "audit.reconciled", "event_id": f"{operation}:result", "operation_id": operation,
                     "kind": "result", "outcome": outcome, "actor_id": actor,
                     "execution_may_have_completed": outcome != "confirmed_not_executed"})

    def deliver(self, sink, *, limit: int = 100) -> dict[str, int]:
        """At least once; consumer must deduplicate by event_id. Never replays operations."""
        lease, events = self.claim(limit=limit)
        delivered = 0
        try:
            for event in events:
                sink.emit(event)
                if not self.acknowledge(lease, event["event_id"]):
                    raise RuntimeError("Audit delivery lease lost")
                delivered += 1
        finally:
            self.release(lease)
        return {"delivered": delivered}

    def list(self, *, page: int = 1, page_size: int = 50, pending_only: bool = False) -> dict[str, Any]:
        if type(page) is not int or not 1 <= page <= 100000 or type(page_size) is not int or not 1 <= page_size <= 100:
            raise ValueError("Invalid pagination")
        where = "WHERE delivered_at IS NULL" if pending_only else ""
        with self._lock:
            total = self._db.execute(f"SELECT COUNT(*) FROM audit_outbox {where}").fetchone()[0]
            rows = self._db.execute(f"SELECT payload,created_at,delivered_at,attempts FROM audit_outbox {where} ORDER BY seq DESC LIMIT ? OFFSET ?",
                                    (page_size, (page-1)*page_size)).fetchall()
        return {"items": [{**json.loads(row[0]), "recorded_at": row[1], "delivered_at": row[2], "attempts": row[3]} for row in rows],
                "total": total, "page": page, "page_size": page_size}

    def stats(self) -> dict[str, Any]:
        with self._lock:
            row = self._db.execute("SELECT COUNT(*),COALESCE(SUM(delivered_at IS NULL),0),COALESCE(SUM(size_bytes),0),COALESCE(MAX(attempts),0) FROM audit_outbox").fetchone()
            unresolved = self._db.execute("SELECT COUNT(*) FROM audit_outbox i WHERE i.kind='intent' AND NOT EXISTS "
                "(SELECT 1 FROM audit_outbox f WHERE f.operation_id=i.operation_id AND f.kind='result')").fetchone()[0]
        return dict(records=row[0], pending=row[1], bytes=row[2], max_bytes=self.max_bytes, max_attempts=row[3],
                    unresolved_operations=unresolved, retention_days=self.retention_days, synchronous="FULL")

    def prune(self) -> int:
        cutoff = (datetime.now(timezone.utc)-timedelta(days=self.retention_days)).isoformat()
        # Undelivered and uncertain operations are never automatically erased.
        with self._lock, self._db:
            row = self._db.execute("DELETE FROM audit_outbox WHERE created_at<? AND delivered_at IS NOT NULL "
                "AND operation_id NOT IN (SELECT i.operation_id FROM audit_outbox i WHERE i.kind='intent' AND NOT EXISTS "
                "(SELECT 1 FROM audit_outbox f WHERE f.operation_id=i.operation_id AND f.kind='result'))", (cutoff,))
            return row.rowcount

    def close(self) -> None:
        with self._lock:
            self._db.close()
