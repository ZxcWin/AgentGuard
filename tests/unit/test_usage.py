import pytest

from agentguard import Usage


def test_usage_rejects_negative_or_invalid_values():
    with pytest.raises(ValueError):
        Usage(input_tokens=-1)
    with pytest.raises(ValueError):
        Usage(source="invented")
