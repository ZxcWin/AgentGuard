"""准入控制策略的向后兼容导入。"""

from .policies.rate_limit import (
    AdmissionController,
    AdmissionLease,
    CircuitBreaker,
    CircuitRegistry,
    ConcurrencyLimiter,
    RateLimiter,
    SlidingWindowLimiter,
)

__all__ = [
    "CircuitBreaker",
    "CircuitRegistry",
    "AdmissionController",
    "AdmissionLease",
    "ConcurrencyLimiter",
    "RateLimiter",
    "SlidingWindowLimiter",
]
