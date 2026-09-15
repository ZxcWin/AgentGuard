"""公共异常的向后兼容导入。"""

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
