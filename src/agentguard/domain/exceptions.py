class AgentGuardError(Exception):
    """AgentGuard 异常的基类。"""


class ConfigurationError(AgentGuardError, ValueError):
    """配置值无效时抛出。"""


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


__all__ = [
    "AgentGuardError",
    "BudgetExceeded",
    "CircuitOpen",
    "ConfigurationError",
    "RateLimitExceeded",
]
