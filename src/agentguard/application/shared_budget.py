"""Compose local hard limits with injected shared atomic budget reservations."""
from decimal import Decimal


class SharedBudgetManager:
    def __init__(self,local,store):
        self.local,self.store = local,store
    def __getattr__(self,name): return getattr(self.local,name)
    def reserve(self,input_tokens,output_tokens,cost_usd=0,*,purpose="primary"):
        local = self.local.reserve(input_tokens,output_tokens,cost_usd,purpose=purpose)
        try:
            remote = self.store.reserve(local.reserved_tokens,local.reserved_cost_usd)
        except BaseException as error:
            local.release()
            if self.local.run is not None and getattr(error,"reservation_id",None):
                self.local.run.record("budget.shared_uncertain", reservation_id=error.reservation_id,
                                      reason_code=getattr(error,"reason_code","shared_budget_unavailable"))
            raise
        if self.local.run is not None:
            self.local.run.record("budget.shared_reserved",reservation_id=getattr(remote,"identifier",None))
        return SharedReservation(local,remote)


class SharedReservation:
    def __init__(self,local,remote):
        self.local,self.remote = local,remote
        self.started = False
        self.settled = False
    def mark_started(self): self.started = True
    def settle(self,usage,cost_usd=0):
        if self.settled: return
        # Preserve real local usage even if shared settlement has an unknown commit.
        self.local.settle(usage,cost_usd)
        try:
            self.remote.settle(usage,cost_usd)
        finally:
            # A failed/unknown settlement must not be followed by a refund.
            self.settled = True
    def release(self):
        if self.settled: return
        self.settled = True
        self.local.release()
        if self.started:
            # A started request can have incurred charges. Keep conservative hold
            # until explicit operator settlement; never TTL-refund it.
            return
        self.remote.release()
