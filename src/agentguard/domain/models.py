from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

__all__ = ["RunState", "Usage", "utc_now"]
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    source: str = "unknown"

    def __post_init__(self) -> None:
        values = (self.input_tokens, self.output_tokens, self.cached_tokens, self.reasoning_tokens)
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
            raise ValueError("usage token values must be non-negative integers")
        if self.source not in {"provider", "tokenizer", "estimate", "unknown"}:
            raise ValueError("usage source must be provider, tokenizer, estimate, or unknown")

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class RunState:
    name: str = "agent_run"
    user_id: str | None = None
    tenant_id: str | None = None
    project_id: str | None = None
    environment: str | None = None
    agent_id: str | None = None
    run_id: str = field(default_factory=lambda: f"run_{uuid4().hex}")
    started_at: datetime = field(default_factory=utc_now)
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    usage_source: str = "unknown"
    estimated_cost_usd: float = 0.0
    llm_calls: int = 0
    tool_calls: int = 0
    retry_count: int = 0
    status: str = "running"
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add_usage(self, usage: Usage, cost_usd: float = 0.0) -> None:
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cached_tokens += usage.cached_tokens
        self.reasoning_tokens += usage.reasoning_tokens
        source_priority = {"unknown": 0, "estimate": 1, "tokenizer": 2, "provider": 3}
        if source_priority[usage.source] > source_priority[self.usage_source]:
            self.usage_source = usage.source
        self.estimated_cost_usd += cost_usd

    def record(self, event: str, **data: Any) -> None:
        self.events.append({"event": event, "timestamp": utc_now().isoformat(), **data})

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "agent_name": self.name,
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "project_id": self.project_id,
            "environment": self.environment,
            "agent_id": self.agent_id,
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost_usd": round(self.estimated_cost_usd, 8),
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
            "retry_count": self.retry_count,
            "usage_source": self.usage_source,
        }
