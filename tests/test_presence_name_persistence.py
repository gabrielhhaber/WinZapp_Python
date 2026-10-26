"""Presence name persistence must never wait for the database on the UI path."""
from types import SimpleNamespace
import asyncio
import json
import sqlite3
import threading

import pytest
import pytest_asyncio

from core.database_bridge import DatabaseBridge, DatabaseBridgeClosed, DatabaseBridgeTimeout
from core.latest_metadata import LatestMetadataWrite
import core.database_bridge as database_bridge
from tests.test_presence_ui_load import PresenceOwner


def test_presence_handler_queues_names_without_calling_synchronous_metadata():
    owner = PresenceOwner()
    jid = "123@s.whatsapp.net"
    owner.contacts[jid] = {"pushName": "Synthetic contact"}
    admitted = []
    owner.db = SimpleNamespace(
        queue_presence_names=lambda value: admitted.append(dict(value)),
        set_metadata_json=lambda *args: pytest.fail("UI waited for synchronous metadata"))
    owner.on_presence_update(jid, {jid: {"lastKnownPresence": "available"}})
    assert admitted == [{jid: "Synthetic contact"}]
    owner.on_presence_update(jid, {jid: {"lastKnownPresence": "unavailable"}})
    assert len(admitted) == 1


@pytest_asyncio.fixture
async def harness(monkeypatch):
    """Real admission/counter/reset methods, fake DB and submitter; no threads."""
    scheduled, writes, trace = [], [], []
    async def write(key, value):
        writes.append(dict(value))
        trace.append("write")
    async def close(): trace.append("close")
    async def imported(data, **options): trace.append(("import", options))
    async def clear(): trace.append("clear")
    bridge = DatabaseBridge.__new__(DatabaseBridge)
    bridge._closing, bridge._inflight = False, 0
    bridge._state_lock = threading.Lock()
    bridge._inflight_drained = threading.Event()
    bridge._inflight_drained.set()
    bridge._thread = SimpleNamespace(is_alive=lambda: True)
    bridge._loop = asyncio.get_running_loop()
    bridge._db = SimpleNamespace(set_metadata_json=write, close=close,
                                 import_from_dict=imported, clear_all=clear)
    bridge._presence_names = LatestMetadataWrite(
        lambda value: bridge._db.set_metadata_json("presence_pushname_map", value))
    # Patch only the bridge's submitter, not asyncio shared by pytest's runner.
    bridge_asyncio = SimpleNamespace(**vars(asyncio))
    bridge_asyncio.run_coroutine_threadsafe = lambda coro, loop: scheduled.append(coro)
    monkeypatch.setattr(database_bridge, "asyncio", bridge_asyncio)
    yield SimpleNamespace(bridge=bridge, scheduled=scheduled, writes=writes, trace=trace)
    for coro in scheduled:
        coro.close()


async def run_one(h):
    await h.scheduled.pop(0)


async def test_hundred_offers_reserve_one_runner_and_commit_only_latest_snapshot(harness):
    h = harness
    for i in range(100): h.bridge.queue_presence_names({"synthetic": str(i)})
    assert len(h.scheduled) == h.bridge._inflight == 1 and h.writes == []
    await run_one(h)
    assert h.writes == [{"synthetic": "99"}]
    assert h.bridge._inflight == 0 and h.bridge._inflight_drained.is_set()


async def test_offer_copies_input_before_the_ui_can_mutate_it(harness):
    value = {"synthetic": "before"}
    harness.bridge.queue_presence_names(value)
    value["synthetic"] = "after"
    await run_one(harness)
    assert harness.writes == [{"synthetic": "before"}]


async def test_slow_physical_write_keeps_only_one_latest_successor(harness):
    h, release, entered = harness, asyncio.Event(), asyncio.Event()
    async def slow(key, value):
        h.writes.append(dict(value))
        if len(h.writes) == 1:
            entered.set()
            await release.wait()
    h.bridge._db.set_metadata_json = slow
    h.bridge.queue_presence_names({"synthetic": "first"})
    task = asyncio.create_task(h.scheduled.pop(0))
    await asyncio.sleep(0)
    assert entered.is_set()
    for i in range(100): h.bridge.queue_presence_names({"synthetic": str(i)})
    assert h.bridge._inflight == 1 and h.scheduled == []
    release.set()
    await task
    assert h.writes == [{"synthetic": "first"}, {"synthetic": "99"}]
    assert h.bridge._inflight == 0


async def test_close_waits_for_last_accepted_snapshot_and_rejects_new_admission(harness):
    h, release = harness, asyncio.Event()
    async def slow(key, value):
        if not h.writes: await release.wait()
        h.writes.append(dict(value))
        h.trace.append("write")
    h.bridge._db.set_metadata_json = slow
    h.bridge.queue_presence_names({"synthetic": "first"})
    runner = asyncio.create_task(h.scheduled.pop(0))
    await asyncio.sleep(0)
    h.bridge.queue_presence_names({"synthetic": "last"})
    h.bridge._closing = True
    closing = asyncio.create_task(h.bridge._close_database())
    await asyncio.sleep(0)
    assert not closing.done() and "close" not in h.trace
    with pytest.raises(DatabaseBridgeClosed): h.bridge.queue_presence_names({"late": "reject"})
    release.set()
    await runner
    await closing
    assert h.writes[-1] == {"synthetic": "last"}
    assert h.trace[-1] == "close" and h.bridge._inflight_drained.is_set()


async def test_failed_write_has_no_retry_loop_but_close_retries_once(harness, caplog):
    h, attempts = harness, []
    async def fail(key, value):
        attempts.append(dict(value))
        raise RuntimeError("synthetic-private-name")
    h.bridge._db.set_metadata_json = fail
    h.bridge.queue_presence_names({"synthetic": "value"})
    await run_one(h)
    assert len(attempts) == 1 and h.scheduled == [] and h.bridge._inflight == 0
    h.bridge._closing = True
    await h.bridge._close_database()
    assert len(attempts) == 2 and h.trace == ["close"]
    assert "RuntimeError" in caplog.text and "synthetic-private-name" not in caplog.text


async def test_failed_older_write_cannot_replace_a_newer_pending_snapshot(harness):
    h = harness
    async def write(key, value):
        h.writes.append(dict(value))
        if len(h.writes) == 1:
            h.bridge.queue_presence_names({"new": "value"})
            raise RuntimeError("synthetic failure")
    h.bridge._db.set_metadata_json = write
    h.bridge.queue_presence_names({"old": "value"})
    await run_one(h)
    assert h.writes == [{"old": "value"}, {"new": "value"}]
    assert h.bridge._inflight == 0 and h.scheduled == []


async def test_new_offer_after_failure_retries_latest_full_value(harness):
    h = harness
    async def fail(key, value): raise RuntimeError("synthetic")
    h.bridge._db.set_metadata_json = fail
    h.bridge.queue_presence_names({"old": "value"})
    await run_one(h)
    async def good(key, value): h.writes.append(dict(value))
    h.bridge._db.set_metadata_json = good
    h.bridge.queue_presence_names({"new": "value"})
    await run_one(h)
    assert h.writes == [{"new": "value"}]


async def test_scheduler_failure_releases_reservation_without_losing_snapshot(harness, monkeypatch):
    h = harness
    def broken(coro, loop): raise RuntimeError("synthetic scheduler stopped")
    monkeypatch.setattr(database_bridge.asyncio, "run_coroutine_threadsafe", broken)
    with pytest.raises(RuntimeError): h.bridge.queue_presence_names({"first": "value"})
    assert h.bridge._inflight == 0 and h.bridge._inflight_drained.is_set()
    monkeypatch.setattr(database_bridge.asyncio, "run_coroutine_threadsafe",
                        lambda coro, loop: h.scheduled.append(coro))
    h.bridge.queue_presence_names({"last": "value"})
    await run_one(h)
    assert h.writes == [{"last": "value"}]


@pytest.mark.parametrize("closed", [False, True])
async def test_dead_or_closed_bridge_rejects_before_reserving_work(harness, closed):
    h = harness
    h.bridge._closing = closed
    h.bridge._thread = SimpleNamespace(is_alive=lambda: False)
    with pytest.raises(DatabaseBridgeClosed if closed else DatabaseBridgeTimeout):
        h.bridge.queue_presence_names({"synthetic": "value"})
    assert h.scheduled == [] and h.bridge._inflight == 0


async def test_close_claim_after_admission_still_counts_the_accepted_runner(harness, monkeypatch):
    h = harness
    def submit(coro, loop):
        h.bridge._closing = True
        h.scheduled.append(coro)
    monkeypatch.setattr(database_bridge.asyncio, "run_coroutine_threadsafe", submit)
    h.bridge.queue_presence_names({"synthetic": "value"})
    assert h.bridge._inflight == 1 and not h.bridge._inflight_drained.is_set()
    await run_one(h)
    assert h.bridge._inflight_drained.is_set()


async def test_successor_reserved_at_old_completion_is_not_retired_by_old_finally(harness):
    h = harness
    h.bridge.queue_presence_names({"first": "value"})
    lease = h.bridge._presence_names._active
    lease.add_done_callback(lambda future: h.bridge.queue_presence_names({"next": "value"}))
    await run_one(h)
    assert h.bridge._inflight == 1 and not h.bridge._inflight_drained.is_set()
    await run_one(h)
    assert h.writes == [{"first": "value"}, {"next": "value"}]
    assert h.bridge._inflight == 0


async def test_cancelled_idle_waiter_does_not_cancel_physical_write(harness):
    h, release = harness, asyncio.Event()
    async def slow(key, value):
        await release.wait()
        h.writes.append(dict(value))
    h.bridge._db.set_metadata_json = slow
    h.bridge.queue_presence_names({"synthetic": "value"})
    runner = asyncio.create_task(h.scheduled.pop(0))
    waiter = asyncio.create_task(h.bridge._presence_names.wait_idle())
    await asyncio.sleep(0)
    waiter.cancel()  # Only a synthetic waiter, never the write task/transaction.
    with pytest.raises(asyncio.CancelledError): await waiter
    assert not runner.done() and not h.bridge._presence_names._active.cancelled()
    release.set()
    await runner
    assert h.writes == [{"synthetic": "value"}]


@pytest.mark.parametrize("clear_first,clear_metadata", [(False, False), (False, True), (True, False), (True, True)])
async def test_import_fences_names_only_when_metadata_is_actually_reset(harness, clear_first, clear_metadata):
    h, calls = harness, []
    h.bridge._call = lambda coro, **kwargs: calls.append(coro)
    h.bridge.queue_presence_names({"old": "value"})
    h.bridge.save_full_state({}, clear_first=clear_first, clear_metadata=clear_metadata)
    await calls.pop(0)
    await run_one(h)
    assert h.writes == ([] if clear_first and clear_metadata else [{"old": "value"}])
    assert h.trace[0] == ("import", {"clear_first": clear_first, "clear_metadata": clear_metadata})


async def test_clear_all_drops_a_queued_old_snapshot_before_it_can_start(harness):
    h, calls = harness, []
    h.bridge._call = lambda coro, **kwargs: calls.append(coro)
    h.bridge.queue_presence_names({"old": "value"})
    h.bridge.clear_all()
    await calls.pop(0)
    await run_one(h)
    assert h.trace == ["clear"] and h.writes == []
    h.bridge.queue_presence_names({"new": "value"})
    await run_one(h)
    assert h.writes == [{"new": "value"}]


async def test_reset_waits_for_active_write_and_prevents_old_successor_after_clear(harness):
    h, release = harness, asyncio.Event()
    async def slow(key, value):
        await release.wait()
        h.trace.append(("write", dict(value)))
    h.bridge._db.set_metadata_json = slow
    h.bridge.queue_presence_names({"first": "old"})
    runner = asyncio.create_task(h.scheduled.pop(0))
    await asyncio.sleep(0)
    clearing = asyncio.create_task(h.bridge._presence_names.reset(h.bridge._db.clear_all))
    await asyncio.sleep(0)
    h.bridge.queue_presence_names({"second": "old"})
    assert h.trace == []
    release.set()
    await runner
    await clearing
    assert h.trace == [("write", {"first": "old"}), "clear"]
    h.bridge.queue_presence_names({"new": "value"})
    await run_one(h)
    assert h.trace[-1] == ("write", {"new": "value"})


def test_real_temporary_database_close_preserves_the_last_accepted_names(tmp_path):
    from cryptography.fernet import Fernet
    path = tmp_path / "synthetic.db"
    bridge = DatabaseBridge(str(path), Fernet.generate_key())
    try:
        for i in range(32): bridge.queue_presence_names({"synthetic": str(i)})
    finally:
        bridge.close()
    with sqlite3.connect(path) as conn:
        raw, = conn.execute("SELECT value FROM system_metadata WHERE key='presence_pushname_map'").fetchone()
    assert json.loads(raw) == {"synthetic": "31"}
    assert not bridge._thread.is_alive()


@pytest.mark.parametrize("fail", [False, True])
async def test_offer_during_reset_belongs_after_reset_even_when_reset_fails(harness, fail):
    h, entered, release = harness, asyncio.Event(), asyncio.Event()
    async def reset():
        entered.set()
        await release.wait()
        h.trace.append("reset")
        if fail: raise ValueError("synthetic reset failure")
    h.bridge.queue_presence_names({"old": "value"})
    runner = asyncio.create_task(h.scheduled.pop(0))
    clearing = asyncio.create_task(h.bridge._presence_names.reset(reset))
    await entered.wait()
    h.bridge.queue_presence_names({"new": "value"})
    successor = asyncio.create_task(h.scheduled.pop(0)) if h.scheduled else None
    release.set()
    if fail:
        with pytest.raises(ValueError): await clearing
    else:
        await clearing
    await runner
    if successor is not None: await successor
    assert h.writes[-1] == {"new": "value"}
    assert h.trace[-2:] == ["reset", "write"]


async def test_failed_reset_restores_last_accepted_snapshot_without_another_ui_offer(harness):
    h = harness
    h.bridge.queue_presence_names({"accepted": "value"})
    runner = asyncio.create_task(h.scheduled.pop(0))
    async def fail(): raise ValueError("synthetic reset rollback")
    with pytest.raises(ValueError): await h.bridge._presence_names.reset(fail)
    await runner
    assert h.writes == [{"accepted": "value"}]
    assert h.bridge._inflight == 0


async def test_close_retry_can_recover_a_transient_write_failure(harness):
    h, attempts = harness, []
    async def transient(key, value):
        attempts.append(dict(value))
        if len(attempts) == 1: raise RuntimeError("synthetic transient failure")
        h.writes.append(dict(value))
    h.bridge._db.set_metadata_json = transient
    h.bridge.queue_presence_names({"accepted": "value"})
    await run_one(h)
    await h.bridge._close_database()
    assert len(attempts) == 2 and h.writes == [{"accepted": "value"}]
    assert h.trace == ["close"]


async def test_independent_database_queues_do_not_redirect_each_others_snapshots():
    first, second = [], []
    async def write_first(value): first.append(value)
    async def write_second(value): second.append(value)
    one, two = LatestMetadataWrite(write_first), LatestMetadataWrite(write_second)
    first_lease = one.offer({"first": "value"})
    second_lease = two.offer({"second": "value"})
    await two.run(second_lease)
    await one.run(first_lease)
    assert first == [{"first": "value"}] and second == [{"second": "value"}]
