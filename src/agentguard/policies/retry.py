"""Provider-independent retry classification; no waits or SDK imports."""
from __future__ import annotations

from datetime import datetime
from email.utils import parsedate_to_datetime
from math import isfinite
from collections.abc import Mapping

from ..domain.exceptions import AgentGuardError


def retry_reason(error: BaseException) -> str | None:
    if isinstance(error, AgentGuardError):
        return None
    response = getattr(error, "response", None)
    status = getattr(error, "status_code", getattr(response, "status_code", None))
    if status is not None:
        return f"http_{status}" if type(status) is int and status in {408, 429, 500, 502, 503, 504} else None
    if isinstance(error, (ConnectionError, TimeoutError)):
        return "connection_error" if isinstance(error, ConnectionError) else "provider_timeout"
    return None


def retry_after(error: BaseException, now: datetime) -> float | None:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None) or getattr(error, "headers", None)
    if not isinstance(headers, Mapping):
        return None
    value = next((v for k, v in headers.items() if str(k).lower() == "retry-after"), None)
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except (ValueError, TypeError, OverflowError):
        try:
            timestamp = parsedate_to_datetime(str(value))
            if timestamp.tzinfo is None:
                return None
            seconds = (timestamp - now).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    if not isfinite(seconds):
        return float("inf") if seconds > 0 else None
    return max(0.0, seconds)
