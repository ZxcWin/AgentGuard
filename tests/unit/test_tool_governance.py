import asyncio
from concurrent.futures import ThreadPoolExecutor
import inspect
import unittest

from agentguard import AgentGuard
from agentguard.tools import BehaviorFingerprint, LoopDetected, ToolGuard


class ToolGovernanceTests(unittest.TestCase):
    def test_equivalent_arguments_block_before_side_effect(self):
        effects = []
        guard = ToolGuard(BehaviorFingerprint(max_repeated_calls=1))

        def tool(payload, enabled=True):
            effects.append(payload)

        wrapped = guard.wrap("write", tool)
        wrapped({"first": 1, "second": 2})
        with self.assertRaises(LoopDetected):
            wrapped(enabled=True, payload={"second": 2, "first": 1})
        self.assertEqual(len(effects), 1)
        self.assertEqual(guard.calls, 1)
        self.assertEqual(guard.fingerprint.steps, 1)
        self.assertEqual(inspect.signature(wrapped), inspect.signature(tool))

    def test_invalid_arguments_do_not_consume_limits(self):
        guard = ToolGuard(max_calls=1)
        wrapped = guard.wrap("tool", lambda value: value)
        with self.assertRaises(TypeError):
            wrapped()
        circular = []
        circular.append(circular)
        with self.assertRaises(ValueError):
            wrapped(circular)
        self.assertEqual(guard.calls, 0)
        self.assertEqual(wrapped(1), 1)

    def test_container_types_are_distinct(self):
        fingerprint = BehaviorFingerprint(max_repeated_calls=1)
        for value in ([1], (1,), {1}, frozenset({1})):
            fingerprint.check("tool", value)
        self.assertEqual(fingerprint.steps, 4)

    def test_threaded_total_limit_is_atomic(self):
        guard = ToolGuard(max_calls=5)
        wrapped = guard.wrap("tool", lambda value: value)

        def invoke(value):
            try:
                wrapped(value)
                return True
            except LoopDetected:
                return False

        with ThreadPoolExecutor(max_workers=8) as executor:
            self.assertEqual(sum(executor.map(invoke, range(100))), 5)
        self.assertEqual(guard.calls, 5)

    def test_async_wrapper_admits_only_when_awaited(self):
        async def tool(value):
            return value

        guard = ToolGuard(max_calls=1)
        wrapped = guard.wrap("tool", tool)
        self.assertTrue(inspect.iscoroutinefunction(wrapped))
        unused = wrapped(1)
        unused.close()
        self.assertEqual(guard.calls, 0)
        self.assertEqual(asyncio.run(wrapped(2)), 2)

    def test_invalid_limits(self):
        for value in (-1, True, 1.5):
            with self.assertRaises(ValueError):
                ToolGuard(max_calls=value)
            with self.assertRaises(ValueError):
                BehaviorFingerprint(max_steps=value)

    def assert_finished(self, run):
        events = [event for event in run.state.events if event["event"] == "tool.finished"]
        self.assertEqual(len(events), 1)
        self.assertNotIn("private payload", repr(run.state.events))
        for _, concurrency in run.admission._states.values():
            self.assertEqual(concurrency._active, 0)

    def test_sync_failure_records_duration(self):
        error = RuntimeError("private payload")

        def tool():
            raise error

        with AgentGuard().run() as run:
            with self.assertRaises(RuntimeError) as caught:
                run.invoke_tool("tool", tool)
            self.assertIs(caught.exception, error)
            self.assert_finished(run)

    def test_async_cancellation_records_duration(self):
        async def consume():
            started = asyncio.Event()

            async def tool():
                started.set()
                await asyncio.Event().wait()

            with AgentGuard().run() as run:
                task = asyncio.create_task(run.invoke_tool("tool", tool))
                await started.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assert_finished(run)

        asyncio.run(consume())
