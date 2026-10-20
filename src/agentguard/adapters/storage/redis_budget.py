"""Single-writer Redis budget epoch with atomic, idempotent reservations.

No reservation TTL or automatic refunds. Persistence/failover fencing is a deployment
requirement: an epoch must be paused after an unverified primary change.
"""
from __future__ import annotations

from decimal import Decimal, DecimalException, ROUND_CEILING
from hashlib import sha256
from uuid import uuid4

from ...domain.exceptions import BudgetExceeded


_PROVISION = """
if redis.call('EXISTS',KEYS[1]) == 1 then return 0 end
redis.call('HSET',KEYS[1],'epoch',ARGV[1],'active','1','limit_tokens',ARGV[2],
 'limit_micros',ARGV[3],'spent_tokens','0','spent_micros','0','held_tokens','0','held_micros','0','records','0')
return 1
"""
_RESERVE = """
if redis.call('HGET',KEYS[1],'epoch') ~= ARGV[1] or redis.call('HGET',KEYS[1],'active') ~= '1' then return -1 end
local field = 'r:' .. ARGV[2]
local old = redis.call('HGET',KEYS[1],field)
local expected = 'held:' .. ARGV[3] .. ':' .. ARGV[4]
if old then if old == expected then return 1 else return -2 end end
local st = tonumber(redis.call('HGET',KEYS[1],'spent_tokens'))
local sm = tonumber(redis.call('HGET',KEYS[1],'spent_micros'))
local ht = tonumber(redis.call('HGET',KEYS[1],'held_tokens'))
local hm = tonumber(redis.call('HGET',KEYS[1],'held_micros'))
local lt = tonumber(redis.call('HGET',KEYS[1],'limit_tokens'))
local lm = tonumber(redis.call('HGET',KEYS[1],'limit_micros'))
if st+ht+tonumber(ARGV[3]) > lt or sm+hm+tonumber(ARGV[4]) > lm then return 0 end
if tonumber(redis.call('HGET',KEYS[1],'records')) >= tonumber(ARGV[5]) then return -3 end
redis.call('HINCRBY',KEYS[1],'held_tokens',ARGV[3])
redis.call('HINCRBY',KEYS[1],'held_micros',ARGV[4])
redis.call('HINCRBY',KEYS[1],'records',1)
redis.call('HSET',KEYS[1],field,expected)
return 1
"""
_FINISH = """
if redis.call('HGET',KEYS[1],'epoch') ~= ARGV[1] then return -1 end
local field = 'r:' .. ARGV[2]
local old = redis.call('HGET',KEYS[1],field)
local done = ARGV[5] .. ':' .. ARGV[6] .. ':' .. ARGV[7]
if old == done then return 1 end
if old ~= 'held:' .. ARGV[3] .. ':' .. ARGV[4] then return -2 end
if tonumber(redis.call('HGET',KEYS[1],'spent_tokens'))+tonumber(ARGV[6]) > 9000000000000000 or
 tonumber(redis.call('HGET',KEYS[1],'spent_micros'))+tonumber(ARGV[7]) > 9000000000000000 then return -4 end
redis.call('HINCRBY',KEYS[1],'held_tokens',-tonumber(ARGV[3]))
redis.call('HINCRBY',KEYS[1],'held_micros',-tonumber(ARGV[4]))
redis.call('HINCRBY',KEYS[1],'spent_tokens',ARGV[6])
redis.call('HINCRBY',KEYS[1],'spent_micros',ARGV[7])
redis.call('HSET',KEYS[1],field,done)
return 1
"""
_PAUSE = """
if redis.call('HGET',KEYS[1],'epoch') ~= ARGV[1] then return 0 end
redis.call('HSET',KEYS[1],'active','0')
return 1
"""
_RECONCILE = """
if redis.call('HGET',KEYS[1],'active') ~= '0' then return -3 end
""" + _FINISH
_RESUME = """
if redis.call('HGET',KEYS[1],'epoch') ~= ARGV[1] then return 0 end
redis.call('HSET',KEYS[1],'active','1')
return 1
"""


def bounded(value):
    if type(value) is not int or not 0 <= value <= 10**12:
        raise ValueError("Shared budget integers must be between 0 and 10^12")
    return value


def micros(value):
    try:
        if type(value) not in (int, float, str, Decimal) or len(str(value)) > 64:
            raise ValueError()
        amount = Decimal(str(value))
        if not amount.is_finite() or not 0 <= amount <= 1000000: raise ValueError()
        if amount == 0: return 0
        if amount <= Decimal("0.000001"): return 1
        return bounded(int((amount * 1000000).to_integral_value(rounding=ROUND_CEILING)))
    except (DecimalException, OverflowError, ValueError):
        raise ValueError("Invalid shared USD cost") from None


class RedisBudgetStore:
    def __init__(self, client, *, scope: str, epoch: str, prefix="agentguard", max_records=100000):
        if any(not isinstance(item,str) or not 1 <= len(item) <= 256 for item in (scope,epoch,prefix)):
            raise ValueError("Invalid shared budget scope or epoch")
        if type(max_records) is not int or not 1 <= max_records <= 1000000:
            raise ValueError("Invalid reservation record bound")
        self.client,self.epoch,self.max_records = client,epoch,max_records
        self.key = f"{prefix}:budget:{{{sha256(scope.encode()).hexdigest()}}}"

    @classmethod
    def from_url(cls,url,**kwargs):
        import importlib
        client = importlib.import_module("redis").Redis.from_url(url,socket_timeout=2,socket_connect_timeout=2)
        return cls(client,**kwargs)

    def _eval(self,script,*args):
        try:
            return int(self.client.eval(script,1,self.key,*args))
        except Exception:
            raise BudgetExceeded("Shared budget storage unavailable; admission refused",reason_code="shared_budget_unavailable") from None

    def provision(self,*,token_limit:int,cost_limit_usd):
        if self._eval(_PROVISION,self.epoch,bounded(token_limit),micros(cost_limit_usd)) != 1:
            raise ValueError("Budget scope already exists; do not reset a live epoch")

    def reserve(self, tokens, cost_usd, *, reservation_id=None):
        tokens,cost = bounded(tokens),micros(cost_usd)
        reservation_id = reservation_id or uuid4().hex
        if not isinstance(reservation_id,str) or not 1 <= len(reservation_id) <= 128:
            raise ValueError("Invalid reservation ID")
        try:
            result = self._eval(_RESERVE,self.epoch,reservation_id,tokens,cost,self.max_records)
        except BudgetExceeded as error:
            # An unknown Redis commit must be discoverable without guessing a new ID.
            error.reservation_id = reservation_id
            raise
        if result != 1:
            reason = {0:"shared_budget_exceeded",-1:"shared_budget_fenced",-2:"shared_reservation_conflict",-3:"shared_budget_capacity"}.get(result,"shared_budget_unavailable")
            raise BudgetExceeded("Shared budget reservation refused",reason_code=reason)
        return RedisBudgetReservation(self,reservation_id,tokens,cost)

    def pause(self):
        if self._eval(_PAUSE,self.epoch) != 1: raise ValueError("Budget epoch mismatch")

    def status(self, *, reservation_id=None):
        fields = ["epoch", "active", "limit_tokens", "limit_micros", "spent_tokens", "spent_micros",
                  "held_tokens", "held_micros", "records"]
        if reservation_id is not None:
            if not isinstance(reservation_id, str) or not 1 <= len(reservation_id) <= 128:
                raise ValueError("Invalid reservation ID")
            fields.append("r:" + reservation_id)
        try:
            values = self.client.hmget(self.key, fields)
            decoded = [v.decode() if isinstance(v, bytes) else v for v in values]
        except Exception:
            raise BudgetExceeded("Shared budget storage unavailable", reason_code="shared_budget_unavailable") from None
        result = dict(zip(fields, decoded))
        if result["epoch"] != self.epoch:
            raise BudgetExceeded("Budget epoch mismatch", reason_code="shared_budget_fenced")
        return result

    def reconcile(self, *, reservation_id, expected_tokens, expected_cost_usd, actual_tokens,
                  actual_cost_usd, audit_store, actor):
        """Explicitly settle a verified orphan while paused; never execute its request."""
        from ...application.audit import AuditCoordinator
        if not isinstance(reservation_id, str) or not 1 <= len(reservation_id) <= 128:
            raise ValueError("Invalid reservation ID")
        args = (self.epoch, reservation_id, bounded(expected_tokens), micros(expected_cost_usd),
                "settled", bounded(actual_tokens), micros(actual_cost_usd))
        audit = AuditCoordinator(audit_store)
        operation = audit.begin(action="budget.reconcile", actor_id=actor, reservation_id=reservation_id)
        try:
            result = self._eval(_RECONCILE, *args)
        except Exception:
            audit.end(operation, outcome="uncertain", execution_may_have_completed=True)
            raise
        audit.end(operation, outcome="completed" if result == 1 else "rejected")
        if result != 1:
            raise BudgetExceeded("Pause scope and verify exact orphan reservation before reconciliation",
                                 reason_code="shared_settlement_conflict")

    def resume(self, *, audit_store, actor):
        """Operator has verified persistence/failover fencing and repaired the primary."""
        from ...application.audit import AuditCoordinator
        audit = AuditCoordinator(audit_store)
        operation = audit.begin(action="budget.resume", actor_id=actor)
        try:
            result = self._eval(_RESUME, self.epoch)
        except Exception:
            audit.end(operation, outcome="uncertain", execution_may_have_completed=True)
            raise
        audit.end(operation, outcome="completed" if result == 1 else "rejected")
        if result != 1:
            raise ValueError("Budget epoch mismatch")


class RedisBudgetReservation:
    def __init__(self,store,identifier,tokens,cost):
        self.store,self.identifier,self.tokens,self.cost = store,identifier,tokens,cost
        self.done = False

    def settle(self,usage,cost_usd=0):
        if self.done: return
        result = self.store._eval(_FINISH,self.store.epoch,self.identifier,self.tokens,self.cost,
                                  "settled",bounded(usage.total_tokens),micros(cost_usd))
        if result != 1: raise BudgetExceeded("Shared settlement conflict; reconcile reservation",reason_code="shared_settlement_conflict")
        self.done = True

    def release(self):
        if self.done: return
        result = self.store._eval(_FINISH,self.store.epoch,self.identifier,self.tokens,self.cost,"released",0,0)
        if result != 1: raise BudgetExceeded("Shared release conflict; reconcile reservation",reason_code="shared_settlement_conflict")
        self.done = True
