"""Versioned HTTP DTOs; these optional dependencies never enter the SDK core."""
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int


class HealthView(BaseModel):
    status: Literal["ok"]
    api_version: str
    schema_version: int
    sdk_version: str
    demo: bool
    storage: str
    policy_scope: str
    policy_persistence: str
    authentication: str = "local"
    cache_backend: str = "memory"
    cache_scope: str = "local_process"


class EventView(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    event: str
    timestamp: str


class RunView(BaseModel):
    run_id: str
    trace_id: str
    agent_name: str
    status: Literal["running", "completed", "failed", "cancelled", "timed_out"]
    started_at: str
    ended_at: str | None
    duration_ms: int | None
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    reasoning_tokens: int
    total_tokens: int
    estimated_cost_usd: str
    usage_source: str
    llm_calls: int
    tool_calls: int
    retry_count: int
    config_version: int
    blocked: int
    models: list[str] = Field(default_factory=list)
    cost_known: bool = False
    user_id: str | None = None
    tenant_id: str | None = None
    project_id: str | None = None
    environment: str | None = None
    agent_id: str | None = None


class RunDetail(RunView):
    events: list[EventView]
    events_total: int


class SeriesPoint(BaseModel):
    timestamp: str
    calls: int
    tokens: int
    failed: int


class AgentView(BaseModel):
    name: str
    runs: int
    tokens: int
    cost: str
    failed: int


class OverviewView(BaseModel):
    hours: int
    timezone: str
    runs: int
    calls: int
    tokens: int
    estimated_cost_usd: str
    failed: int
    missing_price_runs: int
    blocked: int
    sources: dict[str, int]
    models: list[str]
    series: list[SeriesPoint]
    agents: list[AgentView]


class PolicyView(BaseModel):
    version: int
    config: dict[str, dict[str, Any]]
    editable: dict[str, list[str]]
    scope: str
    persistence: str
