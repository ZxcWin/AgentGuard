from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from threading import RLock

from ..config.loader import AgentGuardConfig
from ..domain.exceptions import BudgetExceeded
from ..domain.models import RunState, Usage
from .cost import PricingTable


@dataclass
class BudgetReservation:
    manager: "BudgetManager"
    reserved_tokens: int
    reserved_cost_usd: Decimal
    settled: bool = False
    purpose: str = "primary"

    def settle(self, usage: Usage, cost_usd: float | Decimal = 0.0) -> None:
        self.manager._settle(self, usage, cost_usd)

    def release(self) -> None:
        self.manager._release(self)


class BudgetManager:
    def __init__(self, config: AgentGuardConfig, run: RunState | None = None, pricing: PricingTable | None = None) -> None:
        self.config = config
        self.run = run
        self.pricing = pricing or PricingTable.from_config(config.pricing)
        self._lock = RLock()
        self._reserved_tokens = 0
        self._reserved_cost_usd = Decimal("0")
        self._reserved_retry_tokens = 0
        self._reserved_retry_cost_usd = Decimal("0")
        self._spent_retry_tokens = 0
        self._spent_retry_cost_usd = Decimal("0")
        self._warnings_emitted: set[str] = set()

    @staticmethod
    def _purpose(purpose: str) -> str:
        if purpose not in {"primary", "retry", "quality_retry", "fallback"}:
            raise ValueError("purpose 必须是 primary、retry、quality_retry 或 fallback")
        return purpose

    def _retry_limit(self, value: int | Decimal | None) -> int | Decimal | None:
        if value is None:
            return None
        return value * Decimal(str(self.config.token.retry_budget_ratio))

    def _warn_if_needed(
        self,
        metric: str,
        projected: int | Decimal,
        limit: int | Decimal | None,
    ) -> None:
        if self.run is None or limit is None or limit <= 0:
            return
        threshold = self.config.budget.cost_warning_threshold
        if projected < limit * Decimal(str(threshold)):
            return
        if metric in self._warnings_emitted:
            return
        self._warnings_emitted.add(metric)
        self.run.record(
            "budget.warning",
            metric=metric,
            projected=float(projected),
            limit=float(limit),
            threshold=threshold,
        )

    def reserve(
        self,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float | Decimal = 0.0,
        *,
        purpose: str = "primary",
    ) -> BudgetReservation:
        purpose = self._purpose(purpose)
        requested_cost = Decimal(str(cost_usd))
        if min(input_tokens, output_tokens) < 0 or requested_cost < 0 or not requested_cost.is_finite():
            raise ValueError("预算值必须是非负数")
        requested_tokens = input_tokens + output_tokens
        buffered_tokens = int(requested_tokens * (1 + self.config.token.reservation_buffer) + 0.999999)
        buffered_cost = requested_cost * (Decimal(1) + Decimal(str(self.config.token.reservation_buffer)))
        token_limit = self.config.token.run_token_budget
        cost_limit = self.config.budget.run_cost_usd
        with self._lock:
            current_tokens = self.run.total_tokens if self.run else 0
            current_cost = Decimal(str(self.run.estimated_cost_usd)) if self.run else Decimal("0")
            self._warn_if_needed(
                "tokens",
                current_tokens + self._reserved_tokens + buffered_tokens,
                token_limit,
            )
            self._warn_if_needed(
                "cost_usd",
                current_cost + self._reserved_cost_usd + buffered_cost,
                Decimal(str(cost_limit)) if cost_limit is not None else None,
            )
            if self.config.token.max_input_tokens is not None and input_tokens > self.config.token.max_input_tokens:
                raise BudgetExceeded("输入 Token 超过限制", reason_code="input_token_limit")
            if self.config.token.max_output_tokens is not None and output_tokens > self.config.token.max_output_tokens:
                raise BudgetExceeded("输出 Token 超过限制", reason_code="output_token_limit")
            if token_limit is not None and current_tokens + self._reserved_tokens + buffered_tokens > token_limit:
                raise BudgetExceeded("Run Token 预算已超限", reason_code="run_token_budget")
            if cost_limit is not None and current_cost + self._reserved_cost_usd + buffered_cost > Decimal(str(cost_limit)):
                raise BudgetExceeded("Run 成本预算已超限", reason_code="run_cost_budget")
            if self.config.budget.max_cost_usd is not None and requested_cost > Decimal(str(self.config.budget.max_cost_usd)):
                raise BudgetExceeded("请求成本预算已超限", reason_code="request_cost_budget")
            if purpose != "primary":
                retry_token_limit = self._retry_limit(token_limit)
                retry_cost_limit = self._retry_limit(
                    Decimal(str(cost_limit)) if cost_limit is not None else None
                )
                if retry_token_limit is not None and self._spent_retry_tokens + self._reserved_retry_tokens + buffered_tokens > retry_token_limit:
                    raise BudgetExceeded("重试 Token 预算已超限", reason_code="retry_token_budget")
                if retry_cost_limit is not None and self._spent_retry_cost_usd + self._reserved_retry_cost_usd + buffered_cost > retry_cost_limit:
                    raise BudgetExceeded("重试成本预算已超限", reason_code="retry_cost_budget")
            self._reserved_tokens += buffered_tokens
            self._reserved_cost_usd += buffered_cost
            if purpose != "primary":
                self._reserved_retry_tokens += buffered_tokens
                self._reserved_retry_cost_usd += buffered_cost
        return BudgetReservation(self, buffered_tokens, buffered_cost, purpose=purpose)

    def _settle(self, reservation: BudgetReservation, usage: Usage, cost_usd: float | Decimal) -> None:
        with self._lock:
            if reservation.settled:
                return
            self._reserved_tokens -= reservation.reserved_tokens
            self._reserved_cost_usd -= reservation.reserved_cost_usd
            if reservation.purpose != "primary":
                self._reserved_retry_tokens -= reservation.reserved_tokens
                self._reserved_retry_cost_usd -= reservation.reserved_cost_usd
                self._spent_retry_tokens += usage.total_tokens
                self._spent_retry_cost_usd += Decimal(str(cost_usd))
            reservation.settled = True
            if self.run:
                self.run.add_usage(usage, float(Decimal(str(cost_usd))))
                token_limit = self.config.token.run_token_budget
                cost_limit = self.config.budget.run_cost_usd
                if token_limit is not None and self.run.total_tokens > token_limit:
                    self.run.record(
                        "budget.exceeded",
                        metric="tokens",
                        actual=self.run.total_tokens,
                        limit=token_limit,
                        reason_code="settled_token_budget",
                    )
                if cost_limit is not None and self.run.estimated_cost_usd > cost_limit:
                    self.run.record(
                        "budget.exceeded",
                        metric="cost_usd",
                        actual=self.run.estimated_cost_usd,
                        limit=cost_limit,
                        reason_code="settled_cost_budget",
                    )

    def _release(self, reservation: BudgetReservation) -> None:
        with self._lock:
            if reservation.settled:
                return
            self._reserved_tokens -= reservation.reserved_tokens
            self._reserved_cost_usd -= reservation.reserved_cost_usd
            if reservation.purpose != "primary":
                self._reserved_retry_tokens -= reservation.reserved_tokens
                self._reserved_retry_cost_usd -= reservation.reserved_cost_usd
            reservation.settled = True


def estimate_tokens(value: object) -> int:
    """在不依赖供应商 tokenizer 的情况下保守估算 Token 数。"""
    if value is None:
        return 0
    if isinstance(value, str):
        return max(1, (len(value) + 3) // 4)
    if isinstance(value, dict):
        return sum(estimate_tokens(key) + estimate_tokens(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return sum(estimate_tokens(item) for item in value)
    return estimate_tokens(str(value))
