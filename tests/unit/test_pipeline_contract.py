import asyncio

import pytest

from agentguard import (
    AgentGuard,
    AgentGuardConfig,
    BaseInterceptor,
    BudgetExceeded,
    RateLimitExceeded,
)


class RecordingInterceptor(BaseInterceptor):
    def __init__(self):
        self.calls = []

    def before_request(self, request):
        self.calls.append(("before", request["snapshot"].model))
        updated = dict(request)
        updated["kwargs"] = dict(request["kwargs"])
        updated["kwargs"]["marker"] = "injected"
        return updated

    def after_response(self, response):
        self.calls.append(("after", response))
        return {**response, "intercepted": True}

    def on_start(self, context):
        self.calls.append(("start", context.request_id, context.trace_id, context.run_id, context.attempt))

    def on_finish(self, context):
        self.calls.append(("finish", context.request_id))

    def on_error(self, error):
        self.calls.append(("error", str(error)))


def test_interceptor_can_modify_request_and_response_and_receives_call_ids():
    guard = AgentGuard()
    interceptor = RecordingInterceptor()

    def provider(**kwargs):
        assert kwargs["marker"] == "injected"
        return {"usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    with guard.run() as run:
        run.add_interceptor(interceptor)
        response = run.invoke(provider, model="demo", messages="hello")

    assert response["intercepted"] is True
    assert [item[0] for item in interceptor.calls] == ["before", "start", "after", "finish"]
    start = interceptor.calls[1]
    assert start[1].startswith("req_")
    assert start[2] == run.state.trace_id
    assert start[3] == run.state.run_id
    assert start[4] == 1


def test_request_preparation_applies_output_limit_before_provider_call():
    seen = {}

    def provider(**kwargs):
        seen.update(kwargs)
        return {"usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    guard = AgentGuard(AgentGuardConfig.from_mapping({"token": {"max_output_tokens": 8}}))
    with guard.run() as run:
        run.invoke(provider, model="demo", messages="hello", max_output_tokens=32)

    assert seen["max_output_tokens"] == 8


def test_admission_rejection_does_not_call_provider():
    called = False

    def provider(**kwargs):
        nonlocal called
        called = True
        return {"usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    guard = AgentGuard(AgentGuardConfig.from_mapping({"rate_limit": {"max_concurrency": 0}}))
    with guard.run() as run:
        with pytest.raises(RateLimitExceeded):
            run.invoke(provider, messages="hello", max_output_tokens=1)

    assert called is False
    assert run.state.llm_calls == 0


def test_budget_rejection_releases_admission_before_provider_call():
    guard = AgentGuard(AgentGuardConfig.from_mapping({"token": {"run_token_budget": 1}}))

    with guard.run() as run:
        with pytest.raises(BudgetExceeded) as error:
            run.invoke(lambda **kwargs: {}, messages="hello", max_output_tokens=10)

    assert error.value.reason_code == "run_token_budget"
    assert run.budget._reserved_tokens == 0
    assert run.admission._states == {}


def test_interceptor_can_short_circuit_before_provider():
    class Blocker(BaseInterceptor):
        def before_request(self, request):
            return None

    called = False

    def provider(**kwargs):
        nonlocal called
        called = True
        return {}

    guard = AgentGuard()
    with guard.run() as run:
        run.add_interceptor(Blocker())
        with pytest.raises(BudgetExceeded) as error:
            run.invoke(provider, messages="blocked")

    assert error.value.reason_code == "interceptor_blocked"
    assert called is False


def test_interceptor_error_lifecycle_for_async_call():
    interceptor = RecordingInterceptor()
    guard = AgentGuard()

    async def provider(**kwargs):
        raise RuntimeError("异步供应商失败")

    async def consume():
        with guard.run() as run:
            run.add_interceptor(interceptor)
            await run.invoke(provider, messages="hello")

    with pytest.raises(RuntimeError):
        asyncio.run(consume())

    assert [item[0] for item in interceptor.calls] == ["before", "start", "error", "finish"]


def test_async_cancellation_releases_call_resources_and_finishes_once():
    interceptor = RecordingInterceptor()
    guard = AgentGuard()

    async def provider(**kwargs):
        await asyncio.sleep(10)
        return {"usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    async def consume():
        with guard.run() as run:
            run.add_interceptor(interceptor)
            task = asyncio.create_task(run.invoke(provider, messages="hello"))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return run

    run = asyncio.run(consume())
    assert run.budget._reserved_tokens == 0
    assert [item[0] for item in interceptor.calls] == ["start", "error", "finish"]


def test_sync_stream_passes_each_chunk_through_interceptors_and_finishes_once():
    interceptor = RecordingInterceptor()
    guard = AgentGuard()

    def provider(**kwargs):
        yield {"text": "one"}
        yield {"text": "two"}

    with guard.run() as run:
        run.add_interceptor(interceptor)
        stream = run.invoke(provider, messages="hello", max_output_tokens=10, stream=True)
        chunks = list(stream)

    assert all(chunk["intercepted"] is True for chunk in chunks)
    assert sum(item[0] == "after" for item in interceptor.calls) == 2
    assert sum(item[0] == "finish" for item in interceptor.calls) == 1


def test_async_stream_passes_each_chunk_through_interceptors():
    interceptor = RecordingInterceptor()
    guard = AgentGuard()

    async def provider(**kwargs):
        yield {"text": "one"}
        yield {"text": "two"}

    async def consume():
        with guard.run() as run:
            run.add_interceptor(interceptor)
            stream = run.invoke(provider, messages="hello", max_output_tokens=10, stream=True)
            chunks = [chunk async for chunk in stream]
        return chunks

    chunks = asyncio.run(consume())
    assert [chunk["intercepted"] for chunk in chunks] == [True, True]
    assert sum(item[0] == "finish" for item in interceptor.calls) == 1


def test_provider_call_stage_keeps_generator_lazy_until_consumed():
    events = []

    def provider(**kwargs):
        events.append("调用")
        yield "内容"

    with AgentGuard().run() as run:
        stream = run.invoke(provider, messages="hello", stream=True)
        assert events == []
        assert run.state.output_tokens == 0
        assert next(stream) == "内容"
        assert events == ["调用"]
        stream.close()

    assert run.state.output_tokens == 1
