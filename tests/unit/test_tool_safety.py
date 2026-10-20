import asyncio

import pytest

from agentguard import AgentGuard, ExecutionTimeout, LoopDetected, ToolApprovalDenied


def test_required_idempotency_key_blocks_before_side_effect_and_duplicate_key():
    effects = []
    guard = AgentGuard({"tools": {"idempotency_required_for": ["charge"], "side_effects": {"charge": "irreversible"}}})
    with guard.run() as run:
        with pytest.raises(LoopDetected):
            run.invoke_tool("charge", lambda: effects.append(1))
        run.invoke_tool("charge", lambda: effects.append(1), idempotency_key="payment-1")
        with pytest.raises(LoopDetected):
            run.invoke_tool("charge", lambda: effects.append(1), idempotency_key="payment-1")
    assert effects == [1]
    events = run.state.events
    assert any(event["event"] == "tool.completed" and event.get("idempotency_key_hash") for event in events)
    assert "payment-1" not in repr(events)


def test_required_approval_rejects_and_approved_call_records_only_outcome():
    calls = []
    guard = AgentGuard({"tools": {"approval_required_for": ["delete"], "side_effects": {"delete": "irreversible"}}})
    with guard.run() as run:
        with pytest.raises(ToolApprovalDenied):
            run.invoke_tool("delete", lambda: calls.append(1))
        with pytest.raises(ToolApprovalDenied):
            run.invoke_tool("delete", lambda: calls.append(1), approval=lambda: False)
        run.invoke_tool("delete", lambda: calls.append(1), approval=lambda: True, idempotency_key="d-1")
    assert calls == [1]
    assert any(event.get("approval_outcome") == "approved" for event in run.state.events)


def test_async_approval_is_awaited_and_timeout_is_cooperative():
    async def scenario():
        calls = []
        guard = AgentGuard({"tools": {"approval_required_for": ["write"], "approval_timeout_seconds": 0.01}})
        async def approve(*_):
            await asyncio.sleep(0)
            return True
        async def write():
            calls.append(1)
            return "ok"
        async with guard.run() as run:
            assert await run.invoke_tool("write", write, approval=approve, idempotency_key="w-1") == "ok"
        assert calls == [1]
    asyncio.run(scenario())


@pytest.mark.parametrize("key", ["", "  ", "x" * 257, 123, True])
def test_invalid_idempotency_keys_are_rejected_without_recording_raw_value(key):
    effects = []
    with AgentGuard().run() as run:
        with pytest.raises(LoopDetected):
            run.invoke_tool("write", lambda: effects.append(1), idempotency_key=key)
    assert effects == []
    assert run.state.events[-2]["reason_code"] == "invalid_idempotency_key"
    assert run.state.tool_calls == 0


def test_approval_callback_error_is_not_retried_or_logged():
    calls = []
    effects = []
    def approval():
        calls.append(1)
        raise TypeError("private callback error")
    with AgentGuard({"tools": {"approval_required_for": ["write"]}}).run() as run:
        with pytest.raises(TypeError, match="private callback error"):
            run.invoke_tool("write", lambda: effects.append(1), approval=approval)
    assert calls == [1] and effects == []
    assert "private callback error" not in repr(run.state.events)
    assert any(e.get("reason_code") == "approval_failed" for e in run.state.events)


def test_sync_approval_timeout_blocks_tool_after_callback_returns(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("agentguard.application.deadlines.monotonic", lambda: now[0])
    effects = []
    def approval():
        now[0] = 2.0
        return True
    config = {"tools": {"approval_required_for": ["write"], "approval_timeout_seconds": 1}}
    with AgentGuard(config).run() as run:
        with pytest.raises(ExecutionTimeout) as error:
            run.invoke_tool("write", lambda: effects.append(1), approval=approval)
    assert error.value.scope == "approval" and effects == []
    assert any(e.get("reason_code") == "approval_timed_out" for e in run.state.events)


def test_sync_tool_rejects_async_approval_without_running_callback_body():
    callbacks, effects = [], []
    async def approval():
        callbacks.append(1)
        return True
    with AgentGuard({"tools": {"approval_required_for": ["write"]}}).run() as run:
        with pytest.raises(ToolApprovalDenied):
            run.invoke_tool("write", lambda: effects.append(1), approval=approval)
    assert callbacks == [] and effects == []


def test_approval_receives_only_tool_name_and_key_hash_and_duplicate_skips_callback():
    callbacks = []
    def approval(*args):
        callbacks.append(args)
        return True
    with AgentGuard({"tools": {"approval_required_for": ["write"]}}).run() as run:
        run.invoke_tool("write", lambda: None, approval=approval, idempotency_key="private-key")
        with pytest.raises(LoopDetected):
            run.invoke_tool("write", lambda: None, approval=approval, idempotency_key="private-key")
    assert len(callbacks) == 1
    assert callbacks[0][0] == "write" and len(callbacks[0][1]) == 64
    assert "private-key" not in repr(callbacks) + repr(run.state.events)


@pytest.mark.parametrize("outcome", ["denied", "timed_out", "cancelled"])
def test_async_approval_failure_cleans_up_and_key_can_be_used_after_approval(outcome):
    async def scenario():
        effects = []
        cleaned = asyncio.Event()
        entered = asyncio.Event()
        async def approval():
            try:
                entered.set()
                if outcome == "denied":
                    return False
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        async def write():
            effects.append(1)
            return "ok"
        guard = AgentGuard({"tools": {"approval_required_for": ["write"], "approval_timeout_seconds": 0.1},
                            "rate_limit": {"max_concurrency": 1}})
        async with guard.run() as run:
            task = asyncio.create_task(run.invoke_tool("write", write, approval=approval, idempotency_key="key"))
            try:
                await asyncio.wait_for(entered.wait(), 2)
            except BaseException:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
            if outcome == "cancelled":
                task.cancel()
            expected = {"denied": ToolApprovalDenied, "timed_out": ExecutionTimeout,
                        "cancelled": asyncio.CancelledError}[outcome]
            with pytest.raises(expected):
                await task
            assert cleaned.is_set() and effects == [] and not run._pending
            assert any(e.get("reason_code") == f"approval_{outcome}" for e in run.state.events)
            assert await run.invoke_tool("write", write, approval=lambda: True, idempotency_key="key") == "ok"
            assert effects == [1] and not run._pending
    asyncio.run(scenario())


def test_concurrent_async_approval_cannot_execute_duplicate_key_twice():
    async def scenario():
        effects = []
        barrier = asyncio.Event()
        arrivals = 0
        async def approval(*_):
            nonlocal arrivals
            arrivals += 1
            if arrivals == 2:
                barrier.set()
            await barrier.wait()
            return True
        async def write():
            effects.append(1)
            await asyncio.sleep(0)
            return "ok"
        async with AgentGuard({"tools": {"approval_required_for": ["write"]}}).run() as run:
            results = await asyncio.gather(*[
                run.invoke_tool("write", write, approval=approval, idempotency_key="shared-key")
                for _ in range(2)
            ], return_exceptions=True)
            assert results.count("ok") == 1 and sum(isinstance(r, LoopDetected) for r in results) == 1
            assert effects == [1] and not run._pending
    asyncio.run(asyncio.wait_for(scenario(), 2))


def test_tool_failure_keeps_idempotency_key_consumed():
    effects = []
    def write():
        effects.append(1)
        raise RuntimeError("response lost after side effect")
    with AgentGuard().run() as run:
        with pytest.raises(RuntimeError):
            run.invoke_tool("write", write, idempotency_key="key")
        with pytest.raises(LoopDetected):
            run.invoke_tool("write", write, idempotency_key="key")
    assert effects == [1]
