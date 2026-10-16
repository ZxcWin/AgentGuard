"""Interfaces for optional infrastructure."""

from typing import Any, Protocol

from ..domain.models import RunState


class RunStore(Protocol):
    def save(self, state: RunState, config_version: int) -> None: ...


class PolicyStore(Protocol):
    def load(self) -> tuple[int, dict[str, Any]] | None: ...
    def save(self, version: int, config: dict[str, Any], *, expected_version: int) -> None: ...
