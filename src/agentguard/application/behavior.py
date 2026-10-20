"""Bounded progress and output checks. Store hashes, never output bodies."""
from contextvars import ContextVar
from hashlib import sha256
from threading import RLock

from ..policies.content import text_content
from ..policies.tools import LoopDetected


tool_stack: ContextVar[tuple[str, ...]] = ContextVar("agentguard_tool_stack", default=())


class BehaviorMonitor:
    def __init__(self, config):
        self.config = config
        self._lock = RLock()
        self._calls = 0
        self._last_output = None
        self._repeated = 0
        self._progress = None
        self._seen_progress = set()
        self._active_tools = 0

    def check(self):
        with self._lock:
            maximum = self.config.max_calls_without_progress
            if maximum is not None and self._calls >= maximum:
                raise LoopDetected("No-progress call limit reached; report a new verified progress marker")
            repeated = self.config.max_repeated_outputs
            if repeated is not None and self._repeated >= repeated:
                raise LoopDetected("Repeated output limit reached")

    def started(self):
        with self._lock:
            self.check()
            self._calls += 1

    def output(self, value):
        if self.config.max_repeated_outputs is None:
            return
        try:
            content = text_content(value, 200000)
        except Exception:
            return
        digest = sha256(content.encode()).digest()
        with self._lock:
            self._repeated = self._repeated + 1 if digest == self._last_output else 1
            self._last_output = digest

    def progress(self, marker: str):
        if not isinstance(marker, str) or not 1 <= len(marker) <= 256:
            raise ValueError("Progress marker must contain 1 to 256 characters")
        digest = sha256(marker.encode()).digest()
        with self._lock:
            if digest not in self._seen_progress:
                if len(self._seen_progress) >= 10000:
                    raise LoopDetected("Progress marker capacity exceeded")
                self._seen_progress.add(digest)
                self._progress = digest
                self._calls = self._repeated = 0
                self._last_output = None

    def acquire_tool(self, tools):
        with self._lock:
            if tools.max_depth is not None and len(tool_stack.get()) >= tools.max_depth:
                raise LoopDetected("Tool recursion depth exceeded")
            if tools.max_concurrent is not None and self._active_tools >= tools.max_concurrent:
                raise LoopDetected("Concurrent tool limit exceeded")
            self.started()
            self._active_tools += 1

    def release_tool(self):
        with self._lock:
            self._active_tools -= 1
