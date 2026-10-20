from datetime import datetime, timezone
import json

import pytest

from agentguard import AgentGuard, PolicyUnavailable
from agentguard.adapters.remote_policy import PolicyVerifier, sign_policy, HTTPPolicyProvider
from agentguard.config.remote import RemoteConfigManager


SCOPE = {"tenant_id": "t", "project_id": "p", "environment": "test"}
KEY = b"local-test-key-never-use-in-deployment" * 2


class Clock:
    wall = 2000000000.
    mono = 10.
    def advance(self, seconds): self.wall += seconds; self.mono += seconds


class Provider:
    calls = 0
    fail = False
    def __init__(self, envelope): self.envelope = envelope
    def fetch(self):
        self.calls += 1
        if self.fail: raise OSError("PRIVATE")
        return self.envelope


def envelope(clock, *, version=1, ttl=900, config=None, recovery=False):
    return sign_policy({"version": version, "issued_at": datetime.fromtimestamp(clock.wall, timezone.utc).isoformat(),
        "expires_at": datetime.fromtimestamp(clock.wall + ttl, timezone.utc).isoformat(), "scope": SCOPE,
        "config": config or {}, "recovery": recovery}, KEY)


def manager(tmp_path, clock, provider):
    return RemoteConfigManager(provider, PolicyVerifier({"primary": KEY}), scope=SCOPE, cache_path=tmp_path / "policy.json",
        wall_clock=lambda: clock.wall, monotonic_clock=lambda: clock.mono)


def test_authenticated_snapshot_scope_expiry_and_existing_run(tmp_path):
    clock = Clock()
    provider = Provider(envelope(clock, ttl=60))
    remote = manager(tmp_path, clock, provider)
    guard = AgentGuard(config_manager=remote)
    with pytest.raises(PolicyUnavailable): guard.run()
    with guard.run(**SCOPE) as run:
        assert run.invoke(lambda: "ok") == "ok"
        clock.advance(61)
        with pytest.raises(PolicyUnavailable): run.invoke(lambda: "never")
    provider.fail = True
    with pytest.raises(PolicyUnavailable): guard.run(**SCOPE)
    assert remote.status()["version"] == 1


def test_outage_valid_cache_then_expired_no_fail_open(tmp_path):
    clock = Clock()
    provider = Provider(envelope(clock))
    remote = manager(tmp_path, clock, provider)
    assert remote.snapshot().version == 1
    provider.fail = True
    clock.advance(61)
    assert remote.snapshot().version == 1
    assert remote.failures == 1
    clock.advance(900)
    with pytest.raises(PolicyUnavailable): remote.snapshot()


def test_signature_scope_rollback_and_same_version_mutation_are_rejected(tmp_path):
    clock = Clock()
    provider = Provider(envelope(clock, version=4))
    remote = manager(tmp_path, clock, provider)
    assert remote.snapshot().version == 4
    for bad in [envelope(clock, version=3), envelope(clock, version=4, config={"token":{"max_output_tokens":1}}),
                {**envelope(clock,version=5), "signature": "0"*64}]:
        provider.envelope = bad
        remote.refresh()
        assert remote.snapshot().version == 4
    restored = manager(tmp_path, clock, Provider(envelope(clock,version=3)))
    assert restored.snapshot().version == 4
    assert restored.failures == 1


def test_signed_renewal_does_not_extend_existing_run_and_recovery_is_bounded(tmp_path):
    from agentguard import SQLiteAuditStore
    audit = SQLiteAuditStore(tmp_path / "audit.db")
    clock = Clock()
    provider = Provider(envelope(clock, ttl=60))
    remote = manager(tmp_path, clock, provider)
    old = remote.snapshot()
    clock.advance(40)
    provider.envelope = envelope(clock, ttl=900)
    remote.refresh()
    clock.advance(21)
    with pytest.raises(PolicyUnavailable): old.valid()
    assert remote.snapshot().version == 1
    with pytest.raises(PolicyUnavailable): remote.install_recovery(envelope(clock,version=2,recovery=True,ttl=900), audit_store=audit, actor="admin")
    remote.install_recovery(envelope(clock,version=2,recovery=True,ttl=120), audit_store=audit, actor="admin")
    assert manager(tmp_path,clock,Provider(envelope(clock,version=1))).snapshot().version == 2
    audit.close()


def test_recovery_can_tighten_but_cannot_remove_limit_or_change_price(tmp_path):
    from agentguard import SQLiteAuditStore
    clock = Clock()
    remote = manager(tmp_path,clock,Provider(envelope(clock,config={"token":{"run_token_budget":100}})))
    remote.snapshot()
    audit = SQLiteAuditStore(tmp_path / "audit.db")
    for bad in [{"token":{"run_token_budget":101}}, {}, {"token":{"run_token_budget":50}, "security":{"enabled":False,"action":"observe"}},
                {"token":{"run_token_budget":50}, "circuit":{"failure_threshold":6}}]:
        with pytest.raises(PolicyUnavailable):
            remote.install_recovery(envelope(clock,version=2,recovery=True,ttl=120,config=bad),audit_store=audit,actor="admin")
    remote.install_recovery(envelope(clock,version=2,recovery=True,ttl=120,config={"token":{"run_token_budget":50}}),audit_store=audit,actor="admin")
    assert remote.snapshot().config.token.run_token_budget == 50
    assert audit.stats()["unresolved_operations"] == 0
    audit.close()


def test_clock_rollback_and_corrupt_cache_fail_closed(tmp_path):
    clock = Clock()
    remote = manager(tmp_path,clock,Provider(envelope(clock)))
    remote.snapshot()
    clock.wall -= 10
    with pytest.raises(PolicyUnavailable): remote.snapshot()
    assert remote.status()["clock_failed"]
    (tmp_path / "policy.json").write_text('{"PRIVATE":"corrupt"}')
    with pytest.raises(PolicyUnavailable): manager(tmp_path,Clock(),Provider(envelope(Clock())))


@pytest.mark.parametrize("url", ["http://remote.example/policy", "https://user:password@remote.example/policy", "file:///tmp/policy"])
def test_transport_requires_authenticated_https(url):
    with pytest.raises(ValueError): HTTPPolicyProvider(url,token="token")


def test_expiry_during_interceptor_and_tool_approval_stops_side_effect(tmp_path):
    clock = Clock()
    provider = Provider(envelope(clock, ttl=60, config={"tools":{"approval_required_for":["write"]}}))
    remote = manager(tmp_path,clock,provider)
    guard = AgentGuard(config_manager=remote)
    calls = []
    with guard.run(**SCOPE) as run:
        def approve(*args): clock.advance(61); return True
        with pytest.raises(PolicyUnavailable): run.invoke_tool("write",lambda: calls.append("tool"),approval=approve)
    assert calls == []


def test_expired_newer_refresh_preserves_live_snapshot(tmp_path):
    clock = Clock()
    provider = Provider(envelope(clock, version=2))
    remote = manager(tmp_path,clock,provider)
    remote.snapshot()
    provider.envelope = envelope(clock,version=3,ttl=60)
    clock.advance(61)
    remote.refresh()
    assert remote.snapshot().version == 2
