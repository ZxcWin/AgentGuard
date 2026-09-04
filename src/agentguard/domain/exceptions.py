class AgentGuardError(Exception):
    """Base exception for AgentGuard errors."""


class ConfigurationError(AgentGuardError, ValueError):
    """Raised when a configuration value is invalid."""


class BudgetExceeded(AgentGuardError):
    """Raised when a token or cost budget cannot cover a request."""

    def __init__(self, message: str, *, reason_code: str = "budget_exceeded", retryable: bool = False) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.retryable = retryable


class RateLimitExceeded(AgentGuardError):
    """Raised when a request cannot enter the configured rate limit."""


class CircuitOpen(AgentGuardError):
    """Raised when a circuit breaker is open."""


__all__ = [
    "AgentGuardError",
    "BudgetExceeded",
    "CircuitOpen",
    "ConfigurationError",
    "RateLimitExceeded",
]
