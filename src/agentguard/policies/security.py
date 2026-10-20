from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import Any
from enum import Enum

from .content import ContentLimit, text_content

__all__ = ["SecurityAction", "SecurityFinding", "SecurityGuard", "SecurityResult"]


class SecurityAction(str, Enum):
    OBSERVE = "observe"
    BLOCK = "block"
    REDACT = "redact"


@dataclass(frozen=True)
class SecurityFinding:
    code: str
    severity: str = "high"


@dataclass(frozen=True)
class SecurityResult:
    passed: bool
    findings: tuple[SecurityFinding, ...] = ()
    action: str = SecurityAction.OBSERVE
    stage: str = "request"


class SecurityGuard:
    """Bounded, content-blind security checks for model requests and responses.

    The guard only returns rule codes and counts. It never includes matched text,
    prompt contents, or object representations in results or runtime events.
    """

    _secret_patterns = (
        ("secret.openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
        ("secret.github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b")),
        ("secret.google_key", re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b")),
        ("secret.bearer", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.I)),
        ("secret.assignment", re.compile(r"\b(?:api[_-]?key|secret|token|password)\s*[:=]\s*[^\s,;]{8,}", re.I)),
    )
    _pii_patterns = (
        ("pii.email", re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)),
        ("pii.phone", re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")),
    )
    _injection_patterns = (
        ("prompt_injection.ignore_instructions", re.compile(r"\b(ignore|disregard|forget)\s+(all\s+)?previous\s+instructions\b", re.I)),
        ("prompt_injection.system_prompt", re.compile(r"\b(system|developer)\s+prompt\b", re.I)),
        ("prompt_injection.jailbreak", re.compile(r"\b(jailbreak|do\s+anything\s+now|dan\s+mode)\b", re.I)),
    )

    def __init__(self, *, action: str = SecurityAction.BLOCK, detect_secrets: bool = True,
                 detect_pii: bool = False, detect_prompt_injection: bool = True,
                 max_chars: int = 200_000) -> None:
        if action not in {SecurityAction.OBSERVE, SecurityAction.BLOCK, SecurityAction.REDACT}:
            raise ValueError("security action must be observe, block or redact")
        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars <= 0:
            raise ValueError("max_chars must be a positive integer")
        self.action = action
        self.detect_secrets = detect_secrets
        self.detect_pii = detect_pii
        self.detect_prompt_injection = detect_prompt_injection
        self.max_chars = max_chars
        self._redaction_key = secrets.token_bytes(32)

    def inspect_request(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> SecurityResult:
        return self.inspect((args, kwargs), stage="request")

    def inspect_response(self, response: Any, *, stage: str = "response") -> SecurityResult:
        return self.inspect(response, stage=stage)

    def inspect(self, value: Any, *, stage: str = "request") -> SecurityResult:
        try:
            text = text_content(value, self.max_chars, include_keys=stage == "request" or self.action == "redact")
            from .redaction import structured_secret
            field_secret = self.detect_secrets and structured_secret(value)
        except ContentLimit:
            return SecurityResult(False, (SecurityFinding("security.scan_limit"),), self.action, stage)
        patterns: list[tuple[str, re.Pattern[str]]] = []
        if self.detect_secrets:
            patterns.extend(self._secret_patterns)
        if self.detect_pii:
            patterns.extend(self._pii_patterns)
        if self.detect_prompt_injection and stage == "request":
            patterns.extend(self._injection_patterns)
        findings = tuple(SecurityFinding(code) for code, pattern in patterns if pattern.search(text))
        if field_secret:
            findings += (SecurityFinding("secret.field"),)
        # One finding per rule keeps event cardinality bounded and deterministic.
        unique = tuple(dict.fromkeys(findings))
        return SecurityResult(not unique, unique, self.action, stage)

    def redact(self, value):
        from .redaction import redact
        patterns = list(self._secret_patterns) if self.detect_secrets else []
        if self.detect_pii:
            patterns.extend(self._pii_patterns)
        return redact(value, patterns=patterns, key=self._redaction_key,
                      max_chars=self.max_chars, detect_secrets=self.detect_secrets)
