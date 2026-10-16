from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any

from ...domain.exceptions import PolicyVersionConflict


class SQLitePolicyStore:
    """Durable policy CAS shared by local processes that explicitly use it."""

    def __init__(self, path: str | Path) -> None:
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = RLock()
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS current_policy (id INTEGER PRIMARY KEY CHECK (id=1), version INTEGER NOT NULL, config TEXT NOT NULL)")
        self._db.commit()

    def load(self) -> tuple[int, dict[str, Any]] | None:
        with self._lock:
            row = self._db.execute("SELECT version, config FROM current_policy WHERE id=1").fetchone()
        return (row[0], json.loads(row[1])) if row else None

    def save(self, version: int, config: dict[str, Any], *, expected_version: int) -> None:
        payload = json.dumps(config, allow_nan=False)
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute("SELECT version FROM current_policy WHERE id=1").fetchone()
            actual = row[0] if row else 0
            if actual != expected_version:
                raise PolicyVersionConflict("Policy version changed; reload before saving")
            self._db.execute("INSERT OR REPLACE INTO current_policy VALUES (1, ?, ?)", (version, payload))

    def close(self) -> None:
        with self._lock:
            self._db.close()
