"""Optional local console: install agentguard[console] to run this adapter."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from random import Random
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
import sqlite3
import re
from threading import RLock

from .console_models import EventView, HealthView, OverviewView, Page, PolicyView, RunDetail, RunView
from .auth import ConsoleCredentials, Principal

from ..adapters.storage.sqlite import SQLiteRunStore
from ..adapters.storage.sqlite_policy import SQLitePolicyStore
from ..adapters.events import MemoryEventSink
from ..adapters.storage.audit import SQLiteAuditStore
from ..adapters.storage.ledger import SQLiteCostLedger
from ..adapters.storage.sqlite_cache import SQLiteExactCache
from ..application.audit import AuditCoordinator
from ..application.runtime import AgentGuard
from ..config.manager import ConfigManager, ConfigVersionConflict
from ..config.merge import deep_merge
from ..config.schema import AgentGuardConfig
from ..domain.exceptions import AgentGuardError, BudgetExceeded, ConfigurationError, AuditUnavailable
from ..domain.cache import CacheRequest, ToolCacheRequest

EDITABLE = {
    "behavior": ["max_repeated_outputs", "max_calls_without_progress"],
    "telemetry": ["enable_metrics_export", "retention_days"],
    "cache": ["enabled", "ttl_seconds", "max_entries", "max_bytes", "allowed_models", "allowed_tools"],
    "enforcement": ["profile"],
    "security": ["enabled", "action", "detect_secrets", "detect_pii", "detect_prompt_injection", "max_chars"],
    "quality": ["enabled", "action", "strict_json", "max_chars", "json_schema", "max_retries", "min_chars", "required_substrings", "handler_timeout_seconds"],
    "token": ["max_input_tokens", "max_output_tokens", "run_token_budget", "reservation_buffer", "retry_budget_ratio", "on_exceeded", "missing_usage", "missing_price", "usage_mismatch"],
    "budget": ["max_cost_usd", "run_cost_usd", "cost_warning_threshold"],
    "rate_limit": ["rpm", "tpm", "max_concurrency", "max_tool_calls"],
    "circuit": ["failure_threshold", "recovery_seconds", "max_keys"],
    "tools": ["max_calls", "max_steps", "max_repeated_calls", "per_tool_limits", "side_effects", "idempotency_required_for", "approval_required_for", "approval_timeout_seconds", "max_depth", "max_concurrent"],
    "timeout": ["run_seconds", "llm_seconds", "tool_seconds"],
    "retry": ["max_retries", "initial_delay_seconds", "max_delay_seconds", "max_elapsed_seconds", "jitter"],
    "degradation": ["fallback_model", "model_profiles", "required_capabilities", "allowed_regions", "data_classification", "max_cost_usd"],
    "pricing": ["version", "models"],
}


class PolicyChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: dict[str, Any]
    expected_version: int = Field(ge=1, strict=True)


class AuditResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(min_length=1, max_length=256)
    outcome: Literal["confirmed_completed", "confirmed_not_executed", "uncertain_recorded"]


class InvoiceImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(min_length=1, max_length=128)
    invoice_id: str = Field(min_length=1, max_length=128)
    lines: list[dict[str, Any]] = Field(min_length=1, max_length=1000)


class LedgerAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    adjustment_id: str = Field(min_length=1,max_length=128)
    reference_id: str = Field(min_length=1,max_length=512)
    amount_usd: str = Field(min_length=1,max_length=40)


def create_app(*, database: str | Path = ".agentguard/runs.db", demo: bool = False,
               frontend: str | Path | None = None, allowed_origins: tuple[str, ...] = (),
               credentials: ConsoleCredentials | None = None, public_hosts: tuple[str, ...] = (),
               policy_signing_key: bytes | None = None, policy_scope: dict[str, str] | None = None,
               cache_database: str | Path | None = None, cache_signing_key: bytes | None = None,
               llm_upstream_url: str | None = None) -> FastAPI:
    if (cache_database is None) != (cache_signing_key is None):
        raise ValueError("Persistent cache requires both cache_database and cache_signing_key")
    if cache_signing_key is not None and (type(cache_signing_key) is not bytes or not 32 <= len(cache_signing_key) <= 4096):
        raise ValueError("Cache signing key must contain 32 to 4096 bytes")
    if policy_signing_key is not None:
        if credentials is None or len(policy_signing_key) < 32 or not policy_scope or set(policy_scope) != {"tenant_id", "project_id", "environment"}:
            raise ValueError("Signed policy service requires credentials, a 32-byte signing key and exact scope")
    if public_hosts and credentials is None:
        raise ValueError("Remote hosts require server-side credentials")
    if any(not host or "/" in host or ":" in host or "*" in host or host != host.lower() for host in public_hosts):
        raise ValueError("public_hosts must contain exact lowercase hostnames or IPv4 addresses")
    for origin in allowed_origins:
        parsed = urlsplit(origin)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment or "*" in origin):
            raise ValueError("Allowed origins must be exact HTTP(S) origins, e.g. http://192.168.1.10:5173")
    path = Path(database)
    if cache_database is not None and Path(cache_database).resolve() in {
        path.resolve(), path.with_name(path.stem + ".audit.db").resolve(), path.with_name(path.stem + ".ledger.db").resolve()
    }:
        raise ValueError("Cache must use a separate database")
    path.parent.mkdir(parents=True, exist_ok=True)
    proxy_db = sqlite3.connect(str(path.with_name(path.stem + ".proxy.db")), check_same_thread=False)
    proxy_db.execute("PRAGMA journal_mode=WAL")
    proxy_db.execute("CREATE TABLE IF NOT EXISTS proxy_projects (name TEXT PRIMARY KEY, upstream TEXT NOT NULL)")
    proxy_db.commit()
    proxy_lock = RLock()
    store = SQLiteRunStore(path)
    policy_store = SQLitePolicyStore(path)
    audit_store = SQLiteAuditStore(path.with_name(path.stem + ".audit.db"))
    ledger = SQLiteCostLedger(path.with_name(path.stem + ".ledger.db"))
    manager = ConfigManager({"token": {"run_token_budget": 32000, "max_output_tokens": 2048},
                             "budget": {"run_cost_usd": 1.0},
                             "rate_limit": {"rpm": 60, "max_concurrency": 8, "max_tool_calls": 20},
                             "pricing": {"models": {"local-example": {"input_per_million": 0.15, "output_per_million": 0.6}}}}, environ={}, policy_store=policy_store)
    persistent_cache = SQLiteExactCache(cache_database, max_entries=10000, max_bytes=256 * 1024 * 1024) if cache_database is not None else None
    guard = AgentGuard(config_manager=manager, run_store=store, event_sink=MemoryEventSink(), audit_store=audit_store,
                       cache_store=persistent_cache, cache_signing_key=cache_signing_key)
    proxy_guard = AgentGuard(config_manager=manager, run_store=store)
    cache_scope = "shared_host" if persistent_cache is not None else "local_process"
    policy_audit = guard._audit
    cleanup = {"deleted_runs": 0, "failures": 0}
    if llm_upstream_url is not None:
        from .llm_proxy import validate_upstream_url
        llm_upstream_url = validate_upstream_url(llm_upstream_url)

    async def retention_loop(stop: asyncio.Event):
        while not stop.is_set():
            try:
                cleanup["deleted_runs"] += await asyncio.to_thread(store.prune, manager.config.telemetry.retention_days)
                await asyncio.to_thread(audit_store.prune)
                if persistent_cache is not None:
                    await asyncio.to_thread(persistent_cache.prune)
            except Exception:
                cleanup["failures"] += 1
            try:
                await asyncio.wait_for(stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass

    def cached_example_provider(**kwargs):
        return {"content": "{}", "usage": {"input_tokens": 4, "output_tokens": 8}}

    def cached_example_tool(item):
        return {"item": item, "category": "runtime-governance", "available": True}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop = asyncio.Event()
        task = None
        try:
            if demo and store.list(hours=168)["total"] == 0:
                generate_examples(guard, 48)
            task = asyncio.create_task(retention_loop(stop))
            yield
        finally:
            stop.set()
            if task is not None:
                await task
            await asyncio.to_thread(guard.close, 5)
            await asyncio.to_thread(proxy_guard.close, 5)
            store.close()
            policy_store.close()
            audit_store.close()
            ledger.close()
            proxy_db.close()
            if persistent_cache is not None:
                persistent_cache.close()

    app = FastAPI(title="AgentGuard Console API", version="1.0.0", lifespan=lifespan)
    app.state.guard, app.state.proxy_guard, app.state.store = guard, proxy_guard, store
    app.state.config_manager = manager
    app.state.audit_store = audit_store
    app.state.policy_audit = policy_audit
    app.state.ledger = ledger
    from .llm_proxy import install_llm_proxy, validate_upstream_url
    if llm_upstream_url is not None:
        llm_upstream_url = validate_upstream_url(llm_upstream_url)
        proxy_db.execute("INSERT OR IGNORE INTO proxy_projects(name, upstream) VALUES (?, ?)", ("default", llm_upstream_url))
        proxy_db.commit()

    def resolve_upstream(name: str) -> str | None:
        with proxy_lock:
            row = proxy_db.execute("SELECT upstream FROM proxy_projects WHERE name=?", (name,)).fetchone()
        return row[0] if row else None

    install_llm_proxy(app, proxy_guard, resolve_upstream)

    @app.middleware("http")
    async def local_access(request: Request, call_next):
        host = request.url.hostname
        origin = request.headers.get("origin")
        allowed = {f"http://{host}:5173", f"http://{host}:8000", f"http://{host}:8001", str(request.base_url).rstrip("/"), *allowed_origins}
        if host not in {"localhost", "127.0.0.1", "testserver", "::1", *public_hosts} or (origin and origin not in allowed):
            return JSONResponse({"error": {"code": "forbidden", "message": "Local console access only"}}, status_code=403)
        request.state.principal = Principal("local", "admin")
        protected = request.url.path.startswith("/api/v1/") and request.url.path != "/api/v1/health"
        if credentials is not None and protected:
            principal = credentials.authenticate(request.headers.get("authorization"))
            if principal is None:
                return JSONResponse({"error": {"code": "unauthorized", "message": "Workspace token required"}},
                                    status_code=401, headers={"WWW-Authenticate": "Bearer"})
            request.state.principal = principal
            if not credentials.permitted(principal, request.method, request.url.path):
                try:
                    await asyncio.to_thread(policy_audit.decision, action="permission.check", outcome="denied",
                                            actor_id=principal.subject, reason_code="workspace_role_denied")
                except AuditUnavailable:
                    return JSONResponse({"error":{"code":"audit_unavailable","message":"Required audit storage unavailable"}},status_code=503)
                return JSONResponse({"error": {"code": "forbidden", "message": "Workspace role does not permit this action"}}, status_code=403)
        return await call_next(request)

    if allowed_origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(allowed_origins),
                           allow_methods=["GET", "HEAD", "POST", "PUT", "DELETE"],
                           allow_headers=["Authorization", "Content-Type"], max_age=600)

    def openapi_schema():
        if app.openapi_schema is None:
            schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
            if credentials is not None:
                schema.setdefault("components", {}).setdefault("securitySchemes", {})["WorkspaceToken"] = {"type":"http", "scheme":"bearer"}
                for path, operations in schema["paths"].items():
                    if path != "/api/v1/health":
                        for operation in operations.values():
                            if isinstance(operation, dict): operation["security"] = [{"WorkspaceToken":[]}]
            app.openapi_schema = schema
        return app.openapi_schema
    app.openapi = openapi_schema

    @app.exception_handler(AuditUnavailable)
    async def audit_error(request: Request, error: AuditUnavailable):
        return JSONResponse({"error": {"code": "audit_unavailable", "message": "Required audit storage unavailable; protected operations paused",
            "execution_may_have_completed": error.execution_may_have_completed, "operation_id": error.operation_id}}, status_code=503)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, error: StarletteHTTPException):
        return JSONResponse({"error": {"code": f"http_{error.status_code}", "message": str(error.detail)}}, status_code=error.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        return JSONResponse({"error": {"code": "invalid_request", "message": "Invalid request parameters or body"}}, status_code=422)

    @app.exception_handler(sqlite3.OperationalError)
    async def storage_error(request: Request, error: sqlite3.OperationalError):
        return JSONResponse({"error": {"code": "storage_unavailable", "message": "Local storage is temporarily unavailable; retry later"}}, status_code=503)

    @app.get("/api/v1/health", response_model=HealthView)
    def health():
        return {"status": "ok", "api_version": "1", "schema_version": 1, "sdk_version": "0.1.0",
                "demo": demo, "storage": "sqlite", "policy_scope": "local_database", "policy_persistence": "sqlite",
                "authentication": "bearer" if credentials is not None else "local",
                "cache_backend": "sqlite" if persistent_cache is not None else "memory", "cache_scope": cache_scope}

    @app.get("/api/v1/session")
    def session(request: Request):
        return {"subject": request.state.principal.subject, "role": request.state.principal.role,
                "workspace": "current_database"}

    @app.get("/api/v1/proxy/projects")
    def proxy_projects():
        return {"items": [{"name": name, "upstream": upstream} for name, upstream in
                           proxy_db.execute("SELECT name, upstream FROM proxy_projects ORDER BY name").fetchall()]}

    @app.put("/api/v1/proxy/projects")
    def save_proxy_project(payload: dict[str, Any]):
        from .llm_proxy import validate_upstream_url
        name, upstream = payload.get("name"), payload.get("upstream")
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", name):
            raise HTTPException(422, "Invalid project name")
        if not isinstance(upstream, str):
            raise HTTPException(422, "Invalid model upstream URL")
        try:
            upstream = validate_upstream_url(upstream)
        except ValueError:
            raise HTTPException(422, "Invalid model upstream URL") from None
        with proxy_lock, proxy_db:
            proxy_db.execute("INSERT OR REPLACE INTO proxy_projects(name, upstream) VALUES (?, ?)", (name, upstream))
        return {"name": name, "upstream": upstream}

    @app.delete("/api/v1/proxy/projects/{name}")
    def delete_proxy_project(name: str):
        if name == "default":
            raise HTTPException(409, "The default project cannot be removed")
        with proxy_lock, proxy_db:
            cursor = proxy_db.execute("DELETE FROM proxy_projects WHERE name=?", (name,))
        if cursor.rowcount == 0:
            raise HTTPException(404, "Project not found")
        return {"deleted": True}

    @app.get("/api/v1/policies/snapshot")
    def remote_snapshot():
        if policy_signing_key is None:
            raise HTTPException(404, "Remote policy signing is disabled")
        from ..adapters.remote_policy import sign_policy
        snapshot = manager.snapshot()
        now = datetime.now(timezone.utc)
        values = asdict(snapshot.config)
        if values["rate_limit"]["redis_url"]:
            raise HTTPException(409, "Remote snapshots cannot contain Redis credentials; provision runtime secrets locally")
        return sign_policy({"version": snapshot.version, "issued_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=15)).isoformat(), "scope": policy_scope,
            "config": values, "recovery": False}, policy_signing_key)

    @app.get("/api/v1/audit")
    def audit(page: int = Query(1, ge=1, le=100000), page_size: int = Query(50, ge=1, le=100), pending_only: bool = False):
        return audit_store.list(page=page, page_size=page_size, pending_only=pending_only)

    @app.get("/api/v1/audit/health")
    def audit_health():
        return {**audit_store.stats(), "blocked": policy_audit.blocked or guard._audit.blocked}

    @app.get("/api/v1/billing")
    def billing():
        return ledger.summary()

    @app.get("/api/v1/billing/entries")
    def billing_entries(page: int = Query(1,ge=1,le=100000), page_size: int = Query(50,ge=1,le=100)):
        return ledger.list(page=page,page_size=page_size)

    @app.post("/api/v1/billing/invoices")
    def import_invoice(change: InvoiceImport, request: Request):
        operation = policy_audit.begin(action="billing.import",actor_id=request.state.principal.subject)
        try:
            result = ledger.import_invoice(change.provider,change.invoice_id,change.lines,actor=request.state.principal.subject)
        except ValueError:
            policy_audit.end(operation,outcome="rejected")
            raise HTTPException(422,"Invalid invoice or conflicting immutable entry") from None
        except Exception:
            policy_audit.end(operation,outcome="failed",execution_may_have_completed=True)
            raise
        policy_audit.end(operation,outcome="completed")
        return result

    @app.post("/api/v1/billing/adjustments")
    def adjust_invoice(change: LedgerAdjustment, request: Request):
        operation = policy_audit.begin(action="billing.adjust",actor_id=request.state.principal.subject)
        try:
            result = ledger.adjust(**change.model_dump(),actor=request.state.principal.subject)
        except ValueError:
            policy_audit.end(operation,outcome="rejected")
            raise HTTPException(422,"Invalid adjustment or conflicting immutable entry") from None
        except Exception:
            policy_audit.end(operation,outcome="failed",execution_may_have_completed=True)
            raise
        policy_audit.end(operation,outcome="completed")
        return result

    @app.post("/api/v1/billing/sync-estimates")
    def sync_estimates(request: Request):
        operation = policy_audit.begin(action="billing.sync_estimates",actor_id=request.state.principal.subject)
        count = 0
        try:
            for estimate in store.cost_estimates():
                ledger.estimate(**estimate)
                count += 1
        except Exception:
            policy_audit.end(operation,outcome="failed",execution_may_have_completed=True)
            raise
        policy_audit.end(operation,outcome="completed")
        return {"processed":count,"scope":"latest_1000_terminal_runs"}

    @app.post("/api/v1/audit/recover")
    def audit_recover():
        guard.recover_audit()
        return {"recovered": True, "note": "Uncertain operations are not replayed"}

    @app.post("/api/v1/audit/reconcile")
    def reconcile_audit(change: AuditResolution, request: Request):
        try:
            audit_store.reconcile(change.operation_id, outcome=change.outcome, actor=request.state.principal.subject)
        except ValueError:
            raise HTTPException(409, "Unknown operation or result already recorded") from None
        return {"recorded": True, "replayed": False}

    @app.get("/api/v1/telemetry")
    def telemetry():
        return {**guard.telemetry_stats(), "scope": "local_process", "audit_durable": False, "required_audit_enabled": True,
                "retention_days": manager.config.telemetry.retention_days, "cleanup": dict(cleanup)}

    @app.get("/api/v1/metrics", response_class=PlainTextResponse)
    def metrics():
        if not manager.config.telemetry.enable_metrics_export:
            raise HTTPException(404, "Metrics export is disabled")
        return PlainTextResponse(guard.telemetry_metrics(), media_type="text/plain; version=0.0.4")

    @app.get("/api/v1/overview", response_model=OverviewView)
    def overview(hours: int = Query(24, ge=1, le=168), model: str = Query("", max_length=100)):
        return store.overview(hours, model)

    @app.get("/api/v1/runs", response_model=Page[RunView])
    def runs(hours: int = Query(24, ge=1, le=168), status: Literal["", "running", "completed", "failed", "cancelled", "timed_out"] = "",
             search: str = Query("", max_length=100), page: int = Query(1, ge=1, le=100000), page_size: int = Query(12, ge=1, le=100)):
        return store.list(hours=hours, status=status, search=search, page=page, page_size=page_size)

    def get_run(run_id: str):
        result = store.get(run_id)
        if result is None:
            raise HTTPException(404, "Run not found")
        return result

    @app.get("/api/v1/runs/{run_id}", response_model=RunDetail)
    def run(run_id: str):
        result = get_run(run_id)
        result["events_total"] = len(result["events"])
        result["events"] = result["events"][:100]
        return result

    @app.get("/api/v1/runs/{run_id}/events", response_model=Page[EventView])
    def events(run_id: str, page: int = Query(1, ge=1, le=100000), page_size: int = Query(50, ge=1, le=100)):
        rows = get_run(run_id)["events"]
        return {"items": rows[(page - 1) * page_size:page * page_size], "total": len(rows), "page": page, "page_size": page_size}

    @app.get("/api/v1/policies/current", response_model=PolicyView)
    def policy():
        snapshot = manager.snapshot()
        config = asdict(snapshot.config)
        return {"version": snapshot.version, "config": {group: {key: config[group][key] for key in keys}
                for group, keys in EDITABLE.items()}, "editable": EDITABLE, "scope": "local_database", "persistence": "sqlite"}

    def check_change(change: PolicyChange):
        for group, fields in change.config.items():
            if group not in EDITABLE or not isinstance(fields, dict) or set(fields) - set(EDITABLE[group]):
                raise HTTPException(422, "Unsupported policy field")
        try:
            values = asdict(manager.config)
            for group, fields in change.config.items():
                values[group].update(fields)
            return AgentGuardConfig.from_mapping(values)
        except ConfigurationError as error:
            raise HTTPException(422, str(error)) from error

    @app.post("/api/v1/policies/validate")
    def validate(change: PolicyChange):
        check_change(change)
        return {"valid": True, "version": manager.snapshot().version}

    @app.put("/api/v1/policies/current", response_model=PolicyView)
    def update(change: PolicyChange, request: Request):
        validated = check_change(change)
        operation = policy_audit.begin(action="policy.publish", actor_id=request.state.principal.subject,
                                       expected_version=change.expected_version, target_version=change.expected_version + 1)
        try:
            manager.replace(validated, expected_version=change.expected_version)
        except ConfigVersionConflict as error:
            policy_audit.end(operation, outcome="conflict")
            raise HTTPException(409, str(error)) from error
        except Exception:
            policy_audit.end(operation, outcome="failed", execution_may_have_completed=True)
            raise
        policy_audit.end(operation, outcome="published", target_version=manager.snapshot().version)
        return policy()

    @app.delete("/api/v1/cache")
    def clear_cache(request: Request):
        operation = policy_audit.begin(action="cache.invalidate", actor_id=request.state.principal.subject)
        try:
            guard.clear_cache()
        except Exception:
            policy_audit.end(operation, outcome="failed", execution_may_have_completed=True)
            raise
        policy_audit.end(operation, outcome="completed")
        return {"cleared": True, "scope": cache_scope}

    @app.post("/api/v1/demo/runs")
    def sample(scenario: Literal["standard", "retry", "security", "quality", "cache", "tool-cache", "circuit"] = "standard"):
        if not demo:
            raise HTTPException(404, "Demo mode is disabled")
        if scenario == "circuit":
            run = guard.run(name="circuit-example", environment="demo")
            def unavailable(**kwargs):
                raise ConnectionError("Local simulated provider failure")
            try:
                with run:
                    # Bound demo work even when the configured threshold is large.
                    for _ in range(min(run.config.circuit.failure_threshold + 1, 6)):
                        try:
                            run.invoke(unavailable, model="local-example", messages="local circuit example", max_tokens=1)
                        except ConnectionError:
                            continue
            except AgentGuardError:
                pass
            return {"created": 1, "run_id": run.state.run_id}
        if scenario == "tool-cache":
            run = guard.run(name="readonly-tool-cache-example", environment="demo", tenant_id="local-demo",
                            project_id="console", user_id="demo-reader")
            options = ToolCacheRequest(namespace="local-catalog", prompt_version="n/a",
                                       authorization_context="demo-reader-v1", data_classification="public", pure=True,
                                       provider_id="console-local-catalog-v1", tool_version="v1", data_version="catalog-v1")
            try:
                with run:
                    for _ in range(2):
                        run.invoke_tool("catalog_lookup", cached_example_tool, "agentguard", cache_options=options)
            except AgentGuardError:
                pass
            return {"created": 1, "run_id": run.state.run_id}
        if scenario == "cache":
            run = guard.run(name="cache-example", environment="demo", tenant_id="local-demo",
                            project_id="console", user_id="demo-reader")
            options = CacheRequest(namespace="local-example-v1", prompt_version="v1",
                                   authorization_context="demo-reader-v1", data_classification="public", pure=True,
                                   provider_id="console-local-example-v1")
            try:
                with run:
                    for _ in range(2):
                        run.invoke(cached_example_provider, model="local-example", messages="local cache example",
                                   max_tokens=32, cache_options=options)
            except AgentGuardError:
                pass
            return {"created": 1, "run_id": run.state.run_id}
        if scenario in {"security", "quality"}:
            run = guard.run(name=f"{scenario}-example", environment="demo")
            def provider(**kwargs):
                return {"content": "local example: invalid JSON", "usage": {"input_tokens": 4, "output_tokens": 8}}
            try:
                with run:
                    run.invoke(provider, model="local-example", max_tokens=32,
                               messages="api_key=local-demo-placeholder" if scenario == "security" else "local quality example")
            except AgentGuardError:
                pass
            return {"created": 1, "run_id": run.state.run_id}
        if scenario == "retry":
            run = guard.run(name="retry-example", environment="demo")
            attempts = 0
            def provider(**kwargs):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise ConnectionError("Local simulated transient failure")
                return {"usage": {"input_tokens": 4, "output_tokens": 8}}
            try:
                with run:
                    run.invoke(provider, model="local-example", messages="local retry example", max_tokens=32)
            except (AgentGuardError, ConnectionError):
                pass
            return {"created": 1, "run_id": run.state.run_id}
        generate_examples(guard, 1)
        return {"created": 1}

    if frontend is not None and Path(frontend).is_dir():
        app.mount("/", StaticFiles(directory=str(frontend), html=True), name="console")
    return app


def generate_examples(guard: AgentGuard, count: int) -> None:
    random = Random(42 if count > 1 else datetime.now().timestamp())
    names = ["research-assistant", "customer-support", "code-reviewer", "document-analyst"]
    for index in range(count):
        try:
            with guard.run(name=names[index % len(names)], environment="demo") as run:
                if count > 1:
                    run.state.started_at = datetime.now(timezone.utc) - timedelta(minutes=random.randrange(1, 1380))
                    run.state.events[0]["timestamp"] = run.state.started_at.isoformat()
                for _ in range(random.randint(1, 4)):
                    def provider(**kwargs):
                        if index % 4 == 0:
                            run.invoke_tool("local_search", lambda: {"items": 3})
                        return {"usage": {"prompt_tokens": random.randint(90, 230), "completion_tokens": random.randint(35, 180)}}
                    if index % 9 == 4:
                        run.invoke(lambda **kwargs: {"text": "local sample"}, model="local-example", messages="local sample", max_tokens=100)
                    else:
                        run.invoke(provider, model="local-example", messages="local sample", max_tokens=256)
                if index % 11 == 7:
                    run.invoke(lambda **kwargs: None, model="local-example", messages="x" * 140000, max_tokens=256)
                if index % 13 == 5:
                    raise RuntimeError("Local simulated provider failure")
        except (AgentGuardError, RuntimeError):
            pass
        if count > 1:
            # Shift all fixture timestamps together, retaining order and duration.
            ended = datetime.fromisoformat(run.state.events[-1]["timestamp"])
            shift = ended - run.state.started_at - timedelta(milliseconds=random.randint(120, 1800))
            for event in run.state.events[1:]:
                event["timestamp"] = (datetime.fromisoformat(event["timestamp"]) - shift).isoformat()
            guard._run_store.save(run.state, run.config_version)


def main() -> None:
    import uvicorn
    parser = argparse.ArgumentParser(description="AgentGuard local console")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--credentials", help="File containing workspace token hashes")
    parser.add_argument("--public-host", action="append", default=[], help="Exact remote hostname; requires --credentials")
    parser.add_argument("--policy-key-file", help="Raw signing secret file, at least 32 bytes")
    parser.add_argument("--policy-scope", help="JSON file with tenant_id/project_id/environment")
    parser.add_argument("--cache-database", help="Separate same-host SQLite exact cache database")
    parser.add_argument("--cache-key-file", help="Separate raw cache signing secret file, 32 to 4096 bytes")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--database", default=None)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--frontend", default="frontend/dist")
    parser.add_argument("--llm-upstream-url", help="Enable OpenAI-compatible /v1/chat/completions proxy to this provider base URL")
    parser.add_argument("--allow-origin", action="append", default=[], help="Exact trusted frontend origin for LAN development; repeatable")
    args = parser.parse_args()
    if bool(args.cache_database) != bool(args.cache_key_file):
        parser.error("Use --cache-database and --cache-key-file together")
    cache_key = None
    if args.cache_key_file:
        try:
            with Path(args.cache_key_file).open("rb") as stream:
                cache_key = stream.read(4097)
            if not 32 <= len(cache_key) <= 4096:
                raise ValueError()
        except (OSError, ValueError):
            parser.error("Cannot read a valid cache key file")
    if args.host not in {"127.0.0.1", "localhost", "::1"} and (not args.credentials or not args.public_host):
        parser.error("Remote binding requires --credentials and --public-host; terminate HTTPS at a trusted reverse proxy")
    credentials = ConsoleCredentials.load(args.credentials) if args.credentials else None
    policy_key = Path(args.policy_key_file).read_bytes() if args.policy_key_file else None
    import json
    policy_scope = json.loads(Path(args.policy_scope).read_text(encoding="utf-8")) if args.policy_scope else None
    database = args.database or (".agentguard/demo.db" if args.demo else ".agentguard/runs.db")
    uvicorn.run(create_app(database=database, demo=args.demo, frontend=args.frontend, allowed_origins=tuple(args.allow_origin),
                          credentials=credentials, public_hosts=tuple(args.public_host),
                          policy_signing_key=policy_key, policy_scope=policy_scope,
                          cache_database=args.cache_database, cache_signing_key=cache_key,
                          llm_upstream_url=args.llm_upstream_url), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
