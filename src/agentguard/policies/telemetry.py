"""Deterministic ordinary-event sampling; never used for required audit."""
from dataclasses import dataclass, field
import hashlib
import math
from types import MappingProxyType
from typing import Mapping


def _rate(value):
    return type(value) in {int, float} and math.isfinite(value) and 0 <= value <= 1


@dataclass(frozen=True)
class EventSampling:
    success_rate: float = 1.0
    slow_seconds: float = 1.0
    tenant_rates: Mapping[str, float] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        if not _rate(self.success_rate) or type(self.slow_seconds) not in {int, float} or (
            not math.isfinite(self.slow_seconds) or not 0 < self.slow_seconds <= 86400
        ):
            raise ValueError("Invalid event sampling rate or slow threshold")
        if not isinstance(self.tenant_rates, Mapping) or len(self.tenant_rates) > 1000 or any(
            type(key) is not str or not 1 <= len(key) <= 128 or not _rate(value)
            for key, value in self.tenant_rates.items()
        ):
            raise ValueError("Invalid bounded tenant sampling rates")
        object.__setattr__(self, "tenant_rates", MappingProxyType(dict(self.tenant_rates)))

    def accepts(self, event, tenant_id=None):
        name = event["event"]
        if name.startswith("audit.") or "uncertain" in name or name.rsplit(".", 1)[-1] in {
            "failed", "rejected", "timed_out", "cancelled", "finding", "redacted", "retry_stopped"
        } or event.get("action") in {"block", "deny", "reject"} or event.get("outcome") in {
            "failed", "rejected", "denied", "timed_out", "cancelled"
        } or event.get("approval_outcome") in {"denied", "failed", "cancelled", "timed_out"}:
            return True
        duration = event.get("duration_seconds")
        if type(duration) in {int, float} and math.isfinite(duration) and duration >= self.slow_seconds:
            return True
        rate = self.tenant_rates.get(tenant_id if type(tenant_id) is str else None, self.success_rate)
        identity = event.get("trace_id") or event.get("run_id") or event.get("event_id")
        if not isinstance(identity, str) or not identity:
            return True
        return int.from_bytes(hashlib.sha256(identity.encode()).digest()[:8], "big") < rate * 2**64
