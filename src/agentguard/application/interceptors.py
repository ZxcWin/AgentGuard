from __future__ import annotations

from typing import Any, Iterable

__all__ = ["BaseInterceptor", "InterceptorChain"]


class BaseInterceptor:
    def before_request(self, request: dict[str, Any]) -> dict[str, Any] | None:
        return request

    def after_response(self, response: Any) -> Any:
        return response

    def on_chunk(self, chunk: Any, context: Any) -> Any:
        return self.after_response(chunk)

    def on_error(self, error: BaseException) -> None:
        return None

    def on_start(self, context: Any) -> None:
        return None

    def on_finish(self, context: Any) -> None:
        return None


class InterceptorChain:
    def __init__(self, interceptors: Iterable[BaseInterceptor] = ()) -> None:
        self.interceptors = list(interceptors)

    def add(self, interceptor: BaseInterceptor) -> None:
        self.interceptors.append(interceptor)

    def before_request(self, request: dict[str, Any]) -> dict[str, Any]:
        current = request
        for interceptor in self.interceptors:
            result = interceptor.before_request(current)
            if result is not None:
                current = result
        return current

    def after_response(self, response: Any) -> Any:
        current = response
        for interceptor in reversed(self.interceptors):
            current = interceptor.after_response(current)
        return current

    def on_chunk(self, chunk: Any, context: Any) -> Any:
        current = chunk
        for interceptor in self.interceptors:
            current = interceptor.on_chunk(current, context)
        return current

    def on_error(self, error: BaseException) -> None:
        for interceptor in self.interceptors:
            interceptor.on_error(error)

    def on_start(self, context: Any) -> None:
        for interceptor in self.interceptors:
            interceptor.on_start(context)

    def on_finish(self, context: Any) -> None:
        for interceptor in reversed(self.interceptors):
            interceptor.on_finish(context)
