from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

__all__ = ["QualityAction", "QualityGuard", "QualityResult"]


class QualityAction(str, Enum):
    PASS = "pass"
    RETRY = "retry"
    DEGRADE = "degrade"
    HUMAN = "human"
    BLOCK = "block"
    OBSERVE = "observe"


@dataclass(frozen=True)
class QualityResult:
    passed: bool
    score: float | None = None
    reason: str | None = None
    action: QualityAction = QualityAction.PASS


class QualityGuard:
    def __init__(self, min_similarity: float = 0.7, strict_json: bool = False) -> None:
        if not 0 <= min_similarity <= 1:
            raise ValueError("min_similarity must be between 0 and 1")
        self.min_similarity = min_similarity
        self.strict_json = strict_json
        self._evaluators: list[Callable[[Any], QualityResult]] = []

    def add_evaluator(self, evaluator: Callable[[Any], QualityResult]) -> None:
        self._evaluators.append(evaluator)

    def evaluate(self, response: Any) -> QualityResult:
        content = self._content(response)
        if self.strict_json:
            try:
                json.loads(content)
            except (TypeError, ValueError):
                return QualityResult(False, reason="response is not valid JSON", action=QualityAction.RETRY)
        for evaluator in self._evaluators:
            result = evaluator(response)
            if not result.passed:
                return result
        return QualityResult(True, score=1.0)

    @staticmethod
    def _content(response: Any) -> Any:
        if isinstance(response, str):
            return response
        if isinstance(response, dict):
            return response.get("content", response)
        choices = getattr(response, "choices", None)
        if choices:
            message = getattr(choices[0], "message", None)
            if message is not None:
                return getattr(message, "content", message)
        return getattr(response, "content", response)
