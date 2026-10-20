from __future__ import annotations

import json
import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable
from .content import ContentLimit, text_content

__all__ = ["QualityAction", "QualityGuard", "QualityResult"]


class QualityAction(str, Enum):
    PASS = "pass"
    RETRY = "retry"
    REPAIR = "repair"
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
    version = "builtin-1"

    def __init__(self, min_similarity: float = 0.7, strict_json: bool = False, *,
                 action: QualityAction | str = QualityAction.BLOCK, max_chars: int = 200_000,
                 json_schema=None, max_retries: int = 0, min_chars: int = 0,
                 required_substrings: tuple[str, ...] = (), handler_timeout_seconds: float = 30.0) -> None:
        if not 0 <= min_similarity <= 1:
            raise ValueError("min_similarity 必须介于 0 和 1 之间")
        self.min_similarity = min_similarity
        self.strict_json = strict_json
        if not isinstance(action, str) or action not in set(QualityAction) - {QualityAction.PASS}:
            raise ValueError("invalid quality action")
        if type(handler_timeout_seconds) not in {int, float} or not 0 < handler_timeout_seconds <= 300:
            raise ValueError("quality handler timeout must be between 0 and 300 seconds")
        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars <= 0:
            raise ValueError("max_chars must be a positive integer")
        self.action = QualityAction(action)
        self.max_chars = max_chars
        if type(min_chars) is not int or not 0 <= min_chars <= max_chars:
            raise ValueError("min_chars must be between 0 and max_chars")
        if type(max_retries) is not int or not 0 <= max_retries <= 10:
            raise ValueError("max_retries must be between 0 and 10")
        if not isinstance(required_substrings, (list, tuple)) or len(required_substrings) > 32 or any(
            not isinstance(item, str) or not 1 <= len(item) <= 256 for item in required_substrings
        ):
            raise ValueError("required_substrings must be bounded nonempty strings")
        self.min_chars = min_chars
        self.required_substrings = tuple(required_substrings)
        self._validator = None
        if json_schema is not None:
            from ..adapters.validation.json_schema import build_validator
            self._validator = build_validator(json_schema)
        self._evaluators: list[Callable[[Any], QualityResult]] = []

    def add_evaluator(self, evaluator: Callable[[Any], QualityResult]) -> None:
        self._evaluators.append(evaluator)

    def evaluate(self, response: Any) -> QualityResult:
        try:
            content = text_content(self._content(response), self.max_chars)
        except ContentLimit:
            return QualityResult(False, reason="content_limit", action=self.action)
        if len(content) < self.min_chars or any(item not in content for item in self.required_substrings):
            return QualityResult(False, reason="content_rule", action=self.action)
        if self.strict_json or self._validator is not None:
            try:
                parsed = json.loads(content, parse_constant=_invalid_constant)
                if self._validator is not None and not self._validator.is_valid(parsed):
                    return QualityResult(False, reason="schema_failed", action=self.action)
            except (TypeError, ValueError, RecursionError):
                return QualityResult(False, reason="invalid_json", action=self.action)
        for evaluator in self._evaluators:
            result = evaluator(response)
            if not isinstance(result, QualityResult) or type(result.passed) is not bool or (
                result.score is not None and (isinstance(result.score, bool) or
                not isinstance(result.score, (int, float)) or not math.isfinite(result.score) or
                not 0 <= result.score <= 1)
            ) or result.action not in set(QualityAction):
                raise ValueError("invalid quality evaluator result")
            if not result.passed:
                return result
        return QualityResult(True, score=1.0)

    @staticmethod
    def _content(response: Any) -> Any:
        if isinstance(response, str):
            return response
        if isinstance(response, dict):
            choices = response.get("choices")
            if choices:
                message = choices[0].get("message", choices[0].get("delta", {}))
                return message.get("content", "")
            return response.get("content", response)
        choices = getattr(response, "choices", None)
        if choices:
            message = getattr(choices[0], "message", None)
            if message is not None:
                return getattr(message, "content", message)
        return getattr(response, "content", response)


def _invalid_constant(value: str) -> None:
    raise ValueError("non-finite JSON constant")
