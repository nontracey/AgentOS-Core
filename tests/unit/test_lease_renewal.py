"""Lease renewal survives state changes, excludes other owners and rejects stale effects."""

import asyncio

import pytest

from agentos.kernel.errors import AgentOSError
from agentos.kernel.states import RunState
from tests.conftest import owner_token


def test_fence_is_independent_of_business_state_and_expiry(make_system):
    system = make_system(model_script=[])
    system.boot()
    receipt = asyncio.run(
        system.ingress.accept({"payload": {"goal": "lease"}}, channel="cli", credentials={"token": owner_token(system)})
    )
    store, rid = system.store, receipt.run_id
    first = store.claim_run_lease("default", rid, "one", 10)
    store.transition_run("default", rid, {RunState.CREATED}, RunState.RUNNING)
    assert store.renew_run_lease("default", rid, "one", first.fence, 10)
    assert store.claim_run_lease("default", rid, "two", 10) is None
    store.release_run_lease("default", rid, "one")
    second = store.claim_run_lease("default", rid, "two", 10)
    assert second.fence > first.fence
    assert not store.renew_run_lease("default", rid, "one", first.fence, 10)
    store.prepare_once("default", rid, "effect", "fs.write_text", "hash", first.fence)
    with pytest.raises(AgentOSError, match="superseded"):
        store.commit_dispatch("default", rid, "effect", first.fence, None)
    assert store.get_step("default", rid, "effect").state.value == "prepared"


def test_long_generation_renews_before_competing_owner_can_claim(make_system, monkeypatch):
    system = make_system(model_script=[])
    system.boot()
    system.loop._limits.lease_ttl_s = 1
    receipt = asyncio.run(
        system.ingress.accept({"payload": {"goal": "long"}}, channel="cli", credentials={"token": owner_token(system)})
    )
    renewed = []
    original = system.store.renew_run_lease

    def renew(*args):
        renewed.append(args)
        return original(*args)

    monkeypatch.setattr(system.store, "renew_run_lease", renew)

    async def driving(rid, owner, lease, tenant_id):
        system.store.transition_run("default", rid, {RunState.CREATED}, RunState.RUNNING)
        await asyncio.sleep(1.15)
        assert system.store.claim_run_lease("default", rid, "competitor", 10) is None
        return system.store.get_run("default", rid)

    monkeypatch.setattr(system.loop, "_drive_claimed", driving)
    asyncio.run(system.loop.drive_run(receipt.run_id))
    assert len(renewed) >= 2
    assert system.store.get_run("default", receipt.run_id).lease_owner is None


def test_lost_lease_stops_generation_without_cancelling_worker(make_system, monkeypatch):
    system = make_system(model_script=[])
    system.boot()
    system.loop._limits.lease_ttl_s = 1
    receipt = asyncio.run(system.ingress.accept({"payload": {"goal": "lost lease"}}, channel="cli",
                         credentials={"token": owner_token(system)}))
    monkeypatch.setattr(system.store, "renew_run_lease", lambda *args: False)
    finished = []

    async def driving(rid, owner, lease, tenant_id):
        await asyncio.sleep(2)
        finished.append(True)
        return system.store.get_run("default", rid)

    monkeypatch.setattr(system.loop, "_drive_claimed", driving)
    assert asyncio.run(system.loop.drive_run(receipt.run_id)) is not None
    assert not finished
    assert any(e["kind"] == "lease.lost" for e in system.store.event_list("default", receipt.run_id))
