import asyncio
import unittest

from agentguard import AgentGuard, BaseInterceptor


class TerminalInterceptor(BaseInterceptor):
    def __init__(self, fail=False):
        self.fail = fail
        self.finishes = 0
        self.errors = []

    def on_error(self, error):
        self.errors.append(error)
        if self.fail:
            raise ValueError("error callback failed")

    def on_finish(self, context):
        self.finishes += 1
        if self.fail:
            raise ValueError("finish callback failed")


class StreamLifecycleTests(unittest.TestCase):
    def assert_released(self, run, interceptor, succeeded=False):
        self.assertEqual(run.budget._reserved_tokens, 0)
        self.assertEqual(interceptor.finishes, 1)
        terminal = [
            event["event"] for event in run.state.events
            if event["event"] in {"llm.completed", "llm.failed"}
        ]
        self.assertEqual(terminal, ["llm.completed" if succeeded else "llm.failed"])
        for _, concurrency in run.admission._states.values():
            self.assertEqual(concurrency._active, 0)

    def test_sync_provider_error_survives_callback_errors(self):
        for fail in (False, True):
            with self.subTest(fail=fail), AgentGuard().run() as run:
                interceptor = TerminalInterceptor(fail)
                run.add_interceptor(interceptor)
                error = RuntimeError("provider failed")

                def provider(**kwargs):
                    yield "hello"
                    raise error

                with self.assertRaises(RuntimeError) as caught:
                    list(run.invoke(provider, stream=True))
                self.assertIs(caught.exception, error)
                self.assertGreater(run.state.output_tokens, 0)
                self.assert_released(run, interceptor)

    def test_finish_error_runs_callback_once(self):
        with AgentGuard().run() as run:
            interceptor = TerminalInterceptor(True)
            run.add_interceptor(interceptor)
            with self.assertRaisesRegex(ValueError, "finish callback"):
                list(run.invoke(lambda **kwargs: iter(["hello"]), stream=True))
            self.assert_released(run, interceptor, succeeded=True)

    def test_early_close_is_not_success(self):
        with AgentGuard().run() as run:
            interceptor = TerminalInterceptor()
            run.add_interceptor(interceptor)
            stream = run.invoke(lambda **kwargs: iter(["hello", "world"]), stream=True)
            next(stream)
            stream.close()
            self.assert_released(run, interceptor)

    def test_async_provider_error_survives_callback_errors(self):
        async def consume():
            with AgentGuard().run() as run:
                interceptor = TerminalInterceptor(True)
                run.add_interceptor(interceptor)
                error = RuntimeError("provider failed")

                async def provider(**kwargs):
                    yield "hello"
                    raise error

                with self.assertRaises(RuntimeError) as caught:
                    async for chunk in run.invoke(provider, stream=True):
                        self.assertEqual(chunk, "hello")
                self.assertIs(caught.exception, error)
                self.assert_released(run, interceptor)

        asyncio.run(consume())

    def test_awaited_stream_is_settled_only_after_consumption(self):
        async def consume():
            with AgentGuard().run() as run:
                interceptor = TerminalInterceptor()
                run.add_interceptor(interceptor)

                async def chunks():
                    yield "hello"

                async def provider(**kwargs):
                    return chunks()

                stream = await run.invoke(provider, stream=True)
                self.assertEqual(interceptor.finishes, 0)
                self.assertGreater(run.budget._reserved_tokens, 0)
                self.assertEqual([chunk async for chunk in stream], ["hello"])
                self.assert_released(run, interceptor, succeeded=True)

        asyncio.run(consume())
