"""Opt-in exact caching for explicitly versioned, read-only tools."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
from dataclasses import asdict

from ..adapters.storage.exact_cache import MemoryExactCache
from ..adapters.storage.sqlite_cache import SQLiteExactCache
from ..domain.cache import ToolCacheRequest
from ..domain.exceptions import AuditUnavailable, SecurityViolation
from ..policies.cache import Uncacheable, encode, validate_content
from .caching import MISS


class ToolCacheTicket:
    def __init__(self, context, metadata, name, key, epoch):
        self.context, self.metadata, self.key, self.epoch = context, metadata, key, epoch
        self.runtime = context.cache_runtime
        self.name, self.hit = name, False
        self.tools_before, self.calls_before = context.state.tool_calls, context.state.llm_calls

    def event(self, name, reason=None):
        self.context.state.record("cache.tool_" + name, request_id=self.metadata.request_id,
                                  parent_id=self.metadata.parent_id, tool=self.name,
                                  **({"reason_code": reason} if reason else {}))

    def read(self):
        try:
            if self.epoch != self.runtime.epoch():
                return MISS
            payload = self.runtime.store.get(self.key)
            if payload is None:
                self.event("miss")
                return MISS
            if type(payload) is not bytes or len(payload) > 200000:
                raise Uncacheable("invalid_entry")
            envelope = json.loads(payload)
            response, issued, expires, clock = (envelope["response"], envelope["issued"],
                                                 envelope["expires"], envelope["clock"])
            signed = encode([self.key, response, issued, expires, clock])
            signature = hmac.new(self.epoch, signed, hashlib.sha256).hexdigest()
            now = self.runtime.now()
            if (not hmac.compare_digest(signature, envelope["signature"]) or
                    clock != ("unix" if self.runtime.stable else "monotonic") or
                    type(issued) not in {int, float} or type(expires) not in {int, float} or
                    not math.isfinite(issued) or not math.isfinite(expires) or
                    not 0 < expires - issued <= 3600 or now < issued or now >= expires or
                    self.epoch != self.runtime.epoch()):
                self.runtime.store.delete(self.key)
                return MISS
            result = json.loads(response)
            validate_content(result, response=True)
            if type(result) not in {str, dict, list, int, float, bool, type(None)}:
                raise Uncacheable("unsupported_response")
            if self.context.security_guard is not None:
                from .guards import check_security
                result = check_security(self.context, self.metadata, result, stage="response")
                if any(e.get("request_id") == self.metadata.request_id and
                       e["event"] in {"security.finding", "security.redacted"}
                       for e in self.context.state.events):
                    self.runtime.store.delete(self.key)
                    self.event("bypassed", "current_guard_finding")
                    return MISS
            if self.epoch != self.runtime.epoch():
                return MISS
            self.hit = True
            self.event("hit")
            return result
        except (AuditUnavailable, SecurityViolation):
            try:
                self.runtime.store.delete(self.key)
            except Exception:
                pass
            raise
        except Exception:
            self.event("bypassed", "invalid_entry")
            try:
                self.runtime.store.delete(self.key)
            except Exception:
                pass
            return MISS

    def publish(self, result):
        if self.hit:
            return
        try:
            if self.epoch != self.runtime.epoch():
                self.event("bypassed", "invalidated")
                return
            if (self.context.state.tool_calls != self.tools_before or
                    self.context.state.llm_calls != self.calls_before or
                    any(e.get("request_id") == self.metadata.request_id and
                        e["event"].startswith(("security.", "quality.")) and
                        e["event"] != "quality.passed" for e in self.context.state.events)):
                self.event("bypassed", "nested_or_guard_activity")
                return
            payload = validate_content(result, response=True)
            issued = self.runtime.now()
            expires = issued + self.context.config.cache.ttl_seconds
            clock = "unix" if self.runtime.stable else "monotonic"
            signed = encode([self.key, payload.decode(), issued, expires, clock])
            signature = hmac.new(self.epoch, signed, hashlib.sha256).hexdigest()
            envelope = encode({"response": payload.decode(), "signature": signature,
                               "issued": issued, "expires": expires, "clock": clock})
            self.runtime.store.set(self.key, envelope, ttl_seconds=self.context.config.cache.ttl_seconds)
            self.event("stored")
        except Exception:
            self.event("bypassed", "unsafe_result")


def prepare_tool_cache(context, metadata, name, function, args, kwargs, options, *, effect, preflight):
    if options is None:
        return None
    try:
        if not isinstance(options, ToolCacheRequest):
            raise Uncacheable("invalid_options")
        if not context.config.cache.enabled or options.bypass:
            raise Uncacheable("disabled" if not context.config.cache.enabled else "explicit_bypass")
        if effect != "read" or name not in context.config.cache.allowed_tools:
            raise Uncacheable("tool_not_allowlisted_read")
        if (not options.pure or options.data_classification not in {"public", "internal"} or
                context.config.degradation.data_classification not in {"public", "internal"}):
            raise Uncacheable("purity_or_classification")
        if preflight["approval_required"] or preflight["key_hash"] is not None:
            raise Uncacheable("approval_or_idempotency")
        scope = [context.state.tenant_id, context.state.project_id, context.state.environment, context.state.user_id]
        if any(type(value) is not str or not value.strip() for value in scope):
            raise Uncacheable("missing_identity")
        validate_content([list(args), kwargs])
        runtime = context.cache_runtime
        if isinstance(runtime.store, (MemoryExactCache, SQLiteExactCache)):
            runtime.store.configure(max_entries=context.config.cache.max_entries,
                                    max_bytes=context.config.cache.max_bytes)
        material = encode({"scope": scope, "agent": context.state.agent_id, "tool": name,
                           "tool_version": options.tool_version, "data_version": options.data_version,
                           "namespace": options.namespace, "authorization": options.authorization_context,
                           "prompt_version": options.prompt_version,
                           "classification": options.data_classification,
                           "policy": json.loads(json.dumps(asdict(context.config))),
                           "version": context.config_version,
                           "provider": options.provider_id if runtime.stable and options.provider_id else runtime.provider_key(function),
                           "args": list(args), "kwargs": kwargs})
        epoch = runtime.epoch()
        key = hmac.new(epoch, material, hashlib.sha256).hexdigest()
        return ToolCacheTicket(context, metadata, name, key, epoch)
    except Uncacheable as error:
        context.state.record("cache.tool_bypassed", request_id=metadata.request_id,
                             parent_id=metadata.parent_id, tool=name, reason_code=str(error))
        return None
    except Exception:
        context.state.record("cache.tool_bypassed", request_id=metadata.request_id,
                             parent_id=metadata.parent_id, tool=name, reason_code="cache_prepare_failed")
        return None

