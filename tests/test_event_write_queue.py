"""A long offline replay must not wait for SQLite in wx event handlers."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
import pytest_asyncio

from core.database_bridge import DatabaseBridgeClosed
import core.database_bridge as database_bridge
import core.event_writes as event_writes
from tests.test_presence_name_persistence import harness


@pytest_asyncio.fixture
async def events(harness, monkeypatch):
    h = harness
    monkeypatch.setattr(event_writes, "asyncio", database_bridge.asyncio)
    h.batches, h.contacts, h.metadata = [], [], []

    async def messages(jid, records):
        h.batches.append((jid, deepcopy(records)))

    async def contacts(db, records):
        h.contacts.append(deepcopy(records))

    async def metadata(key, value):
        h.metadata.append((key, deepcopy(value)))

    h.bridge._db.insert_messages_batch = messages
    monkeypatch.setattr(event_writes, "apply_contact_batch", contacts)
    h.bridge._db.set_metadata_json = metadata
    return h


def message(mid, status=1):
    return {"key": {"id": mid}, "messageType": "conversation",
            "message": {"conversation": "synthetic"}, "status": status}


async def drain(h):
    await h.scheduled.pop(0)


async def test_offers_return_without_writing_and_batch_distinct_ids(events):
    h = events
    for i in range(100):
        h.bridge.queue_message("chat", message(str(i)))
    assert h.batches == [] and len(h.scheduled) == h.bridge._inflight == 1
    await drain(h)
    assert len(h.batches) == 1 and len(h.batches[0][1]) == 100
    assert h.bridge._inflight == 0 and h.bridge._inflight_drained.is_set()


async def test_latest_status_wins_and_queued_values_are_copied(events):
    h = events
    record = message("m", 2)
    h.bridge.queue_message("chat", record)
    record["status"] = 3
    h.bridge.queue_message("chat", record)
    record["status"] = 99
    await drain(h)
    assert h.batches[0][1][0]["status"] == 3


async def test_contacts_and_metadata_coalesce_without_losing_other_keys(events):
    h = events
    h.bridge.queue_contacts({"a": {"name": "old"}, "b": {"name": "keep"}}, {})
    h.bridge.queue_contacts({"a": {"name": "new"}}, {"a": {"name": "old"}})
    h.bridge.queue_metadata("read", {"a": 1})
    h.bridge.queue_metadata("read", {"a": 2})
    await drain(h)
    assert h.contacts == [[("a", None, {"name": "old"}),
                           ("b", None, {"name": "keep"}),
                           ("a", {"name": "old"}, {"name": "new"})]]
    assert h.metadata == [("read", {"a": 2})]


async def test_inflight_batch_finishes_before_newer_status(events):
    h, entered, release = events, asyncio.Event(), asyncio.Event()
    original = h.bridge._db.insert_messages_batch

    async def slow(jid, records):
        if not h.batches:
            entered.set()
            await release.wait()
        await original(jid, records)

    h.bridge._db.insert_messages_batch = slow
    h.bridge.queue_message("chat", message("m", 2))
    task = asyncio.create_task(h.scheduled.pop(0))
    await entered.wait()
    h.bridge.queue_message("chat", message("m", 4))
    assert len(h.scheduled) == 0 and h.bridge._inflight == 1
    release.set()
    await task
    assert [rows[0]["status"] for _, rows in h.batches] == [2, 4]


async def test_close_drains_accepted_updates_and_rejects_new_events(events):
    h = events
    h.bridge.queue_message("chat", message("m"))
    runner = asyncio.create_task(h.scheduled.pop(0))
    h.bridge._closing = True
    with pytest.raises(DatabaseBridgeClosed):
        h.bridge.queue_message("chat", message("late"))
    await h.bridge._close_database()
    await runner
    assert len(h.batches) == 1 and h.trace == ["close"]


async def test_failed_batch_retains_distinct_keys_when_newer_values_arrive(events):
    h, attempts = events, []

    async def write(changes):
        attempts.append(deepcopy(changes))
        if len(attempts) == 1:
            h.bridge.queue_message("chat", message("new"))
            h.bridge.queue_message("chat", message("old", 4))
            raise RuntimeError("synthetic")

    h.bridge._event_writes._write = write
    h.bridge.queue_message("chat", message("old", 2))
    h.bridge.queue_message("chat", message("keep"))
    await drain(h)
    assert len(attempts) == 2
    assert {key[2] for key in attempts[-1]} == {"old", "keep", "new"}
    assert attempts[-1][("message", "chat", "old")]["status"] == 4


async def test_reset_drops_pending_old_generation_and_allows_new_events(events):
    h = events
    h.bridge.queue_message("chat", message("old"))
    await h.bridge._event_writes.reset(h.bridge._db.clear_all)
    h.bridge.queue_message("chat", message("new"))
    await drain(h)
    assert [m["key"]["id"] for _, rows in h.batches for m in rows] == ["new"]
    assert h.trace == ["clear"]


async def test_failed_reset_recovers_accepted_updates(events):
    h = events
    h.bridge.queue_message("chat", message("kept"))

    async def fail():
        raise ValueError("synthetic")

    # The reset's failure fence waits for the pre-existing runner.
    runner = asyncio.create_task(h.scheduled.pop(0))
    with pytest.raises(ValueError):
        await h.bridge._event_writes.reset(fail)
    await runner
    assert [m["key"]["id"] for _, rows in h.batches for m in rows] == ["kept"]


async def test_failed_write_is_retained_for_one_close_retry(events):
    h, attempts = events, []

    async def fail(changes):
        attempts.append(changes)
        raise RuntimeError("synthetic")

    h.bridge._event_writes._write = fail
    h.bridge.queue_message("chat", message("kept"))
    await drain(h)
    assert len(attempts) == 1 and h.bridge._inflight == 0
    h.bridge._closing = True
    await h.bridge._close_database()
    assert len(attempts) == 2 and h.trace == ["close"]


async def test_scheduling_failure_keeps_updates_and_releases_the_runner(events, monkeypatch):
    h = events
    submit = event_writes.asyncio.run_coroutine_threadsafe

    def fail(*args):
        raise RuntimeError("synthetic submit failure")

    monkeypatch.setattr(event_writes.asyncio, "run_coroutine_threadsafe", fail)
    with pytest.raises(RuntimeError):
        h.bridge.queue_message("chat", message("old"))
    assert h.bridge._inflight == 0 and h.bridge._inflight_drained.is_set()
    monkeypatch.setattr(event_writes.asyncio, "run_coroutine_threadsafe", submit)
    h.bridge.queue_message("chat", message("new"))
    await drain(h)
    assert {m["key"]["id"] for _, rows in h.batches for m in rows} == {"old", "new"}


@pytest.mark.parametrize("clear_metadata", [True, False])
async def test_full_state_reset_fences_queued_messages_even_when_metadata_is_kept(events, clear_metadata):
    h = events
    h.bridge.queue_message("chat", message("old"))
    resets = []
    h.bridge._call = lambda coro, **kwargs: resets.append(coro)
    h.bridge.save_full_state({}, clear_first=True, clear_metadata=clear_metadata)
    await resets.pop()
    await drain(h)
    assert h.batches == []
    assert h.trace == [("import", {"clear_first": True, "clear_metadata": clear_metadata})]


async def test_reset_waits_for_an_active_transaction_then_rejects_old_pending_updates(events):
    h, entered, release = events, asyncio.Event(), asyncio.Event()

    async def slow(jid, records):
        entered.set()
        await release.wait()
        h.trace.append("committed")

    h.bridge._db.insert_messages_batch = slow
    h.bridge.queue_message("chat", message("running"))
    runner = asyncio.create_task(h.scheduled.pop(0))
    await entered.wait()
    h.bridge.queue_message("chat", message("pending"))
    resetting = asyncio.create_task(h.bridge._event_writes.reset(h.bridge._db.clear_all))
    await asyncio.sleep(0)
    assert h.trace == []
    release.set()
    await resetting
    await runner
    assert h.trace == ["committed", "clear"]


def test_reaction_handler_uses_nonblocking_admission():
    from tests.test_reaction_persisted_from_others import _Stub, JID, _reaction_from_other

    panel = _Stub(JID)
    queued = []
    panel.main_window.db = SimpleNamespace(
        queue_message=lambda *args: queued.append(args),
        insert_message=lambda *args: pytest.fail("UI blocked on a database write"))
    panel.on_incoming_message(JID, _reaction_from_other())
    assert len(queued) == 1


def test_ack_handler_updates_memory_without_waiting_for_database():
    from tests.test_on_message_status_monotone import _win_with_message, _ack

    window = _win_with_message()
    queued = []
    window.db = SimpleNamespace(
        queue_message_status=lambda *args: queued.append(args),
        insert_message=lambda *args: pytest.fail("UI blocked on a database write"))
    window.on_message_status_update(_ack(3))
    assert len(queued) == 1


async def test_failed_reset_merges_new_distinct_updates_and_keeps_newer_same_key(events):
    h, entered, release = events, asyncio.Event(), asyncio.Event()
    h.bridge.queue_message("chat", message("old", 2))
    h.bridge.queue_message("chat", message("keep"))

    async def fail():
        entered.set()
        await release.wait()
        raise ValueError("synthetic")

    reset = asyncio.create_task(h.bridge._event_writes.reset(fail))
    await entered.wait()
    # Let the old runner pull a value before discovering its stale generation.
    runner = asyncio.create_task(h.scheduled.pop(0))
    h.bridge.queue_message("chat", message("old", 4))
    h.bridge.queue_message("chat", message("new"))
    await asyncio.sleep(0)
    h.bridge.queue_message("chat", message("last"))
    release.set()
    with pytest.raises(ValueError):
        await reset
    await runner
    saved = {m["key"]["id"]: m for _, rows in h.batches for m in rows}
    assert saved.keys() == {"old", "keep", "new", "last"}
    assert saved["old"]["status"] == 4
