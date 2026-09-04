"""Backward-compatible imports for public exceptions."""

from .domain.exceptions import (
    AgentGuardError,
    BudgetExceeded,
    CircuitOpen,
    ConfigurationError,
    RateLimitExceeded,
)

__all__ = [
    "AgentGuardError",
    "BudgetExceeded",
    "CircuitOpen",
    "ConfigurationError",
    "RateLimitExceeded",
]
