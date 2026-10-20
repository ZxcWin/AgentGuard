"""Immutable invoice entries and estimates; invoice imports are the authority."""
from __future__ import annotations

from decimal import Decimal, DecimalException
import json
from pathlib import Path
import re
import sqlite3
from threading import RLock
from datetime import datetime, timezone


def identifier(value, name):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", value):
        raise ValueError(f"Invalid {name}")
    return value


def money(value, *, negative=False):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("Amounts must be decimal strings")
    try:
        amount = Decimal(value)
        if (not amount.is_finite() or not -9 <= amount.as_tuple().exponent <= 9
                or amount.copy_abs() > Decimal("1000000000") or (amount < 0 and not negative)):
            raise ValueError()
    except (DecimalException, ValueError, OverflowError):
        raise ValueError("Invalid USD amount or precision (at most 9 decimals)") from None
    return format(amount, "f")


class SQLiteCostLedger:
    def __init__(self, path: str | Path):
        self._lock = RLock()
        self._db = sqlite3.connect(str(path), timeout=2, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("""CREATE TABLE IF NOT EXISTS cost_entries (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, entry_id TEXT UNIQUE NOT NULL,
            kind TEXT NOT NULL, provider TEXT NOT NULL, invoice_id TEXT NOT NULL,
            request_id TEXT, amount TEXT NOT NULL, recorded_at TEXT NOT NULL, actor TEXT NOT NULL,
            reference_id TEXT, payload TEXT NOT NULL)""")
        self._db.execute("CREATE INDEX IF NOT EXISTS cost_request ON cost_entries(request_id,kind)")
        self._db.commit()

    def import_invoice(self, provider: str, invoice_id: str, lines: list[dict], *, actor: str) -> dict:
        provider, invoice_id = identifier(provider,"provider"), identifier(invoice_id,"invoice_id")
        if not isinstance(lines, list) or not 1 <= len(lines) <= 1000:
            raise ValueError("Invoice requires 1 to 1000 lines")
        prepared = []
        seen = set()
        for row in lines:
            if not isinstance(row, dict) or set(row) - {"line_id", "request_id", "amount_usd"} or not {"line_id", "amount_usd"} <= set(row):
                raise ValueError("Invalid invoice fields")
            line = identifier(row["line_id"],"line_id")
            request = identifier(row["request_id"],"request_id") if row.get("request_id") else None
            if line in seen: raise ValueError("Duplicate invoice line")
            seen.add(line)
            prepared.append({"entry_id": json.dumps([provider, invoice_id, line], separators=(",", ":")),
                             "line_id": line, "amount_usd": money(row["amount_usd"]), "request_id": request})
        inserted = 0
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            for row in prepared:
                payload = json.dumps(row, sort_keys=True)
                existing = self._db.execute("SELECT payload FROM cost_entries WHERE entry_id=?", (row["entry_id"],)).fetchone()
                if existing:
                    if existing[0] != payload: raise ValueError("Invoice line conflict; use an explicit adjustment")
                    continue
                self._db.execute("INSERT INTO cost_entries(entry_id,kind,provider,invoice_id,request_id,amount,recorded_at,actor,payload) VALUES(?,?,?,?,?,?,?,?,?)",
                    (row["entry_id"],"invoice",provider,invoice_id,row["request_id"],row["amount_usd"],datetime.now(timezone.utc).isoformat(),actor,payload))
                inserted += 1
        return {"imported": inserted, "duplicates": len(prepared)-inserted, "currency": "USD"}

    def estimate(self, *, request_id: str, amount_usd: str):
        request_id, amount = identifier(request_id,"request_id"), money(amount_usd)
        payload = json.dumps({"request_id":request_id,"amount_usd":amount},sort_keys=True)
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute("SELECT payload FROM cost_entries WHERE entry_id=?", (f"estimate:{request_id}",)).fetchone()
            if row:
                if row[0] != payload: raise ValueError("Estimate already recorded with different value")
                return
            self._db.execute("INSERT INTO cost_entries(entry_id,kind,provider,invoice_id,request_id,amount,recorded_at,actor,payload) VALUES(?,?,?,?,?,?,?,?,?)",
                (f"estimate:{request_id}","estimate","sdk","",request_id,amount,datetime.now(timezone.utc).isoformat(),"sdk",payload))

    def adjust(self, *, adjustment_id: str, reference_id: str, amount_usd: str, actor: str):
        adjustment_id = identifier(adjustment_id,"adjustment_id")
        amount = money(amount_usd,negative=True)
        payload = json.dumps({"reference_id":reference_id,"amount_usd":amount},sort_keys=True)
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            original = self._db.execute("SELECT provider,invoice_id,request_id FROM cost_entries WHERE entry_id=? AND kind='invoice'", (reference_id,)).fetchone()
            if original is None: raise ValueError("Adjustment requires an existing invoice line")
            entry = f"adjustment:{adjustment_id}"
            row = self._db.execute("SELECT payload FROM cost_entries WHERE entry_id=?", (entry,)).fetchone()
            if row:
                if row[0] != payload: raise ValueError("Adjustment ID conflict")
                return {"created":False}
            self._db.execute("INSERT INTO cost_entries(entry_id,kind,provider,invoice_id,request_id,amount,recorded_at,actor,reference_id,payload) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (entry,"adjustment",*original,amount,datetime.now(timezone.utc).isoformat(),actor,reference_id,payload))
        return {"created":True}

    def summary(self):
        with self._lock:
            rows = self._db.execute("SELECT kind,request_id,amount FROM cost_entries").fetchall()
        estimates, actual = {}, {}
        invoice = adjustment = Decimal(0)
        unmatched = 0
        for kind, request, raw in rows:
            amount = Decimal(raw)
            if kind == "estimate": estimates[request] = amount
            else:
                if kind == "invoice": invoice += amount
                else: adjustment += amount
                if request: actual[request] = actual.get(request,Decimal(0))+amount
                else: unmatched += 1
        matched = set(estimates) & set(actual)
        delta = sum((actual[key]-estimates[key] for key in matched),Decimal(0))
        return {"currency":"USD","estimated_usd":str(sum(estimates.values(),Decimal(0))),
                "invoice_usd":str(invoice),"adjustments_usd":str(adjustment),"authoritative_usd":str(invoice+adjustment),
                "matched_requests":len(matched),"matched_difference_usd":str(delta),
                "unbilled_estimates":len(set(estimates)-set(actual)),"unmatched_invoice_requests":len(set(actual)-set(estimates))+unmatched,
                "entries":len(rows),"source":"imported_supplier_invoices"}

    def list(self, *, page=1, page_size=50):
        if type(page) is not int or not 1 <= page <= 100000 or type(page_size) is not int or not 1 <= page_size <= 100:
            raise ValueError("Invalid ledger pagination")
        with self._lock:
            total = self._db.execute("SELECT COUNT(*) FROM cost_entries").fetchone()[0]
            rows = self._db.execute("SELECT entry_id,kind,provider,invoice_id,request_id,amount,recorded_at,actor,reference_id FROM cost_entries ORDER BY seq DESC LIMIT ? OFFSET ?",
                (page_size,(page-1)*page_size)).fetchall()
        fields = ("entry_id","kind","provider","invoice_id","request_id","amount_usd","recorded_at","actor","reference_id")
        return {"items":[dict(zip(fields,row)) for row in rows],"total":total,"page":page,"page_size":page_size}

    def close(self):
        with self._lock: self._db.close()
