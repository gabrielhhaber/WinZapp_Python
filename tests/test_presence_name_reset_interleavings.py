"""Repeated metadata resets must neither revive old names nor lose fresh ones.

Synthetic events/dictionary only: no database, threads, timeouts or GUI.
"""
import asyncio
import itertools

import pytest

from core.latest_metadata import LatestMetadataWrite


CASES = tuple(itertools.product((False, True), (False, True),
                                ("neither", "first", "second"), (False, True)))


@pytest.mark.parametrize("first_fails,second_fails,new_during,write_first", CASES)
async def test_two_resets_keep_only_names_authorized_after_the_last_successful_clear(
        first_fails, second_fails, new_during, write_first):
    stored, writes, runners = {}, [], []
    entered = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]

    async def write(value):
        stored.clear()
        stored.update(value)
        writes.append(dict(value))

    queue = LatestMetadataWrite(write)

    def offer(value):
        lease = queue.offer(value)
        if lease is not None:
            runners.append(asyncio.create_task(queue.run(lease)))

    async def clear(index, fails):
        entered[index].set()
        await release[index].wait()
        if fails:
            raise ValueError("synthetic rolled-back reset")
        stored.clear()

    initial = queue.offer({"old": "value"})
    if write_first:
        runners.append(asyncio.create_task(queue.run(initial)))
        await runners[0]
    first = asyncio.create_task(queue.reset(lambda: clear(0, first_fails)))
    await entered[0].wait()
    if not write_first:
        # Its snapshot was accepted before reset, but dispatch starts later.
        runners.append(asyncio.create_task(queue.run(initial)))
    if new_during == "first":
        offer({"middle": "value"})
    second = asyncio.create_task(queue.reset(lambda: clear(1, second_fails)))
    # The second reset is queued behind the first reset's gate.
    await asyncio.sleep(0)
    assert not entered[1].is_set()
    release[0].set()
    await entered[1].wait()
    if new_during == "second":
        offer({"fresh": "value"})
    release[1].set()
    outcomes = await asyncio.gather(first, second, return_exceptions=True)
    await asyncio.gather(*runners)
    await queue.flush()

    for outcome, failed in zip(outcomes, (first_fails, second_fails)):
        assert isinstance(outcome, ValueError) if failed else outcome is None
    if new_during == "second":
        expected = {"fresh": "value"}
    elif new_during == "first" and second_fails:
        expected = {"middle": "value"}
    elif new_during == "neither" and first_fails and second_fails:
        expected = {"old": "value"}
    else:
        expected = {}
    assert stored == expected
    # Completion/close must not replay an invalidated generation later.
    attempts = len(writes)
    await queue.flush()
    assert stored == expected and len(writes) == attempts


@pytest.mark.parametrize("initial_write_fails", [False, True])
async def test_successful_reset_discards_both_committed_and_retained_failed_names(initial_write_fails):
    stored, attempts = {}, []

    async def write(value):
        attempts.append(dict(value))
        if initial_write_fails:
            raise RuntimeError("synthetic unavailable database")
        stored.update(value)

    async def clear():
        stored.clear()

    queue = LatestMetadataWrite(write)
    await queue.run(queue.offer({"old": "value"}))
    assert len(attempts) == 1
    await queue.reset(clear)
    await queue.flush()
    assert stored == {} and len(attempts) == 1
