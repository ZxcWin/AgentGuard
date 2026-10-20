"""Scoped remote snapshots with finite expiry and persistent version fencing."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from threading import RLock
from time import monotonic, time
from typing import Any
from uuid import uuid4
from random import uniform

from .schema import AgentGuardConfig
from ..adapters.remote_policy import canonical
from ..domain.exceptions import PolicyUnavailable


@dataclass(frozen=True)
class RemoteSnapshot:
    version: int
    config: AgentGuardConfig
    valid: Any


class RemoteConfigManager:
    def __init__(self, provider, verifier, *, scope: dict[str, str], cache_path: str | Path,
                 refresh_seconds: float = 60, wall_clock=time, monotonic_clock=monotonic):
        if set(scope) != {"tenant_id", "project_id", "environment"} or any(not isinstance(v, str) or not 1 <= len(v) <= 128 for v in scope.values()):
            raise ValueError("Remote policy scope requires tenant_id, project_id and environment")
        if type(refresh_seconds) not in (int, float) or not 1 <= refresh_seconds <= 300:
            raise ValueError("Refresh interval must be 1 to 300 seconds")
        self.provider, self.verifier, self.scope = provider, verifier, dict(scope)
        self.path, self.refresh_seconds = Path(cache_path), refresh_seconds
        self.wall_clock, self.monotonic_clock = wall_clock, monotonic_clock
        self._lock = RLock()
        self._snapshot = None
        self._envelope = None
        self._version = 0
        self._refresh_at = 0.0
        self._last_wall = self.wall_clock()
        self._clock_failed = False
        self.failures = 0
        if self.path.exists():
            try:
                with self.path.open("rb") as stream: raw = stream.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024: raise ValueError()
                cached = json.loads(raw)
                self._install(cached, persist=False, recovery=self.verifier.verify(cached).get("recovery") is True)
            except Exception:
                # A corrupt/tampered cache cannot reset the high-water mark.
                raise PolicyUnavailable("Persisted remote policy is invalid; restore a trusted state file") from None

    def _clock(self):
        now = self.wall_clock()
        if now + 1 < self._last_wall:
            self._clock_failed = True
        self._last_wall = max(now, self._last_wall)
        if self._clock_failed:
            raise PolicyUnavailable("Clock moved backwards; remote admission is paused")
        return now

    def _install(self, envelope: dict, *, persist: bool, recovery: bool = False):
        payload = self.verifier.verify(envelope)
        if set(payload) != {"key_id", "version", "issued_at", "expires_at", "scope", "config", "recovery"}:
            raise PolicyUnavailable("Unsupported remote policy fields")
        if payload["scope"] != self.scope or type(payload["version"]) is not int or payload["version"] < 1:
            raise PolicyUnavailable("Policy scope or version mismatch")
        if payload["version"] < self._version:
            raise PolicyUnavailable("Remote policy rollback rejected")
        if type(payload["recovery"]) is not bool or payload["recovery"] != recovery:
            raise PolicyUnavailable("Recovery policies require an explicit recovery install")
        try:
            issued = datetime.fromisoformat(payload["issued_at"])
            expires = datetime.fromisoformat(payload["expires_at"])
            if issued.utcoffset() is None or expires.utcoffset() is None: raise ValueError()
            issued_at, expires_at = issued.timestamp(), expires.timestamp()
        except (ValueError, TypeError, OverflowError):
            raise PolicyUnavailable("Invalid policy timestamps") from None
        ttl = expires_at - issued_at
        now = self._clock()
        if not 60 <= ttl <= (300 if recovery else 3600) or issued_at > now + 5:
            raise PolicyUnavailable("Invalid remote policy lifetime")
        if persist and expires_at <= now:
            raise PolicyUnavailable("Expired policy cannot replace a live snapshot")
        if self._envelope is not None and payload["version"] == self._version:
            old = self.verifier.verify(self._envelope)
            if payload["config"] != old["config"] or payload["scope"] != old["scope"] or recovery != old["recovery"]:
                raise PolicyUnavailable("A policy version cannot change its content")
            # Same-version signed renewal is allowed, but existing Run leases remain pinned.
        config = AgentGuardConfig.from_mapping(payload["config"])
        deadline = self.monotonic_clock() + max(0, min(ttl, expires_at - now))
        def valid():
            with self._lock:
                if self._clock() >= expires_at or self.monotonic_clock() >= deadline:
                    raise PolicyUnavailable("Remote policy expired; new admission refused")
        if persist:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + f".{uuid4().hex}.tmp")
            try:
                with temporary.open("xb") as stream:
                    stream.write(canonical(envelope))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                if temporary.exists(): temporary.unlink()
        self._version, self._envelope = payload["version"], json.loads(canonical(envelope))
        self._snapshot = RemoteSnapshot(self._version, config, valid)

    def refresh(self):
        with self._lock:
            self._refresh_at = self.monotonic_clock() + self.refresh_seconds * uniform(.9, 1)
            try:
                self._install(self.provider.fetch(), persist=True)
            except Exception:
                self.failures += 1
                if self._snapshot is None:
                    raise PolicyUnavailable("No verified remote policy is available") from None
            if self._snapshot is not None:
                self._snapshot.valid()

    def snapshot(self):
        with self._lock:
            if self._snapshot is None or self.monotonic_clock() >= self._refresh_at:
                self.refresh()
            self._snapshot.valid()
            return self._snapshot

    @property
    def config(self):
        return self.snapshot().config

    def validate_scope(self, values):
        if any(values.get(key) != value for key, value in self.scope.items()):
            raise PolicyUnavailable("Run identity does not match the remote policy scope")

    def install_recovery(self, envelope, *, audit_store, actor: str):
        with self._lock:
            if self.verifier.verify(envelope).get("version", 0) <= self._version:
                raise PolicyUnavailable("Recovery must publish a strictly higher version")
            incoming = AgentGuardConfig.from_mapping(self.verifier.verify(envelope).get("config", {}))
            if self._snapshot is None or not recovery_tightens(self._snapshot.config, incoming):
                raise PolicyUnavailable("Offline recovery may only preserve or tighten known policy")
            from ..application.audit import AuditCoordinator
            audit = AuditCoordinator(audit_store)
            operation = audit.begin(action="policy.recovery", actor_id=actor, expected_version=self._version)
            try:
                self._install(envelope, persist=True, recovery=True)
                self._snapshot.valid()
            except Exception:
                audit.end(operation, outcome="failed", execution_may_have_completed=True)
                raise
            audit.end(operation, outcome="completed", target_version=self._version)

    def update(self, *args, **kwargs):
        raise PolicyUnavailable("Remote snapshots must be published by the policy authority")

    def reload(self):
        self.refresh()

    def status(self):
        with self._lock:
            return {"version": self._version, "refresh_failures": self.failures, "clock_failed": self._clock_failed, "scope": dict(self.scope)}


def recovery_tightens(old, new):
    """Conservative allowlist: lower caps only; other policy content must stay equal."""
    maxima = {
        "token": {"max_input_tokens", "max_output_tokens", "run_token_budget"},
        "budget": {"max_cost_usd", "run_cost_usd"},
        "tools": {"max_calls", "max_steps", "max_repeated_calls", "max_depth", "max_concurrent"},
        "timeout": {"run_seconds", "llm_seconds", "tool_seconds"},
        "rate_limit": {"rpm", "tpm", "max_concurrency", "max_tool_calls"},
        "behavior": {"max_repeated_outputs", "max_calls_without_progress"},
    }
    before, after = asdict(old), asdict(new)
    for group, value in before.items():
        if group not in maxima:
            if value != after[group]: return False
            continue
        for field, previous in value.items():
            current = after[group][field]
            if field in maxima[group]:
                if previous is not None and (current is None or current > previous): return False
            elif current != previous:
                return False
    return True
