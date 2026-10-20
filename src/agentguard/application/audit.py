"""Fail-closed audit before protected operations; latch incomplete result writes."""
from __future__ import annotations

from threading import RLock
from typing import Any
from uuid import uuid4

from ..domain.exceptions import AuditUnavailable
from ..ports import AuditStore


class AuditCoordinator:
    def __init__(self, store: AuditStore):
        self.store = store
        self._lock = RLock()
        self.blocked = False
        if hasattr(store, "stats"):
            try:
                self.blocked = bool(store.stats()["unresolved_operations"])
            except Exception:
                self.blocked = True

    def check(self) -> None:
        with self._lock:
            if self.blocked:
                raise AuditUnavailable()

    def begin(self, *, action: str, **fields: Any) -> str:
        with self._lock:
            self.check()
            operation = f"op_{uuid4().hex}"
            self._write(dict(fields, event="audit.intent", kind="intent", operation_id=operation,
                             action=action, event_id=f"{operation}:intent"), completed=False)
            return operation

    def decision(self, *, action: str, outcome: str, **fields: Any) -> None:
        """Durable decision metadata, with no business operation to replay."""
        with self._lock:
            self.check()
            identifier = f"decision_{uuid4().hex}"
            self._write(dict(fields, event="audit.decision", kind="decision", operation_id=identifier,
                             action=action, outcome=outcome, event_id=identifier), completed=False)

    def end(self, operation: str, *, outcome: str, **fields: Any) -> None:
        with self._lock:
            self._write(dict(fields, event="audit.result", kind="result", operation_id=operation,
                             outcome=outcome, event_id=f"{operation}:result"), completed=True)

    def _write(self, event, *, completed: bool):
        try:
            self.store.append(event)
        except Exception:
            self.blocked = True
            raise AuditUnavailable(execution_may_have_completed=completed, operation_id=event["operation_id"]) from None

    def recover(self) -> None:
        """Explicit operator recovery after storage repair; never replays uncertain work."""
        with self._lock:
            try:
                unresolved = hasattr(self.store, "stats") and self.store.stats()["unresolved_operations"]
            except Exception:
                self.blocked = True
                raise AuditUnavailable() from None
            if unresolved:
                raise AuditUnavailable()
            operation = f"recovery_{uuid4().hex}"
            self._write({"event": "audit.recovered", "kind": "recovery", "operation_id": operation,
                         "event_id": operation}, completed=False)
            self.blocked = False
