import asyncio

import pytest

from agentguard import AgentGuard, LoopDetected


def test_repeated_outputs_stop_before_next_tool_side_effect():
    calls = []
    with AgentGuard({"behavior": {"max_repeated_outputs": 2}}).run() as run:
        for _ in range(2):
            run.invoke_tool("read", lambda: calls.append(1) or "same")
        with pytest.raises(LoopDetected): run.invoke_tool("write", lambda: calls.append(2))
        assert calls == [1, 1]
        run.mark_progress("verified-step-1")
        assert run.invoke_tool("next", lambda: "different") == "different"


def test_no_progress_marker_must_change_to_reset_budget():
    with AgentGuard({"behavior": {"max_calls_without_progress": 1}}).run() as run:
        run.mark_progress("a")
        run.invoke(lambda: "one")
        run.mark_progress("a")
        with pytest.raises(LoopDetected): run.invoke(lambda: "two")
        run.mark_progress("b")
        assert run.invoke(lambda: "two") == "two"
        assert not any("a" == value or "b" == value for event in run.state.events for key, value in event.items() if key == "marker")


def test_recursive_tool_depth_blocks_and_cleans_up():
    with AgentGuard({"tools": {"max_depth": 2}}).run() as run:
        def recursive(): return run.invoke_tool("nested", recursive)
        with pytest.raises(LoopDetected): run.invoke_tool("root", recursive)
        assert run.state.tool_calls == 2
        assert run.behavior._active_tools == 0
        assert run.invoke_tool("independent", lambda: 42) == 42


def test_async_depth_and_concurrent_limit_cancellation_release():
    async def scenario():
        gate, entered = asyncio.Event(), asyncio.Event()
        async def slow():
            entered.set()
            await gate.wait()
        async with AgentGuard({"tools": {"max_concurrent": 1, "max_depth": 1}}).run() as run:
            task = asyncio.create_task(run.invoke_tool("slow", slow))
            await entered.wait()
            with pytest.raises(LoopDetected): await run.invoke_tool("other", slow)
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
            assert run.behavior._active_tools == 0
            async def nested(): return await run.invoke_tool("child", slow)
            with pytest.raises(LoopDetected): await run.invoke_tool("parent", nested)
            assert run.behavior._active_tools == 0
    asyncio.run(scenario())
