import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from agentguard import AgentGuard, BaseInterceptor, BudgetExceeded, ExecutionTimeout
from agentguard.config.schema import AgentGuardConfig
from agentguard.domain.exceptions import ConfigurationError
from agentguard.policies.retry import retry_after, retry_reason


class UpstreamError(Exception):
    def __init__(self, status=503, headers=None):
        super().__init__('provider secret')
        self.status_code = status
        self.headers = headers or {}


def guard(**values):
    return AgentGuard({'retry': {'max_retries': 2, 'initial_delay_seconds': 0, 'max_delay_seconds': 10, **values}})


def assert_released(run):
    assert run.budget._reserved_tokens == 0
    assert run.budget._reserved_retry_tokens == 0
    assert not run._pending
    assert run._retry_sessions == 0
    assert all(limiter._active == 0 for _, limiter in run.admission._states.values())


@pytest.mark.parametrize('error, reason', [
    (UpstreamError(429), 'http_429'), (UpstreamError(502), 'http_502'),
    (UpstreamError(401), None), (UpstreamError(501), None), (ValueError(), None),
    (ConnectionError(), 'connection_error'), (TimeoutError(), 'provider_timeout'),
    (ExecutionTimeout('run'), None), (BudgetExceeded('limit'), None),
])
def test_classification(error, reason):
    assert retry_reason(error) == reason


@pytest.mark.parametrize('config', [
    {'max_retries': True}, {'max_retries': -1}, {'max_retries': 11}, {'max_retries': None},
    {'initial_delay_seconds': float('nan')}, {'max_elapsed_seconds': 0},
    {'max_delay_seconds': None}, {'initial_delay_seconds': 2, 'max_delay_seconds': 1}, {'jitter': 'false'},
])
def test_invalid_retry_configuration(config):
    with pytest.raises(ConfigurationError): AgentGuardConfig.from_mapping({'retry': config})


def test_retry_after_seconds_http_date_and_invalid_values():
    now = datetime(2026, 10, 15, tzinfo=timezone.utc)
    assert retry_after(UpstreamError(headers={'Retry-After': '2.5'}), now) == 2.5
    assert retry_after(UpstreamError(headers={'retry-after': format_datetime(now + timedelta(seconds=5))}), now) == 5
    assert retry_after(UpstreamError(headers={'Retry-After': 'not a date'}), now) is None
    assert retry_after(UpstreamError(headers={'Retry-After': '-5'}), now) == 0


def test_sync_retry_records_attempts_and_charges_uncertain_failed_usage():
    attempts = []
    def provider(**kwargs):
        attempts.append(kwargs)
        if len(attempts) < 3: raise UpstreamError()
        return {'usage': {'input_tokens': 2, 'output_tokens': 3}}
    with guard().run() as run:
        assert run.invoke(provider, max_tokens=3)['usage']['input_tokens'] == 2
    assert run.state.llm_calls == 3
    assert run.state.retry_count == 2
    assert run.state.total_tokens == 11  # Two conservative failed estimates + actual success.
    requested = [e for e in run.state.events if e['event'] == 'llm.requested']
    assert [e['attempt'] for e in requested] == [1, 2, 3]
    assert len({e['request_id'] for e in requested}) == 3
    assert run.budget._spent_retry_tokens == 8
    assert_released(run)


def test_disabled_retry_preserves_original_behavior():
    calls = []
    error = UpstreamError()
    def provider(): calls.append(True); raise error
    with AgentGuard().run() as run:
        with pytest.raises(UpstreamError) as caught: run.invoke(provider)
        assert caught.value is error
    assert calls == [True]
    assert run.state.total_tokens == 0
    assert run.state.retry_count == 0
    assert_released(run)


@pytest.mark.parametrize('status', [400, 401, 403, 404])
def test_client_errors_are_not_retried(status):
    with guard().run() as run:
        with pytest.raises(UpstreamError): run.invoke(lambda: (_ for _ in ()).throw(UpstreamError(status)))
    assert run.state.llm_calls == 1
    assert_released(run)


def test_exhaustion_preserves_last_error_and_bounded_attempts():
    error = UpstreamError()
    def provider(): raise error
    with guard(max_retries=1).run() as run:
        with pytest.raises(UpstreamError) as caught: run.invoke(provider)
        assert caught.value is error
    assert run.state.llm_calls == 2
    assert run.state.events[-2]['reason_code'] == 'max_retries'
    assert_released(run)


def test_retry_after_over_limit_never_retries_early():
    error = UpstreamError(429, {'Retry-After': '1000'})
    with guard().run() as run:
        with pytest.raises(UpstreamError): run.invoke(lambda: (_ for _ in ()).throw(error))
    assert run.state.llm_calls == 1
    assert run.state.events[-2]['reason_code'] == 'retry_after_limit'


def test_delay_has_exponential_cap_jitter_and_no_held_admission(monkeypatch):
    from agentguard.application import retries
    waits, calls = [], []
    monkeypatch.setattr(retries, 'uniform', lambda low, high: high / 2)
    def wait(seconds):
        assert all(limiter._active == 0 for _, limiter in run.admission._states.values())
        assert run.budget._reserved_tokens == 0
        waits.append(seconds)
    monkeypatch.setattr(retries, 'sleep', wait)
    def provider():
        calls.append(True)
        if len(calls) < 3: raise ConnectionError()
        return {}
    with guard(initial_delay_seconds=2, max_delay_seconds=3).run() as run: run.invoke(provider)
    assert waits == [1, 1.5]
    assert_released(run)


def test_independent_retry_budget_blocks_before_provider():
    cfg = {'retry': {'max_retries': 2, 'initial_delay_seconds': 0},
           'token': {'run_token_budget': 100, 'max_output_tokens': 30, 'reservation_buffer': 0, 'retry_budget_ratio': .2}}
    with AgentGuard(cfg).run() as run:
        with pytest.raises(BudgetExceeded, match='重试'): run.invoke(lambda **kwargs: (_ for _ in ()).throw(ConnectionError()))
    assert run.state.llm_calls == 1
    assert run.state.total_tokens == 30
    assert_released(run)


def test_stream_and_tools_never_automatically_retry():
    def fail(**kwargs): raise ConnectionError()
    with guard().run() as run:
        with pytest.raises(ConnectionError): run.invoke(fail, stream=True)
        with pytest.raises(ConnectionError): run.invoke_tool('write', fail)
    assert run.state.llm_calls == run.state.tool_calls == 1
    assert run.state.retry_count == 0


def test_nested_tool_suppresses_parent_retry():
    effects = []
    with guard().run() as run:
        def provider():
            run.invoke_tool('write', lambda: effects.append(True))
            raise ConnectionError()
        with pytest.raises(ConnectionError): run.invoke(provider)
    assert effects == [True]
    assert run.state.retry_count == 0
    assert run.state.events[-2]['reason_code'] == 'nested_tool_executed'


def test_interceptor_connection_errors_never_trigger_provider_retry():
    class Interceptor(BaseInterceptor):
        def after_response(self, response): raise ConnectionError('callback')
    with guard().run() as run:
        run.add_interceptor(Interceptor())
        with pytest.raises(ConnectionError): run.invoke(lambda: {})
    assert run.state.llm_calls == 1
    assert_released(run)


@pytest.mark.parametrize('sync_factory', [False, True])
def test_async_and_awaitable_factory_retry_and_automatic_run_completion(sync_factory):
    async def execute():
        attempts, saved = [], []
        class Store:
            def save(self, state, version): saved.append((state.status, state.retry_count))
        async def provider():
            attempts.append(True)
            if len(attempts) == 1: raise ConnectionError()
            return {'ok': True}
        fn = (lambda: provider()) if sync_factory else provider
        configured = AgentGuard({'retry': {'max_retries': 1, 'initial_delay_seconds': 0}}, run_store=Store())
        assert (await configured.guard(fn)())['ok'] is True
        assert attempts == [True, True]
        assert saved == [('running', 0), ('completed', 1)]
    asyncio.run(execute())


def test_async_cancellation_during_backoff_cleans_up():
    async def execute():
        started = asyncio.Event()
        async def provider(): started.set(); raise ConnectionError()
        async with guard(initial_delay_seconds=10, jitter=False).run() as run:
            task = asyncio.create_task(run.invoke(provider))
            await started.wait()
            await asyncio.sleep(.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
            assert run.state.llm_calls == 1
            assert_released(run)
    asyncio.run(execute())


def test_retry_elapsed_budget_keeps_original_error_when_delay_does_not_fit(monkeypatch):
    # This verifies that a retry delay cannot fit, rather than machine speed.
    monkeypatch.setattr("agentguard.application.deadlines.monotonic", lambda: 100.0)
    error = UpstreamError()
    with guard(max_elapsed_seconds=.001, initial_delay_seconds=1, jitter=False).run() as run:
        with pytest.raises(UpstreamError) as caught: run.invoke(lambda: (_ for _ in ()).throw(error))
        assert caught.value is error
    assert run.state.llm_calls == 1
    assert run.state.events[-2]['reason_code'] == 'retry_time_budget'


def test_unawaited_retry_call_never_starts_provider():
    async def execute():
        async def provider(): pytest.fail('must not execute')
        async with guard().run() as run:
            run.invoke(provider)
        assert run.state.llm_calls == 0
        assert_released(run)
    asyncio.run(execute())


def test_total_retry_deadline_cancels_in_flight_provider():
    async def execute():
        stopped = asyncio.Event()
        async def provider():
            try: await asyncio.Event().wait()
            finally: stopped.set()
        async with guard(max_elapsed_seconds=.02).run() as run:
            with pytest.raises(ExecutionTimeout): await run.invoke(provider)
            assert stopped.is_set()
            assert run.state.llm_calls == 1
            assert_released(run)
    asyncio.run(execute())


def test_closing_automatic_awaitable_factory_closes_nested_pending_response():
    saved = []
    class Store:
        def save(self, state, version): saved.append((state.status, state))
    async def provider(): pytest.fail('never awaited')
    configured = AgentGuard({'retry': {'max_retries': 1}}, run_store=Store())
    pending = configured.guard(lambda: provider())()
    pending.close()
    assert saved[-1][0] == 'cancelled'
    assert not any(row['event'] == 'llm.completed' for row in saved[-1][1].events)


def test_async_retry_deadline_begins_when_awaited(monkeypatch):
    from agentguard.application import deadlines
    clock = [100.0]
    monkeypatch.setattr(deadlines, 'monotonic', lambda: clock[0])
    async def provider(): return {'ok': True}
    async def execute():
        async with guard(max_elapsed_seconds=1).run() as run:
            pending = run.invoke(provider)
            clock[0] += 10
            assert (await pending)['ok'] is True
            assert_released(run)
    asyncio.run(execute())
