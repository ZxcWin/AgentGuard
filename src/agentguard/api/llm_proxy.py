"""OpenAI-compatible pass-through proxy with AgentGuard run telemetry."""
from __future__ import annotations

import json
import re
from typing import Any, Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from ..domain.exceptions import (
    AgentGuardError,
    AuditUnavailable,
    BudgetExceeded,
    CircuitOpen,
    ExecutionTimeout,
    QualityViolation,
    RateLimitExceeded,
    SecurityViolation,
)


class ProviderResponseError(Exception):
    def __init__(self, response: Any, body: bytes) -> None:
        self.status_code = response.status_code
        self.body = body
        self.media_type = response.headers.get("content-type", "application/json").split(";")[0]


def provider_error(error: ProviderResponseError) -> Response:
    return Response(error.body, status_code=error.status_code, media_type=error.media_type)


def policy_error(error: AgentGuardError) -> JSONResponse:
    if isinstance(error, (BudgetExceeded, RateLimitExceeded)):
        status = 429
    elif isinstance(error, (CircuitOpen, AuditUnavailable)):
        status = 503
    elif isinstance(error, ExecutionTimeout):
        status = 504
    elif isinstance(error, QualityViolation):
        status = 502
    elif isinstance(error, SecurityViolation):
        status = 400
    else:
        status = 400
    code = getattr(error, "reason_code", type(error).__name__.lower())
    return JSONResponse({"error": {
        "message": str(error),
        "type": "agentguard_error",
        "param": None,
        "code": code,
    }}, status_code=status)


def validate_upstream_url(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError("LLM upstream must be an HTTP(S) base URL without credentials, query, or fragment")
    return value.rstrip("/")


def install_llm_proxy(app: FastAPI, guard: Any, resolve_upstream: Callable[[str], str | None]) -> None:
    """Mount the OpenAI Chat Completions endpoint at /v1/chat/completions."""
    import httpx

    def request_headers(request: Request) -> dict[str, str]:
        headers = {"content-type": "application/json", "accept": request.headers.get("accept", "application/json")}
        for name in ("authorization", "x-api-key", "api-key", "anthropic-version", "anthropic-beta"):
            value = request.headers.get(name)
            if value:
                headers[name] = value
        return headers

    async def record_transport_failure(name: str, model: str, messages: list[Any], max_tokens: int) -> None:
        async def failed_provider(**_: Any):
            raise httpx.ConnectError("Unable to reach the configured model provider")
        try:
            async with guard.run(name=name, environment="proxy") as run:
                await run.invoke(failed_provider, model=model, messages=messages, max_tokens=max_tokens)
        except httpx.ConnectError:
            pass

    @app.post("/v1/chat/completions")
    @app.post("/proxy/{project_name}/v1/chat/completions")
    async def chat_completions(request: Request, project_name: str | None = None):
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(400, "Expected a JSON request body") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("model"), str) or not payload["model"]:
            raise HTTPException(400, "A model name is required")
        if not isinstance(payload.get("messages"), list):
            raise HTTPException(400, "A messages array is required")

        model = payload["model"]
        messages = payload["messages"]
        if project_name is not None and not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", project_name):
            raise HTTPException(400, "Project name may contain letters, numbers, dots, underscores, and hyphens")
        agent_name = project_name or request.headers.get("x-agentguard-agent", "default")[:100]
        try:
            upstream_url = resolve_upstream(agent_name)
        except ValueError as error:
            raise HTTPException(503, str(error)) from None
        if upstream_url is None:
            raise HTTPException(503, "No model upstream configured for this project")
        upstream = validate_upstream_url(upstream_url) + "/chat/completions"
        headers = request_headers(request)
        if payload.get("stream") is True:
            async def provider(**_: Any):
                client = httpx.AsyncClient(timeout=None)
                try:
                    response = await client.send(client.build_request("POST", upstream, headers=headers, json=payload), stream=True)
                    if response.is_error:
                        await response.aread()
                        raise ProviderResponseError(response, response.content)

                    async def chunks():
                        data_lines: list[str] = []
                        try:
                            async for line in response.aiter_lines():
                                if line.startswith("data:"):
                                    data_lines.append(line[5:].lstrip())
                                elif not line and data_lines:
                                    data = "\n".join(data_lines)
                                    data_lines.clear()
                                    if data == "[DONE]":
                                        yield {"_agentguard_done": True}
                                    else:
                                        try:
                                            yield json.loads(data)
                                        except json.JSONDecodeError:
                                            continue
                            if data_lines and "\n".join(data_lines) == "[DONE]":
                                yield {"_agentguard_done": True}
                        finally:
                            await response.aclose()
                            await client.aclose()

                    return chunks()
                except BaseException:
                    await client.aclose()
                    raise

            async def events():
                try:
                    async with guard.run(name=agent_name, environment="proxy") as run:
                        stream = await run.invoke(provider, model=model, messages=messages,
                                                  max_tokens=payload.get("max_tokens", payload.get("max_completion_tokens", 256)),
                                                  stream=True)
                        async for chunk in stream:
                            if chunk.get("_agentguard_done"):
                                yield b"data: [DONE]\n\n"
                            else:
                                yield ("data: " + json.dumps(chunk, ensure_ascii=False, separators=(",", ":")) + "\n\n").encode()
                except ProviderResponseError as error:
                    try:
                        detail = json.loads(error.body)
                    except (ValueError, TypeError):
                        detail = {"message": error.body.decode("utf-8", "replace")}
                    if not isinstance(detail, dict) or "error" not in detail:
                        detail = {"error": detail}
                    yield ("data: " + json.dumps(detail, ensure_ascii=False) + "\n\n").encode()
                    yield b"data: [DONE]\n\n"
                except AgentGuardError as error:
                    detail = json.loads(policy_error(error).body)
                    yield ("data: " + json.dumps(detail, ensure_ascii=False, separators=(",", ":")) + "\n\n").encode()
                    yield b"data: [DONE]\n\n"
                except httpx.HTTPError:
                    await record_transport_failure(agent_name, model, messages,
                                                   payload.get("max_tokens", payload.get("max_completion_tokens", 256)))
                    detail = {"error": {"message": "Unable to reach the configured model provider",
                                         "type": "agentguard_error", "param": None,
                                         "code": "upstream_unavailable"}}
                    yield ("data: " + json.dumps(detail, separators=(",", ":")) + "\n\n").encode()
                    yield b"data: [DONE]\n\n"

            return StreamingResponse(events(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

        async def provider(**_: Any):
            async with httpx.AsyncClient(timeout=300) as client:
                response = await client.post(upstream, headers=headers, json=payload)
            if response.is_error:
                raise ProviderResponseError(response, response.content)
            try:
                return response.json()
            except ValueError:
                raise HTTPException(502, "Provider returned a non-JSON response") from None

        try:
            async with guard.run(name=agent_name, environment="proxy") as run:
                result = await run.invoke(provider, model=model, messages=messages,
                                          max_tokens=payload.get("max_tokens", payload.get("max_completion_tokens", 256)))
        except ProviderResponseError as error:
            return provider_error(error)
        except AgentGuardError as error:
            return policy_error(error)
        except httpx.HTTPError:
            await record_transport_failure(agent_name, model, messages,
                                           payload.get("max_tokens", payload.get("max_completion_tokens", 256)))
            raise HTTPException(502, "Unable to reach the configured model provider") from None
        return JSONResponse(result)

    @app.get("/v1/models")
    @app.get("/proxy/{project_name}/v1/models")
    async def models(request: Request, project_name: str | None = None):
        if project_name is not None and not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", project_name):
            raise HTTPException(400, "Project name may contain letters, numbers, dots, underscores, and hyphens")
        agent_name = project_name or request.headers.get("x-agentguard-agent", "default")[:100]
        try:
            upstream_url = resolve_upstream(agent_name)
        except ValueError as error:
            raise HTTPException(503, str(error)) from None
        if upstream_url is None:
            raise HTTPException(503, "No model upstream configured for this project")
        import httpx
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(validate_upstream_url(upstream_url) + "/models",
                                             headers=request_headers(request))
        except httpx.HTTPError:
            raise HTTPException(502, "Unable to reach the configured model provider") from None
        return Response(response.content, status_code=response.status_code,
                        media_type=response.headers.get("content-type", "application/json").split(";")[0])
