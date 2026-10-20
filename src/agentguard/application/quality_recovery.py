"""Bounded request recovery. No failed response or prompt enters audit/telemetry."""
from __future__ import annotations

from contextvars import ContextVar
from collections.abc import Mapping
from copy import deepcopy
from inspect import isawaitable

from ..domain.exceptions import AuditUnavailable, ExecutionTimeout, QualityViolation
from ..domain.quality import QualityDecision, QualityFailure, QualityRequest
from ..policies.fallback import fallback_rejection
from ..domain.models import Usage
from .deadlines import current_deadline

recovering_run: ContextVar[object] = ContextVar("agentguard_quality_recovery", default=None)


class RecoveryStep:
    def __init__(self, session, error, attempt):
        self.session, self.context = session, session.context
        self.action, self.call = error.action, attempt.call
        self.fields = dict(request_id=self.call.metadata.request_id, run_id=self.context.state.run_id,
                           config_version=self.context.config_version, requested_action=self.action)
        self.deadline = session.deadline.child(self.context.config.quality.handler_timeout_seconds, "quality")
        self.failure = QualityFailure(self.call.quality_response,
            QualityRequest(session.args, session.kwargs),
            self.context.state.run_id, self.call.metadata.request_id, attempt.number, self.action)
        self.operation = None

    def event(self, outcome, reason=None):
        self.context.state.record(f"quality.recovery_{outcome}", **self.fields,
                                  **({"reason_code": reason} if reason else {}))

    def start(self):
        self.context._check()
        self.deadline.check()
        self.event("started")
        if self.context.audit is not None:
            self.operation = self.context.audit.begin(action=f"quality.{self.action}", **self.fields)
        self.failure = QualityFailure(self.failure.response,
            QualityRequest(deepcopy(self.session.args), deepcopy(self.session.kwargs)),
            self.failure.run_id, self.failure.request_id, self.failure.attempt, self.failure.action)

    def callback(self):
        handlers = self.context.quality_recovery
        handler = getattr(handlers, self.action, None)
        if handler is None or (self.action == "human" and self.context.audit is None):
            raise QualityViolation(action="block")
        return handler(self.failure)

    def accept(self, result):
        self.deadline.check()
        # Approval cannot grant additional tool/model calls while it is pending.
        if self.action == "human":
            if not isinstance(result, QualityDecision) or type(result.approved) is not bool or (
                type(result.actor_id) is not str or not 1 <= len(result.actor_id) <= 128 or
                result.actor_id.strip() != result.actor_id or any(ord(c) < 32 for c in result.actor_id)
            ):
                raise QualityViolation(action="block")
            self.context.audit.decision(action="quality.approval", outcome="approved" if result.approved else "denied",
                                        actor_id=result.actor_id, **self.fields)
            if not result.approved:
                raise QualityViolation(action="block")
            result = result.request
        if not isinstance(result, QualityRequest) or type(result.args) is not tuple or not isinstance(result.kwargs, Mapping):
            raise QualityViolation(action="block")
        if any(type(key) is not str for key in result.kwargs) or result.kwargs.get("stream") is True or (
            set(result.kwargs) & {"budget_purpose", "cache_options"}
        ):
            raise QualityViolation(action="block")
        args, kwargs = deepcopy(result.args), deepcopy(dict(result.kwargs))
        # Repair and human handlers can edit input, but model routing is a
        # separate, capability-checked degradation decision.
        if kwargs.get("model") != self.session.kwargs.get("model"):
            raise QualityViolation(action="block")
        if self.operation is not None:
            self.context.audit.end(self.operation, outcome="approved")
            self.operation = None
        self.session.args, self.session.kwargs = args, kwargs
        self.event("completed")

    def degrade(self):
        from .runtime import _request_estimates
        config = self.context.config.degradation
        kwargs = dict(self.session.kwargs, model=config.fallback_model)
        if not config.fallback_model or self.call.admitted.model == config.fallback_model:
            raise QualityViolation(action="block")
        inputs, outputs = _request_estimates(self.context, self.session.args, kwargs)
        cost = self.context.pricing.estimate(config.fallback_model, Usage(inputs, outputs, source="estimate"))
        reason = fallback_rejection(config, self.session.args, kwargs, cost)
        if reason is not None:
            self.event("rejected", reason)
            raise QualityViolation(action="block")
        self.session.kwargs = kwargs
        self.session.fallback_active = True
        if self.operation is not None:
            self.context.audit.end(self.operation, outcome="approved")
            self.operation = None
        self.event("completed")

    def fail(self, error):
        if self.operation is not None:
            operation, self.operation = self.operation, None
            self.context.audit.end(operation, outcome="failed", execution_may_have_completed=True)
        self.event("rejected", "quality_handler_failed")
        if isinstance(error, (AuditUnavailable, ExecutionTimeout)) or not isinstance(error, Exception):
            raise error
        raise QualityViolation(action="block") from None

    def clear(self):
        self.call.quality_response = None
        self.failure = None

    def sync(self):
        try:
            self.start()
            if self.action == "degrade":
                self.degrade()
                return
            token = recovering_run.set(self.context)
            deadline_token = current_deadline.set(self.deadline)
            try:
                result = self.callback()
                if isawaitable(result):
                    if hasattr(result, "close"):
                        result.close()
                    raise QualityViolation(action="block")
            finally:
                recovering_run.reset(token)
                current_deadline.reset(deadline_token)
            self.accept(result)
        except BaseException as error:
            self.fail(error)
        finally:
            self.clear()

    async def async_run(self):
        try:
            self.start()
            if self.action == "degrade":
                self.degrade()
                return
            token = recovering_run.set(self.context)
            deadline_token = current_deadline.set(self.deadline)
            try:
                result = self.callback()
                if isawaitable(result):
                    result = await self.deadline.wait(result)
            finally:
                recovering_run.reset(token)
                current_deadline.reset(deadline_token)
            self.accept(result)
        except BaseException as error:
            self.fail(error)
        finally:
            self.clear()
