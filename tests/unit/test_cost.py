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
