class AgentGuardError(Exception):
    """AgentGuard 异常的基类。"""


class AuditUnavailable(AgentGuardError):
    """Required audit failed; new protected operations must stop."""
    def __init__(self, *, execution_may_have_completed: bool = False, operation_id: str | None = None):
        self.execution_may_have_completed = execution_may_have_completed
        self.operation_id = operation_id
        self.reason_code = "audit_unavailable"
        super().__init__("Required audit storage is unavailable")


class PolicyUnavailable(AgentGuardError):
    """No valid, scoped remote policy is available for admission."""


class ConfigurationError(AgentGuardError, ValueError):
    """配置值无效时抛出。"""


class PolicyVersionConflict(AgentGuardError, ValueError):
    """A policy update lost its compare-and-swap race."""


class BudgetExceeded(AgentGuardError):
    """Token 或成本预算无法覆盖请求时抛出。"""

    def __init__(self, message: str, *, reason_code: str = "budget_exceeded", retryable: bool = False) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.retryable = retryable


class RateLimitExceeded(AgentGuardError):
    """请求无法进入配置的限流窗口时抛出。"""


class CircuitOpen(AgentGuardError):
    """熔断器处于打开状态时抛出。"""


class ExecutionTimeout(AgentGuardError, TimeoutError):
    """A configured monotonic execution deadline was reached."""

    def __init__(self, scope: str) -> None:
        self.scope = scope
        self.reason_code = f"{scope}_timeout"
        super().__init__(f"{scope} execution deadline exceeded")


class QualityViolation(AgentGuardError):
    """A configured quality gate rejected a model response."""

    def __init__(self, reason: str | None = None, *, action: str = "block") -> None:
        self.action = action
        self.reason_code = "quality_failed"
        super().__init__(reason or "model response failed quality checks")


class SecurityViolation(AgentGuardError):
    """A configured security gate rejected model input or output."""

    def __init__(self, *, stage: str, finding_count: int) -> None:
        self.stage = stage
        self.finding_count = finding_count
        self.reason_code = "security_blocked"
        super().__init__(f"security policy blocked {stage} content")


__all__ = [
    "AgentGuardError",
    "BudgetExceeded",
    "CircuitOpen",
    "ConfigurationError",
    "ExecutionTimeout",
    "RateLimitExceeded",
    "QualityViolation",
    "SecurityViolation",
    "AuditUnavailable",
    "PolicyUnavailable",
]
