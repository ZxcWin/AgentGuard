import agentguard


def test_package_root_exports_remain_available():
    expected = {
        "AgentGuard",
        "AgentGuardConfig",
        "BudgetManager",
        "CircuitBreaker",
        "MemoryCache",
        "QualityGuard",
        "RateLimiter",
        "RunContext",
        "RunState",
        "ToolGuard",
        "Usage",
        "guard",
    }

    assert expected <= set(agentguard.__all__)
    for name in expected:
        assert getattr(agentguard, name) is not None


def test_legacy_modules_reexport_canonical_objects():
    from agentguard.api.runtime import AgentGuard as CanonicalAgentGuard
    from agentguard.budget import BudgetManager as LegacyBudgetManager
    from agentguard.cache import MemoryCache as LegacyMemoryCache
    from agentguard.domain.models import RunState as CanonicalRunState
    from agentguard.models import RunState as LegacyRunState
    from agentguard.policies.budget import BudgetManager as CanonicalBudgetManager
    from agentguard.adapters.storage.memory import MemoryCache as CanonicalMemoryCache
    from agentguard.runtime import AgentGuard as LegacyAgentGuard

    assert LegacyAgentGuard is CanonicalAgentGuard
    assert LegacyBudgetManager is CanonicalBudgetManager
    assert LegacyMemoryCache is CanonicalMemoryCache
    assert LegacyRunState is CanonicalRunState
