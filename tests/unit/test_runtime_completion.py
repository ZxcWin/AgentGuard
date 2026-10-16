import asyncio
import inspect
import time
import warnings

import pytest

from agentguard import AgentGuard, BaseInterceptor, ExecutionTimeout, current_run
from agentguard.tools import LoopDetected


def assert_released(run):
    assert run.budget._reserved_tokens == 0
    assert not run._pending
    assert all(limiter._active == 0 for _, limiter in run.admission._states.values())


class TrackingStream:
    def __init__(self):
        self.closes = 0
        self.items = iter(['abcd', 'efgh'])
    def __iter__(self): return self
    def __next__(self): return next(self.items)
    def close(self): self.closes += 1


def test_unconsumed_stream_close_releases_and_propagates_once():
    with AgentGuard().run() as run:
        source = TrackingStream()
        stream = run.invoke(lambda **kwargs: source, stream=True)
        stream.close()
        stream.close()
        assert source.closes == 1
        assert run.state.total_tokens == 0
        assert_released(run)


def test_run_exit_closes_abandoned_stream_before_publishing():
    saved = []
    class Store:
        def save(self, state, config_version): saved.append([row['event'] for row in state.events])
    source = TrackingStream()
    with AgentGuard(run_store=Store()).run() as run:
        stream = run.invoke(lambda **kwargs: source, stream=True)
        next(stream)
    assert source.closes == 1
    assert saved[-1][-2:] == ['llm.failed', 'run.completed']
    assert_released(run)
    with pytest.raises(RuntimeError, match='closed'):
        run.invoke(lambda: None)


def test_sync_callbacks_do_not_replace_original_or_finish_twice():
    class Failing(BaseInterceptor):
        def __init__(self): self.finishes = 0
        def on_error(self, error): raise ValueError('callback')
        def on_finish(self, context):
            self.finishes += 1
            raise ValueError('callback')
    error = RuntimeError('provider error')
    with AgentGuard().run() as run:
        interceptor = Failing()
        run.add_interceptor(interceptor)
        def provider(): raise error
        with pytest.raises(RuntimeError) as caught:
            run.invoke(provider)
        assert caught.value is error
        assert interceptor.finishes == 1
        assert_released(run)


def test_start_callback_failure_releases_reservations():
    class Failing(BaseInterceptor):
        def on_start(self, context): raise ValueError('start failed')
    with AgentGuard().run() as run:
        run.add_interceptor(Failing())
        with pytest.raises(ValueError, match='start failed'):
            run.invoke(lambda: None)
        assert_released(run)


def test_finish_failure_has_one_successful_terminal_event():
    class Failing(BaseInterceptor):
        def __init__(self): self.finishes = 0
        def on_finish(self, context):
            self.finishes += 1
            raise ValueError('finish failed')
    with AgentGuard().run() as run:
        interceptor = Failing()
        run.add_interceptor(interceptor)
        with pytest.raises(ValueError, match='finish failed'):
            run.invoke(lambda: {'usage': {'input_tokens': 1}})
        assert interceptor.finishes == 1
        assert [row['event'] for row in run.state.events if row['event'] in {'llm.completed', 'llm.failed'}] == ['llm.completed']
        assert_released(run)


def test_async_run_exit_drains_unconsumed_and_partial_streams():
    async def execute():
        class Source:
            closes = 0
            def __aiter__(self): return self
            async def __anext__(self): return 'abcd'
            async def aclose(self): self.closes += 1
        for consume in (False, True):
            source = Source()
            async with AgentGuard().run() as run:
                stream = run.invoke(lambda **kwargs: source, stream=True)
                if consume: await anext(stream)
            assert source.closes == 1
            assert_released(run)
    asyncio.run(execute())


def test_unawaited_provider_is_closed_without_runtime_warning():
    async def provider(): return {}
    with warnings.catch_warnings(record=True) as captured:
        with AgentGuard().run() as run:
            pending = run.invoke(provider)
        pending.close()
        assert_released(run)
        assert not captured


def test_automatic_wrapper_lifetime_follows_async_response_and_stream():
    snapshots = []
    class Store:
        def save(self, state, version): snapshots.append(state.summary())
    guard = AgentGuard(run_store=Store())
    class Client:
        async def create(self, **kwargs):
            assert current_run() is not None
            if kwargs.get('stream'):
                async def chunks():
                    assert current_run() is not None
                    yield 'abcd'
                return chunks()
            return {'usage': {'input_tokens': 2, 'output_tokens': 1}}
    async def execute():
        wrapped = guard.wrap(Client())
        await wrapped.create()
        assert snapshots[-1]['status'] == 'completed'
        assert snapshots[-1]['total_tokens'] == 3
        stream = await wrapped.create(stream=True)
        assert snapshots[-1]['status'] == 'running'
        assert [chunk async for chunk in stream] == ['abcd']
        assert snapshots[-1]['status'] == 'completed'
        assert current_run() is None
    asyncio.run(execute())


def test_cumulative_usage_is_not_double_counted_for_stream_budget():
    with AgentGuard({'token': {'run_token_budget': 15}}).run() as run:
        chunks = [{'usage': {'input_tokens': 1, 'output_tokens': 5}}, {'usage': {'input_tokens': 1, 'output_tokens': 10}}]
        assert list(run.invoke(lambda **kwargs: iter(chunks), stream=True, max_tokens=10)) == chunks
    assert run.state.total_tokens == 11


def test_run_tool_limits_block_equivalent_arguments_before_effect():
    effects = []
    with AgentGuard({'tools': {'max_repeated_calls': 1, 'max_calls': 5}}).run() as run:
        def write(value, enabled=True): effects.append(value)
        run.invoke_tool('write', write, {'a': 1, 'b': 2})
        with pytest.raises(LoopDetected):
            run.invoke_tool('write', write, value={'b': 2, 'a': 1}, enabled=True)
        assert len(effects) == 1
        assert run.state.tool_calls == 1
        assert all(len(key) == 64 for key in run.tool_guard.fingerprint._calls)


def test_per_tool_limit_and_safe_result_summary():
    with AgentGuard({'tools': {'per_tool_limits': {'read': 1}, 'side_effects': {'read': 'read'}}}).run() as run:
        run.invoke_tool('read', lambda: 'private payload')
        with pytest.raises(LoopDetected): run.invoke_tool('read', lambda: 'private payload')
        event = next(item for item in run.state.events if item['event'] == 'tool.completed')
        assert event['result_type'] == 'str'
        assert event['result_size'] == 15
        assert 'private payload' not in repr(run.state.events)
        assert_released(run)


def test_async_tool_never_admits_before_await():
    async def tool(): return None
    with AgentGuard().run() as run:
        pending = run.invoke_tool('tool', tool)
        pending.close()
        assert run.state.tool_calls == 0
        assert run.tool_guard.calls == 0


def test_deadlines_reject_expired_run_without_provider_execution():
    with pytest.raises(ExecutionTimeout), AgentGuard({'timeout': {'run_seconds': 0}}).run() as run:
        run.invoke(lambda: pytest.fail('should not execute'))
    assert run.state.status == 'timed_out'


def test_async_llm_timeout_cancels_provider_and_releases():
    async def execute():
        stopped = asyncio.Event()
        async def provider():
            try: await asyncio.Event().wait()
            finally: stopped.set()
        with pytest.raises(ExecutionTimeout), AgentGuard({'timeout': {'llm_seconds': .02}}).run() as run:
            await run.invoke(provider)
        assert stopped.is_set()
        assert run.state.status == 'timed_out'
        assert_released(run)
    asyncio.run(execute())


def test_async_stream_timeout_propagates_close():
    async def execute():
        closed = []
        async def chunks():
            try:
                yield 'abcd'
                await asyncio.Event().wait()
            finally: closed.append(True)
        async with AgentGuard({'timeout': {'llm_seconds': .02}}).run() as run:
            stream = run.invoke(lambda **kwargs: chunks(), stream=True)
            assert await anext(stream) == 'abcd'
            with pytest.raises(ExecutionTimeout): await anext(stream)
        assert closed == [True]
        assert_released(run)
    asyncio.run(execute())


def test_provider_timeout_is_not_mislabeled_as_execution_deadline():
    async def execute():
        original = TimeoutError('provider socket timeout')
        async def provider(): raise original
        async with AgentGuard({'timeout': {'llm_seconds': 30}}).run() as run:
            with pytest.raises(TimeoutError) as caught:
                await run.invoke(provider)
            assert caught.value is original
            assert not any(row['event'] == 'llm.timed_out' for row in run.state.events)
            assert_released(run)
    asyncio.run(execute())


def test_async_late_stream_is_closed_before_timeout_propagates(monkeypatch):
    from agentguard.application import deadlines
    clock = [100.0]
    monkeypatch.setattr(deadlines, 'monotonic', lambda: clock[0])
    closed = []
    class Source:
        def __aiter__(self): return self
        async def __anext__(self): return 'chunk'
        async def aclose(self): closed.append(True)
    async def provider(**kwargs):
        clock[0] += 1
        return Source()
    async def execute():
        async with AgentGuard({'timeout': {'llm_seconds': .5}}).run() as run:
            with pytest.raises(ExecutionTimeout): await run.invoke(provider, stream=True)
            assert closed == [True]
            assert_released(run)
    asyncio.run(execute())


def test_sync_timeout_reports_late_return_without_background_work(monkeypatch):
    from agentguard.application import deadlines
    clock = [100.0]
    monkeypatch.setattr(deadlines, 'monotonic', lambda: clock[0])
    effects = []
    def provider():
        clock[0] += .01
        effects.append(True)
        return {}
    with AgentGuard({'timeout': {'llm_seconds': .001}}).run() as run:
        with pytest.raises(ExecutionTimeout): run.invoke(provider)
        assert effects == [True]
        assert_released(run)


@pytest.mark.parametrize('close_fails', [False, True])
def test_sync_timeout_closes_late_provider_stream_even_if_close_fails(monkeypatch, close_fails):
    from agentguard.application import deadlines
    clock = [100.0]
    monkeypatch.setattr(deadlines, 'monotonic', lambda: clock[0])
    source = TrackingStream()
    if close_fails:
        def fail_close():
            source.closes += 1
            raise ValueError('close failed')
        source.close = fail_close
    def provider(**kwargs):
        clock[0] += 1
        return source
    with AgentGuard({'timeout': {'llm_seconds': .5}}).run() as run:
        with pytest.raises(ExecutionTimeout): run.invoke(provider, stream=True)
        assert source.closes == 1
        assert_released(run)


def test_request_preparation_cannot_extend_llm_deadline(monkeypatch):
    from agentguard.application import deadlines
    clock = [100.0]
    monkeypatch.setattr(deadlines, 'monotonic', lambda: clock[0])
    class Slow(BaseInterceptor):
        def before_request(self, request):
            clock[0] += 1
            return request
    with AgentGuard({'timeout': {'llm_seconds': .5}}).run() as run:
        run.add_interceptor(Slow())
        with pytest.raises(ExecutionTimeout):
            run.invoke(lambda: pytest.fail('expired request must not execute'))
        assert_released(run)


def test_response_interceptor_cannot_replace_billable_usage():
    class Replace(BaseInterceptor):
        def after_response(self, response): return {'usage': {'input_tokens': 0}, 'changed': True}
    with AgentGuard().run() as run:
        run.add_interceptor(Replace())
        response = run.invoke(lambda: {'usage': {'input_tokens': 12, 'output_tokens': 8}})
        assert response['changed'] is True
        assert run.state.total_tokens == 20


def test_async_guard_and_proxy_preserve_coroutine_function_protocol():
    guard = AgentGuard()
    @guard.guard
    async def provider(value=1): return {'value': value}
    class Client:
        async def create(self, value=1): return {'value': value}
    wrapped = guard.wrap(Client()).create
    assert inspect.iscoroutinefunction(provider)
    assert inspect.iscoroutinefunction(wrapped)
    assert inspect.signature(wrapped) == inspect.signature(Client().create)
    async def execute():
        assert (await provider(2))['value'] == 2
        assert (await wrapped(3))['value'] == 3
        assert current_run() is None
    asyncio.run(execute())


def test_child_run_cannot_extend_parent_deadline():
    with AgentGuard({'timeout': {'run_seconds': .05}}).run() as parent:
        with AgentGuard({'timeout': {'run_seconds': 10}}).run() as child:
            assert child.deadline.expires_at == parent.deadline.expires_at


def test_tool_timeout_marks_possibly_executed_write_and_never_retries():
    async def execute():
        effects = []
        async def tool():
            effects.append(True)
            await asyncio.Event().wait()
        async with AgentGuard({'timeout': {'tool_seconds': .02}}).run() as run:
            with pytest.raises(ExecutionTimeout): await run.invoke_tool('write', tool)
        assert effects == [True]
        event = next(row for row in run.state.events if row['event'] == 'tool.timed_out')
        assert event['execution_may_have_completed'] is True
        assert_released(run)
    asyncio.run(execute())


def test_nested_calls_link_to_parent_request():
    with AgentGuard().run() as run:
        def tool(): return run.invoke(lambda: {})
        run.invoke_tool('lookup', tool)
    started = next(row for row in run.state.events if row['event'] == 'tool.started')
    nested = next(row for row in run.state.events if row['event'] == 'llm.requested')
    assert nested['parent_id'] == started['request_id']


def test_async_admission_wait_does_not_block_stream_releasing_slot():
    async def execute():
        async with AgentGuard({'rate_limit': {'max_concurrency': 1, 'wait_timeout_seconds': .5}}).run() as run:
            held = run.invoke(lambda **kwargs: iter(['hello']), stream=True)
            async def provider(): return {'usage': {'input_tokens': 1}}
            pending = asyncio.create_task(run.invoke(provider))
            await asyncio.sleep(.02)
            assert not pending.done()
            held.close()
            assert (await pending)['usage']['input_tokens'] == 1
            assert_released(run)
    asyncio.run(execute())


def test_async_tool_admission_wait_can_be_cancelled():
    async def execute():
        async with AgentGuard({'rate_limit': {'max_concurrency': 1, 'wait_timeout_seconds': .5}}).run() as run:
            held = run.invoke(lambda **kwargs: iter(['hello']), stream=True)
            async def tool(): pytest.fail('must not execute')
            pending = asyncio.create_task(run.invoke_tool('write', tool))
            await asyncio.sleep(.02)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError): await pending
            held.close()
            assert run.state.tool_calls == 0
            assert_released(run)
    asyncio.run(execute())


def test_closing_completed_awaitable_never_cancels_owning_task():
    async def execute():
        async with AgentGuard().run() as run:
            async def provider(): return {}
            response = run.invoke(provider)
            await response
            response.close()
            await asyncio.sleep(0)
            assert_released(run)
    asyncio.run(execute())


def test_automatic_nested_async_calls_do_not_complete_run_early():
    snapshots = []
    class Store:
        def save(self, state, version): snapshots.append(state.status)
    guard = AgentGuard(run_store=Store())
    class Client:
        async def create(self):
            run = current_run()
            await run.invoke(self.inner)
            assert snapshots[-1] == 'running'
            assert not run._closed
            return {'usage': {'input_tokens': 3}}
        async def inner(self): return {}
    asyncio.run(guard.wrap(Client()).create())
    assert snapshots == ['running', 'completed']


def test_run_checks_deadline_on_exit_without_model_calls(monkeypatch):
    from agentguard.application import deadlines
    clock = [0.0]
    monkeypatch.setattr(deadlines, 'monotonic', lambda: clock[0])
    with pytest.raises(ExecutionTimeout), AgentGuard({'timeout': {'run_seconds': 2}}).run() as run:
        clock[0] = 3
    assert run.state.status == 'timed_out'


def test_nested_tool_inherits_llm_deadline(monkeypatch):
    from agentguard.application import deadlines
    clock = [0.0]
    monkeypatch.setattr(deadlines, 'monotonic', lambda: clock[0])
    with AgentGuard({'timeout': {'llm_seconds': 1, 'tool_seconds': 100}}).run() as run:
        def tool(): clock[0] = 2
        def provider(): return run.invoke_tool('read', tool)
        with pytest.raises(ExecutionTimeout) as error: run.invoke(provider)
        assert error.value.scope == 'llm'
        assert_released(run)


@pytest.mark.parametrize('values', [
    {'tools': {'max_calls': True}}, {'tools': {'max_steps': -1}},
    {'tools': {'per_tool_limits': {'write': None}}}, {'tools': {'side_effects': {'write': []}}},
    {'timeout': {'llm_seconds': float('inf')}}, {'timeout': {'tool_seconds': -1}},
])
def test_execution_config_rejects_invalid_values(values):
    from agentguard import AgentGuardConfig, ConfigurationError
    with pytest.raises(ConfigurationError): AgentGuardConfig.from_mapping(values)


def test_async_exit_cancels_task_scheduled_but_not_started():
    async def execute():
        async def provider(): pytest.fail('must not execute after Run exit')
        async with AgentGuard().run() as run:
            task = asyncio.create_task(run.invoke(provider))
        with pytest.raises(asyncio.CancelledError): await task
        assert_released(run)
    asyncio.run(execute())
