from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

__all__ = ["CallContext", "RequestSnapshot", "RunState", "Usage", "utc_now"]
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
        if any(isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10**12 for value in values):
            raise ValueError("usage 的 Token 数必须是非负整数")
        if self.cached_tokens > self.input_tokens or self.reasoning_tokens > self.output_tokens:
            raise ValueError("Cached/reasoning tokens must be subsets of input/output tokens")
        if self.source not in {"provider", "tokenizer", "estimate", "unknown"}:
            raise ValueError("usage 来源必须是 provider、tokenizer、estimate 或 unknown")

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class CallContext:
    """一次模型或工具调用的稳定关联信息。"""

    call_type: str = "llm"
    request_id: str = field(default_factory=lambda: f"req_{uuid4().hex}")
    trace_id: str = field(default_factory=lambda: f"trace_{uuid4().hex}")
    run_id: str = ""
    parent_id: str | None = None
    provider: str | None = None
    model: str | None = None
    attempt: int = 1
    idempotency_key: str | None = None


@dataclass(frozen=True)
class RequestSnapshot:
    """不持有供应商对象的标准化请求快照。"""

    model: str | None = None
    messages: Any = None
    parameters: dict[str, Any] = field(default_factory=dict)
    stream: bool = False
    data_classification: str | None = None


@dataclass
class RunState:
    name: str = "agent_run"
    user_id: str | None = None
    tenant_id: str | None = None
    project_id: str | None = None
    environment: str | None = None
    agent_id: str | None = None
    run_id: str = field(default_factory=lambda: f"run_{uuid4().hex}")
    trace_id: str = field(default_factory=lambda: f"trace_{uuid4().hex}")
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
    event_observer: Callable[[dict[str, Any]], None] | None = field(default=None, repr=False, compare=False)

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
        value = {**data, "event": event, "timestamp": utc_now().isoformat(),
                 "event_id": f"evt_{uuid4().hex}", "schema_version": 1,
                 "run_id": self.run_id, "trace_id": self.trace_id}
        self.events.append(value)
        if self.event_observer is not None:
            try:
                self.event_observer(dict(value))
            except Exception:
                # Ordinary telemetry must never change the result of a protected call.
                pass

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "trace_id": self.trace_id,
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
