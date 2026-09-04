"""Backward-compatible imports for admission-control policies."""

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
