import asyncio

from agentguard import AgentGuard


class SyncResource:
    def __init__(self):
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.closed = True
        return False

    def __iter__(self):
        return iter((1, 2))


class SyncResourceReturningValue(SyncResource):
    def __enter__(self):
        return "同步上下文值"


class AsyncResource:
    def __init__(self):
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.closed = True
        return False

    def __aiter__(self):
        async def values():
            yield 1
            yield 2

        return values()


class AsyncResourceReturningValue(AsyncResource):
    async def __aenter__(self):
        return "异步上下文值"


def test_wrap_is_idempotent_for_same_guard_and_preserves_sync_protocols():
    guard = AgentGuard()
    resource = SyncResource()
    wrapped = guard.wrap(resource)

    assert guard.wrap(wrapped) is wrapped
    with wrapped as entered:
        assert entered is wrapped
        assert list(wrapped) == [1, 2]
    assert resource.closed is True


def test_async_context_and_iteration_protocols_are_forwarded():
    guard = AgentGuard()
    resource = AsyncResource()
    wrapped = guard.wrap(resource)

    async def consume():
        async with wrapped as entered:
            assert entered is wrapped
            return [value async for value in wrapped]

    assert asyncio.run(consume()) == [1, 2]
    assert resource.closed is True


def test_context_manager_preserves_other_return_values():
    guard = AgentGuard()
    sync_resource = SyncResourceReturningValue()
    async_resource = AsyncResourceReturningValue()

    with guard.wrap(sync_resource) as entered:
        assert entered == "同步上下文值"

    async def enter_async():
        async with guard.wrap(async_resource) as entered:
            return entered

    assert asyncio.run(enter_async()) == "异步上下文值"
