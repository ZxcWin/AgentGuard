import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from agentguard import SQLiteCostLedger


def test_immutable_invoice_idempotency_adjustments_and_reconciliation(tmp_path):
    path = tmp_path / "ledger.db"
    ledger = SQLiteCostLedger(path)
    ledger.estimate(request_id="request-1", amount_usd="0.3")
    ledger.estimate(request_id="unbilled", amount_usd="0.1")
    lines = [{"line_id":"a","request_id":"request-1","amount_usd":"0.4"},
             {"line_id":"b","amount_usd":"0.2"}]
    assert ledger.import_invoice("supplier","invoice-1",lines,actor="admin")["imported"] == 2
    assert ledger.import_invoice("supplier","invoice-1",lines,actor="admin")["duplicates"] == 2
    with pytest.raises(ValueError):
        ledger.import_invoice("supplier","invoice-1",[{**lines[0],"amount_usd":"0.5"}],actor="admin")
    reference = json.dumps(["supplier","invoice-1","a"],separators=(",",":"))
    change = dict(adjustment_id="credit-1",reference_id=reference,amount_usd="-0.1",actor="admin")
    assert ledger.adjust(**change) == {"created":True}
    assert ledger.adjust(**change) == {"created":False}
    with pytest.raises(ValueError): ledger.adjust(**{**change,"amount_usd":"-0.2"})
    summary = ledger.summary()
    assert summary["authoritative_usd"] == "0.5"
    assert summary["matched_difference_usd"] == "0.0"
    assert summary["unbilled_estimates"] == summary["unmatched_invoice_requests"] == 1
    ledger.close()
    reopened = SQLiteCostLedger(path)
    assert reopened.list()["total"] == 5
    reopened.close()


def test_invoice_batch_is_atomic_and_estimates_idempotent_across_connections(tmp_path):
    path = tmp_path / "ledger.db"
    ledgers = [SQLiteCostLedger(path) for _ in range(4)]
    row = {"line_id":"existing","amount_usd":"1"}
    ledgers[0].import_invoice("s","i",[row],actor="a")
    with pytest.raises(ValueError):
        ledgers[0].import_invoice("s","i",[{"line_id":"new","amount_usd":"2"},{**row,"amount_usd":"3"}],actor="a")
    assert ledgers[0].list()["total"] == 1
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda db: db.estimate(request_id="same",amount_usd="0.1"),ledgers))
    assert ledgers[0].list()["total"] == 2
    for db in ledgers: db.close()


@pytest.mark.parametrize("amount",["NaN","Infinity","-1","0.0000000001","1000000001","1e1000000","0e1000000000",1])
def test_invoice_rejects_invalid_amounts(tmp_path, amount):
    ledger = SQLiteCostLedger(tmp_path / "ledger.db")
    with pytest.raises(ValueError): ledger.import_invoice("s","i",[{"line_id":"a","amount_usd":amount}],actor="a")
    assert ledger.list()["total"] == 0
    ledger.close()
