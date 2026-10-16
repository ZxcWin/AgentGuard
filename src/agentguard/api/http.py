"""Optional local console: install agentguard[console] to run this adapter."""
from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from random import Random
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
import sqlite3

from .console_models import EventView, HealthView, OverviewView, Page, PolicyView, RunDetail, RunView

from ..adapters.storage.sqlite import SQLiteRunStore
from ..adapters.storage.sqlite_policy import SQLitePolicyStore
from ..application.runtime import AgentGuard
from ..config.manager import ConfigManager, ConfigVersionConflict
from ..config.merge import deep_merge
from ..config.schema import AgentGuardConfig
from ..domain.exceptions import AgentGuardError, BudgetExceeded, ConfigurationError

EDITABLE = {
    "token": ["max_input_tokens", "max_output_tokens", "run_token_budget", "reservation_buffer", "retry_budget_ratio", "on_exceeded", "missing_usage"],
    "budget": ["max_cost_usd", "run_cost_usd", "cost_warning_threshold"],
    "rate_limit": ["rpm", "tpm", "max_concurrency", "max_tool_calls"],
    "tools": ["max_calls", "max_steps", "max_repeated_calls", "per_tool_limits", "side_effects", "idempotency_required_for", "approval_required_for", "approval_timeout_seconds"],
    "timeout": ["run_seconds", "llm_seconds", "tool_seconds"],
    "retry": ["max_retries", "initial_delay_seconds", "max_delay_seconds", "max_elapsed_seconds", "jitter"],
    "degradation": ["fallback_model", "model_profiles", "required_capabilities", "allowed_regions", "data_classification", "max_cost_usd"],
    "pricing": ["version", "models"],
}


class PolicyChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: dict[str, Any]
    expected_version: int = Field(ge=1, strict=True)


def create_app(*, database: str | Path = ".agentguard/runs.db", demo: bool = False,
               frontend: str | Path | None = None, allowed_origins: tuple[str, ...] = ()) -> FastAPI:
    for origin in allowed_origins:
        parsed = urlsplit(origin)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment or "*" in origin):
            raise ValueError("Allowed origins must be exact HTTP(S) origins, e.g. http://192.168.1.10:5173")
    path = Path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteRunStore(path)
    policy_store = SQLitePolicyStore(path)
    manager = ConfigManager({"token": {"run_token_budget": 32000, "max_output_tokens": 2048},
                             "budget": {"run_cost_usd": 1.0},
                             "rate_limit": {"rpm": 60, "max_concurrency": 8, "max_tool_calls": 20},
                             "pricing": {"models": {"local-example": {"input_per_million": 0.15, "output_per_million": 0.6}}}}, environ={}, policy_store=policy_store)
    guard = AgentGuard(config_manager=manager, run_store=store)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if demo and store.list(hours=168)["total"] == 0:
            generate_examples(guard, 48)
        yield
        store.close()
        policy_store.close()

    app = FastAPI(title="AgentGuard Console API", version="1.0.0", lifespan=lifespan)
    app.state.guard, app.state.store = guard, store
    app.state.config_manager = manager

    @app.middleware("http")
    async def local_access(request: Request, call_next):
        host = request.url.hostname
        origin = request.headers.get("origin")
        allowed = {f"http://{host}:5173", f"http://{host}:8000", f"http://{host}:8001", str(request.base_url).rstrip("/"), *allowed_origins}
        if host not in {"localhost", "127.0.0.1", "testserver", "::1"} or (origin and origin not in allowed):
            return JSONResponse({"error": {"code": "forbidden", "message": "Local console access only"}}, status_code=403)
        return await call_next(request)

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
                "demo": demo, "storage": "sqlite", "policy_scope": "local_database", "policy_persistence": "sqlite"}

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
    def update(change: PolicyChange):
        validated = check_change(change)
        try:
            manager.replace(validated, expected_version=change.expected_version)
        except ConfigVersionConflict as error:
            raise HTTPException(409, str(error)) from error
        return policy()

    @app.post("/api/v1/demo/runs")
    def sample(scenario: Literal["standard", "retry"] = "standard"):
        if not demo:
            raise HTTPException(404, "Demo mode is disabled")
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
    parser.add_argument("--host", choices=["127.0.0.1", "localhost"], default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--database", default=None)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--frontend", default="frontend/dist")
    parser.add_argument("--allow-origin", action="append", default=[], help="Exact trusted frontend origin for LAN development; repeatable")
    args = parser.parse_args()
    database = args.database or (".agentguard/demo.db" if args.demo else ".agentguard/runs.db")
    uvicorn.run(create_app(database=database, demo=args.demo, frontend=args.frontend, allowed_origins=tuple(args.allow_origin)), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
