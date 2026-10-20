"""Opt-in real Redis contract; CI provisions an isolated Redis service."""
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from agentguard import RedisBudgetStore, BudgetExceeded, Usage


@pytest.mark.skipif(not os.environ.get("AGENTGUARD_TEST_REDIS_URL"), reason="Real Redis service not configured")
def test_real_atomic_budget_concurrency_and_epoch_fence():
    url = os.environ["AGENTGUARD_TEST_REDIS_URL"]
    scope = "ci-"+uuid4().hex
    store = RedisBudgetStore.from_url(url,scope=scope,epoch="epoch-1")
    store.provision(token_limit=100,cost_limit_usd="1")
    try:
        def work(index):
            other = RedisBudgetStore.from_url(url,scope=scope,epoch="epoch-1")
            try: return other.reserve(10,"0.1",reservation_id=str(index))
            except BudgetExceeded: return None
            finally: other.client.close()
        with ThreadPoolExecutor(max_workers=20) as pool:
            reservations = [r for r in pool.map(work,range(100)) if r]
        assert len(reservations) == 10
        assert store.status()["held_tokens"] == "100"
        for reservation in reservations:
            # Each reservation keeps its own client; redis-py close permits reconnect.
            reservation.settle(Usage(input_tokens=3,output_tokens=2),"0.05")
        assert store.status()["spent_tokens"] == "50" and store.status()["held_tokens"] == "0"
        store.pause()
        with pytest.raises(BudgetExceeded): store.reserve(1,0)
        wrong = RedisBudgetStore(store.client,scope=scope,epoch="epoch-2")
        with pytest.raises(BudgetExceeded): wrong.reserve(1,0)
    finally:
        # Only the explicit unique test key is removed; no broad flush.
        store.client.delete(store.key)
        store.client.close()
