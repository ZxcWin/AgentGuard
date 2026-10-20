"""Runtime gates emit operational metadata only, never evaluator prose."""
from __future__ import annotations

from time import monotonic
from collections.abc import Iterator, AsyncIterator
from typing import Any

from ..domain.exceptions import QualityViolation, SecurityViolation
from ..policies.security import SecurityResult
from ..policies.quality import QualityAction, QualityResult


def security_audit(context, metadata, *, outcome, stage, reason):
    if context.audit is not None:
        context.audit.decision(action="security.check", outcome=outcome, stage=stage, reason_code=reason,
                               request_id=metadata.request_id, run_id=context.state.run_id,
                               config_version=context.config_version)


def content_limit(context: Any, metadata: Any, kind: str, stage: str) -> None:
    guard = getattr(context, f"{kind}_guard")
    requested = str(getattr(guard.action, "value", guard.action))
    action = "block" if requested in {"retry", "repair", "degrade", "human", "redact"} else requested
    context.state.record(f"{kind}.rejected" if action == "block" else f"{kind}.finding",
                         request_id=metadata.request_id, stage=stage,
                         reason_code=f"{kind}.content_limit", action=action, requested_action=requested)
    if action == "block":
        if kind == "security":
            security_audit(context, metadata, outcome="blocked", stage=stage, reason="security.content_limit")
            raise SecurityViolation(stage=stage, finding_count=0)
        raise QualityViolation(action=requested)


def check_security(context: Any, metadata: Any, value: Any, *, stage: str) -> Any:
    guard = context.security_guard
    started = monotonic()
    try:
        if guard.action == "redact" and stage == "response" and isinstance(value, (Iterator, AsyncIterator)):
            raise ValueError("redaction requires a non-streaming response")
        result = guard.inspect_request(*value) if stage == "request" else guard.inspect_response(value, stage=stage)
        if not isinstance(result, SecurityResult) or type(result.passed) is not bool or result.action not in {"block", "observe", "redact"}:
            raise ValueError("invalid security result")
        codes = {finding.code for finding in result.findings}
        if result.passed and codes:
            raise ValueError("inconsistent security result")
    except Exception:
        # Checker failure is always fail-closed, even in observe mode.
        security_audit(context, metadata, outcome="blocked", stage=stage, reason="security_check_failed")
        context.state.record("security.rejected", request_id=metadata.request_id, stage=stage,
                             reason_code="security_check_failed", action="block", finding_count=0,
                             duration_seconds=monotonic() - started)
        raise SecurityViolation(stage=stage, finding_count=0) from None
    if result.passed:
        return value
    known = {code for code, _ in (*guard._secret_patterns, *guard._pii_patterns, *guard._injection_patterns)} | {"security.scan_limit", "secret.field"}
    safe_codes = sorted({code if code in known else "security.custom_finding" for code in codes}) or ["security.custom_finding"]
    action = str(getattr(result.action, "value", result.action))
    replacements = 0
    if action == "redact":
        action = "block"
        if stage != "stream" and codes and all(type(code) is str and code.startswith(("secret.", "pii.")) and code in known for code in codes):
            try:
                converted, replacements = guard.redact(value)
                if replacements > 0 and guard.inspect(converted, stage=stage).passed:
                    value, action = converted, "redact"
            except Exception:
                pass
    security_audit(context, metadata, outcome=action, stage=stage, reason="security_finding")
    for code in safe_codes:
        context.state.record("security.finding",
                             request_id=metadata.request_id, stage=stage, reason_code=code,
                             finding_count=len(safe_codes), action=action,
                             duration_seconds=monotonic() - started)
    if action == "redact":
        context.state.record("security.redacted", request_id=metadata.request_id, stage=stage,
                             finding_count=replacements, action="redact")
    if action == "block":
        context.state.record("security.rejected", request_id=metadata.request_id, stage=stage,
                             reason_code="security_blocked", finding_count=len(safe_codes), action="block")
        raise SecurityViolation(stage=stage, finding_count=len(safe_codes))
    return value


def check_quality(context: Any, metadata: Any, response: Any, *, stage: str = "response") -> None:
    started = monotonic()
    guard = context.quality_guard
    try:
        result = guard.evaluate(response)
        if not isinstance(result, QualityResult) or type(result.passed) is not bool or result.action not in set(QualityAction):
            raise ValueError("invalid quality result")
    except Exception:
        context.state.record("quality.rejected", request_id=metadata.request_id, stage=stage,
                             reason_code="quality_evaluator_failed", action="block", evaluator_version="builtin-1",
                             duration_seconds=monotonic() - started)
        raise QualityViolation() from None
    requested = QualityAction(result.action).value
    action = "pass" if result.passed else "observe" if requested in {"pass", "observe"} else "block"
    context.state.record("quality.passed" if result.passed else "quality.finding" if action == "observe" else "quality.rejected",
                         request_id=metadata.request_id, stage=stage, reason_code="quality_passed" if result.passed else "quality_failed",
                         action=action, requested_action=requested, evaluator_version="builtin-1",
                         duration_seconds=monotonic() - started)
    if action == "block":
        raise QualityViolation(action=requested)
