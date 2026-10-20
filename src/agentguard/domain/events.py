"""Versioned operational event envelope; payloads never cross this boundary."""
from __future__ import annotations

import math
import re
from typing import Any, Mapping

EVENT_SCHEMA_VERSION = 1
EVENT_FAMILIES = frozenset({"run", "llm", "tool", "budget", "rate_limit", "quality", "security", "cache", "retry", "circuit", "usage", "fallback", "timeout", "audit", "policy", "behavior"})
EVENT_FIELDS = frozenset({"event", "timestamp", "config_version", "request_id", "trace_id", "run_id",
    "parent_id", "attempt", "estimated_tokens", "actual_tokens", "total_tokens", "limit", "reason_code",
    "action", "duration_seconds", "model", "usage_source", "outcome", "tool", "side_effect", "result_type",
    "result_size", "execution_may_have_completed", "delay_seconds", "from_model", "to_model", "trigger_reason",
    "idempotency_key_hash", "approval_required", "approval_outcome", "stage", "finding_count",
    "requested_action", "evaluator_version", "event_id", "schema_version", "estimated_cost_usd",
    "kind", "operation_id", "actor_id", "target_version", "expected_version", "reservation_id"})


def operational_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Copy bounded scalar metadata only. Names/identifiers must not contain secrets."""
    result: dict[str, Any] = {}
    for key in EVENT_FIELDS:
        value = event.get(key)
        if value is None or type(value) is bool:
            result[key] = value
        elif type(value) is int and -(2**63) <= value < 2**63:
            result[key] = value
        elif type(value) is float and math.isfinite(value) and abs(value) <= 1e100:
            result[key] = value
        elif type(value) is str:
            result[key] = value[:256]
    name = result.get("event", "")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_.]*", name) or name.split(".")[0] not in EVENT_FAMILIES:
        result["event"] = "run.custom"
    result["schema_version"] = EVENT_SCHEMA_VERSION
    return {key: value for key, value in result.items() if key in event or key in {"schema_version", "event"}}
