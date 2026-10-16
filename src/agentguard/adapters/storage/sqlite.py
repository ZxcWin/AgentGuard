from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any

from ...domain.models import RunState


# Only operational metadata crosses the storage boundary. Payloads are excluded.
EVENT_FIELDS = {"event", "timestamp", "config_version", "request_id", "trace_id", "run_id",
                "parent_id", "attempt", "estimated_tokens", "actual_tokens", "total_tokens",
                "limit", "reason_code", "action", "duration_seconds", "model", "usage_source", "outcome",
                "tool", "side_effect", "result_type", "result_size", "execution_may_have_completed", "delay_seconds",
                "from_model", "to_model", "trigger_reason", "idempotency_key_hash", "approval_required", "approval_outcome"}


class SQLiteRunStore:
    def __init__(self, path: str | Path) -> None:
        self._lock = RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("""CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, status TEXT NOT NULL,
            summary TEXT NOT NULL, events TEXT NOT NULL)""")
        self._db.execute("CREATE INDEX IF NOT EXISTS runs_started ON runs(started_at)")
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def save(self, state: RunState, config_version: int) -> None:
        events = [{key: value for key, value in item.items()
                   if key in EVENT_FIELDS and isinstance(value, (str, int, float, bool, type(None)))}
                  for item in state.events]
        for index, event in enumerate(events):
            event["id"] = f"{state.run_id}:{index}"
        summary = state.summary()
        summary["config_version"] = config_version
        summary["estimated_cost_usd"] = str(Decimal(str(summary["estimated_cost_usd"])))
        summary["ended_at"] = events[-1]["timestamp"] if state.status != "running" and events else None
        summary["duration_ms"] = (round((datetime.fromisoformat(summary["ended_at"]) - state.started_at).total_seconds() * 1000)
                                  if summary["ended_at"] else None)
        summary["blocked"] = sum(event.get("event", "").endswith(("rejected", "stream_stopped")) for event in events)
        summary["models"] = sorted({event["model"] for event in events if event.get("model")})
        summary["cost_known"] = not any(event.get("event") == "budget.price_missing" for event in events)
        sources = {event["usage_source"] for event in events if event.get("usage_source")}
        if len(sources) > 1:
            summary["usage_source"] = "mixed"
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO runs VALUES (?, ?, ?, ?, ?)", (
                state.run_id, state.started_at.isoformat(), state.status,
                json.dumps(summary), json.dumps(events)))

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT summary, events FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return {**json.loads(row[0]), "events": json.loads(row[1])} if row else None

    def list(self, *, hours: int = 24, status: str = "", search: str = "", page: int = 1,
             page_size: int = 12) -> dict[str, Any]:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        where, params = "started_at >= ?", [since]
        if status:
            where += " AND status = ?"
            params.append(status)
        if search:
            where += " AND (run_id LIKE ? ESCAPE '\\' OR json_extract(summary, '$.agent_name') LIKE ? ESCAPE '\\')"
            term = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            params.extend([term, term])
        with self._lock:
            total = self._db.execute(f"SELECT count(*) FROM runs WHERE {where}", params).fetchone()[0]
            rows = self._db.execute(f"SELECT summary FROM runs WHERE {where} ORDER BY started_at DESC, run_id DESC LIMIT ? OFFSET ?",
                                    [*params, page_size, (page - 1) * page_size]).fetchall()
        return {"items": [json.loads(row[0]) for row in rows], "total": total, "page": page, "page_size": page_size}

    def overview(self, hours: int, model: str = "") -> dict[str, Any]:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        with self._lock:
            rows = self._db.execute("SELECT summary FROM runs WHERE started_at >= ?", (since.isoformat(),)).fetchall()
        runs = [json.loads(row[0]) for row in rows]
        models = sorted({item for run in runs for item in run.get("models", [])})
        if model:
            runs = [run for run in runs if model in run.get("models", [])]
        buckets = [{"timestamp": (since + timedelta(hours=hours * index / 24)).isoformat(), "calls": 0, "tokens": 0, "failed": 0} for index in range(24)]
        sources = {source: 0 for source in ("provider", "estimate", "tokenizer", "unknown", "mixed")}
        agents: dict[str, dict[str, Any]] = {}
        for run in runs:
            index = min(23, max(0, int((datetime.fromisoformat(run["started_at"]) - since).total_seconds() / (hours * 150))))
            buckets[index]["calls"] += run["llm_calls"]
            buckets[index]["tokens"] += run["total_tokens"]
            buckets[index]["failed"] += run["status"] in {"failed", "timed_out"}
            sources[run["usage_source"]] += 1
            agent = agents.setdefault(run["agent_name"], {"name": run["agent_name"], "runs": 0, "tokens": 0, "cost": Decimal(0), "failed": 0})
            agent["runs"] += 1
            agent["tokens"] += run["total_tokens"]
            agent["cost"] += Decimal(run["estimated_cost_usd"])
            agent["failed"] += run["status"] in {"failed", "timed_out"}
        for agent in agents.values():
            agent["cost"] = str(agent["cost"])
        return {"hours": hours, "timezone": "UTC", "runs": len(runs),
                "calls": sum(run["llm_calls"] for run in runs),
                "tokens": sum(run["total_tokens"] for run in runs),
                "estimated_cost_usd": str(sum((Decimal(run["estimated_cost_usd"]) for run in runs), Decimal(0))),
                "failed": sum(run["status"] in {"failed", "timed_out"} for run in runs),
                "missing_price_runs": sum(not run.get("cost_known", False) for run in runs if run["llm_calls"]),
                "blocked": sum(run["blocked"] for run in runs), "sources": sources,
                "models": models, "series": buckets, "agents": sorted(agents.values(), key=lambda item: item["runs"], reverse=True)}
