from decimal import Decimal

from agentguard import AgentGuardConfig, Usage
from agentguard.policies.cost import ModelPrice, PricingTable


def test_pricing_table_calculates_cost_for_input_output_and_cache():
    table = PricingTable(
        version="v1",
        models={
            "demo": ModelPrice(
                input_per_million=Decimal("2"),
                output_per_million=Decimal("4"),
                cached_input_per_million=Decimal("0.5"),
            )
        },
    )

    cost = table.estimate(
        "demo",
        Usage(input_tokens=1_000_000, output_tokens=500_000, cached_tokens=200_000),
    )

    assert cost == Decimal("3.700000")


def test_unknown_model_has_no_cost_estimate():
    table = PricingTable.from_config(AgentGuardConfig().pricing)

    assert table.estimate("missing", Usage(input_tokens=10, source="estimate")) is None


def test_cached_usage_without_discount_uses_normal_input_rate():
    table = PricingTable(models={"demo": ModelPrice(input_per_million=Decimal("2"))})
    assert table.estimate("demo", Usage(input_tokens=1000000, cached_tokens=1000000)) == Decimal("2.000000")


def test_explicit_zero_reasoning_price_is_preserved():
    table = PricingTable(models={"demo": ModelPrice(output_per_million=Decimal("2"), reasoning_output_per_million=Decimal("0"))})
    assert table.estimate("demo", Usage(output_tokens=1000000, reasoning_tokens=1000000)) == Decimal("0.000000")
