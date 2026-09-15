from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Mapping

from ..config.schema import PricingConfig
from ..domain.exceptions import ConfigurationError
from ..domain.models import Usage

_MICRO_USD = Decimal("0.000001")


def _decimal(value: object, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ConfigurationError(f"{name} 必须是有效的十进制数") from error
    if not result.is_finite() or result < 0:
        raise ConfigurationError(f"{name} 必须是有限的非负十进制数")
    return result


@dataclass(frozen=True)
class ModelPrice:
    input_per_million: Decimal = Decimal("0")
    output_per_million: Decimal = Decimal("0")
    cached_input_per_million: Decimal | None = None
    reasoning_output_per_million: Decimal | None = None

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> "ModelPrice":
        return cls(
            input_per_million=_decimal(values.get("input_per_million", 0), "input_per_million"),
            output_per_million=_decimal(values.get("output_per_million", 0), "output_per_million"),
            cached_input_per_million=(
                None
                if values.get("cached_input_per_million") is None
                else _decimal(values["cached_input_per_million"], "cached_input_per_million")
            ),
            reasoning_output_per_million=(
                None
                if values.get("reasoning_output_per_million") is None
                else _decimal(values["reasoning_output_per_million"], "reasoning_output_per_million")
            ),
        )


@dataclass(frozen=True)
class PricingTable:
    version: str = "1"
    models: Mapping[str, ModelPrice] | None = None

    def price_for(self, model: str | None) -> ModelPrice | None:
        if not model or not self.models:
            return None
        return self.models.get(model) or self.models.get("*")

    def estimate(self, model: str | None, usage: Usage) -> Decimal | None:
        price = self.price_for(model)
        if price is None:
            return None
        input_tokens = max(0, usage.input_tokens - usage.cached_tokens)
        cached_tokens = min(usage.input_tokens, usage.cached_tokens)
        output_rate = price.reasoning_output_per_million or price.output_per_million
        cached_rate = price.cached_input_per_million
        total = Decimal(input_tokens) * price.input_per_million / Decimal(1_000_000)
        total += Decimal(usage.output_tokens) * price.output_per_million / Decimal(1_000_000)
        if usage.reasoning_tokens and price.reasoning_output_per_million is not None:
            total += Decimal(usage.reasoning_tokens) * (output_rate - price.output_per_million) / Decimal(1_000_000)
        if cached_tokens and cached_rate is not None:
            total += Decimal(cached_tokens) * cached_rate / Decimal(1_000_000)
        return total.quantize(_MICRO_USD, rounding=ROUND_HALF_UP)

    @classmethod
    def from_config(cls, config: PricingConfig) -> "PricingTable":
        return cls(
            version=config.version,
            models={
                model: ModelPrice(
                    input_per_million=Decimal(str(price.input_per_million)),
                    output_per_million=Decimal(str(price.output_per_million)),
                    cached_input_per_million=(
                        None
                        if price.cached_input_per_million is None
                        else Decimal(str(price.cached_input_per_million))
                    ),
                    reasoning_output_per_million=(
                        None
                        if price.reasoning_output_per_million is None
                        else Decimal(str(price.reasoning_output_per_million))
                    ),
                )
                for model, price in config.models.items()
            },
        )
