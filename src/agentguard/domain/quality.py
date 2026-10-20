"""Application-owned quality recovery contracts; payloads are never event fields."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping


@dataclass(frozen=True, repr=False)
class QualityRequest:
    """Replacement request to the same guarded provider, never a trusted response."""
    args: tuple[Any, ...] = ()
    kwargs: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, repr=False)
class QualityFailure:
    response: Any
    request: QualityRequest
    run_id: str
    request_id: str
    attempt: int
    action: str


@dataclass(frozen=True, repr=False)
class QualityDecision:
    """A human authorizes one replacement request, which must pass every gate."""
    approved: bool
    actor_id: str
    request: QualityRequest | None = None


@dataclass(frozen=True)
class QualityRecovery:
    """Pure request repair and application-owned human approval callbacks."""
    repair: Callable[[QualityFailure], Any] | None = None
    human: Callable[[QualityFailure], Any] | None = None

    def __post_init__(self):
        if any(value is not None and not callable(value) for value in (self.repair, self.human)):
            raise ValueError("quality recovery handlers must be callable")
