"""Synthetic load tests for the storage layer of a large, freshly-synced account.

Run from the repository root::

    python -m pytest -s -q tests/load/test_large_account_database_load.py

``WINZAPP_LOAD_CHAT_COUNT`` and ``WINZAPP_LOAD_MESSAGES_PER_CHAT`` set the
scenario (default 1,200 chats x 40 messages = 48,000 messages, which is what a
busy account looks like after an initial sync).

Why this is the layer worth loading. ``core/database.py`` is a **single
serialized aiosqlite connection** behind a per-write ``asyncio.Lock``, and
every payload column (``message_json``, ``last_message_json``) is
Fernet-encrypted with the per-install key. So each stored message costs a
Fernet token, and every write in the account queues behind one lock. On top of
that sits ``DatabaseBridge``, whose every call blocks the calling wx/worker
thread on ``run_coroutine_threadsafe(...).result(timeout=...)`` — which is why
a slow storage layer does not show up as "sync is slow", it shows up as
"WinZapp stopped responding", historically the most-reported symptom.

What is asserted is therefore not a wall-clock budget (CI and developer
machines differ far too much for that) but the two things that actually break
at size:

  * **cost stays proportional to the data** — an accidental O(n^2), a per-chat
    full-table scan, or a re-read of every message to store one, is invisible
    on the two-row fixtures the rest of the suite uses and fatal at 48,000;
  * **nothing is silently lost** — batching, encryption and the id-less-message
    guard must round-trip the whole account, not most of it.

Both are measured against the real ``DatabaseManager`` on a real on-disk SQLite
file, not ``:memory:``: WAL, the write lock and fsync behaviour are the parts
under test, and an in-memory database does not have them.
"""

import os
import time

import pytest
from cryptography.fernet import Fernet

from tests.conftest import fastest_of_async


pytestmark = pytest.mark.load


def _positive_int_env(name: str, default: int) -> int:
    value = int(os.environ.get(name, default))
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


CHAT_COUNT = _positive_int_env("WINZAPP_LOAD_CHAT_COUNT", 1_200)
PER_CHAT = _positive_int_env("WINZAPP_LOAD_MESSAGES_PER_CHAT", 40)


def _jid(index: int) -> str:
    return f"5511{index:09d}@s.whatsapp.net"


def _message(jid: str, index: int) -> dict:
    """A message in the canonical shape websocket_client.py normalises to."""
    return {
        "key": {"remoteJid": jid, "fromMe": index % 3 == 0, "id": f"{jid}-m{index}"},
        "message": {"conversation": f"mensagem {index} " + "x" * 120},
        "messageType": "conversation",
        "messageTimestamp": 1_700_000_000 + index,
        "pushName": "Contato",
    }


def _chat(jid: str, newest: dict) -> dict:
    return {
        "remoteJid": jid,
        "t": newest["messageTimestamp"],
        "unreadCount": 0,
        "lastMessage": newest,
    }


@pytest.fixture
async def disk_db(tmp_path):
    """A real on-disk DatabaseManager. WAL and the write lock are the subject
    here, and :memory: has neither."""
    from core.database import DatabaseManager

    async with DatabaseManager(str(tmp_path / "messages.db"), Fernet.generate_key()) as db:
        yield db


async def _populate(db, chat_count, per_chat):
    """Store the account the way an initial sync does: messages in per-chat
    batches, chats in one bulk upsert."""
    chats = {}
    for index in range(chat_count):
        jid = _jid(index)
        messages = [_message(jid, n) for n in range(per_chat)]
        await db.insert_messages_batch(jid, messages)
        chats[jid] = _chat(jid, messages[-1])
    await db.upsert_chats_batch(chats)
    return chats


class TestTheWholeAccountRoundTrips:
    async def test_every_message_of_every_chat_survives_storage(self, disk_db):
        """Batching, Fernet and the id-less-message guard, over the whole
        account rather than one row. A batch that silently drops part of itself
        looks exactly like a short conversation to every caller above."""
        started = time.perf_counter()
        await _populate(disk_db, CHAT_COUNT, PER_CHAT)
        write_elapsed = time.perf_counter() - started

        total = CHAT_COUNT * PER_CHAT
        print(
            f"\n[load] stored {total} messages across {CHAT_COUNT} chats in "
            f"{write_elapsed:.2f}s ({total / max(write_elapsed, 1e-6):.0f} msg/s)"
        )

        jids = await disk_db.get_chat_jids()
        assert len(jids) == CHAT_COUNT

        # Spot-check across the range rather than all of them: a per-chat count
        # is a query each, and the failure modes here are systematic, not
        # sporadic.
        for index in (0, CHAT_COUNT // 2, CHAT_COUNT - 1):
            assert await disk_db.get_message_count(_jid(index)) == PER_CHAT

    async def test_the_encrypted_payload_comes_back_intact(self, disk_db):
        """Fernet is applied per payload. A key/encoding mistake shows up as a
        message that stores fine and reads back as junk — which the UI renders
        as an empty row, not as an error."""
        await _populate(disk_db, min(CHAT_COUNT, 50), PER_CHAT)
        jid = _jid(7)
        messages = await disk_db.get_messages(jid, limit=PER_CHAT)
        assert len(messages) == PER_CHAT
        bodies = {m["message"]["conversation"] for m in messages}
        assert f"mensagem 0 " + "x" * 120 in bodies
        assert all(m["key"]["remoteJid"] == jid for m in messages)


class TestCostStaysProportionalToTheData:
    """The assertions are ratios, not seconds. A machine four times slower
    fails nothing; a query that got quadratic fails everywhere."""

    async def test_loading_the_chat_list_scales_with_the_chat_count(self, disk_db):
        """get_chats() is on the blocking startup path — prepare_sync() waits
        for it before the window is shown, through DatabaseBridge, which blocks
        the calling thread on a timeout.

        Note what it costs per chat, because the signature hides it: `limit`
        here is the MESSAGE page size, and every chat pays
        _build_message_wrapper() — one COUNT query, one SELECT, and a Fernet
        decrypt per record returned. The growth is linear but the constant is
        large, and the constant is what gets multiplied by the account.
        """
        await _populate(disk_db, CHAT_COUNT, PER_CHAT)

        await disk_db.get_chats()  # warm the page cache
        started = time.perf_counter()
        chats = await disk_db.get_chats()
        elapsed = time.perf_counter() - started

        assert len(chats) == CHAT_COUNT
        assert all(
            chat["messages"]["messages"]["total"] == PER_CHAT
            for chat in list(chats.values())[:5]
        )
        print(
            f"\n[load] get_chats() over {CHAT_COUNT} chats x {PER_CHAT} msgs: "
            f"{elapsed:.3f}s ({elapsed / CHAT_COUNT * 1000:.2f} ms/chat)"
        )

    async def test_the_chat_list_cost_is_linear_in_the_chat_count(self, disk_db):
        """Linear is the requirement; the constant is separately large (see
        above). A regression that made this quadratic would be invisible on the
        handful of chats the rest of the suite uses and would push a real
        account past DatabaseBridge's timeout — which reaches the user as
        "WinZapp stopped responding", not as a slow query.
        """
        quarter = max(2, CHAT_COUNT // 4)
        await _populate(disk_db, quarter, PER_CHAT)
        await disk_db.get_chats()          # warm the page cache
        # Quickest of several, not one sample — see conftest.fastest_of().
        small_elapsed = max(
            await fastest_of_async(lambda _i: disk_db.get_chats()), 1e-6)

        for index in range(quarter, CHAT_COUNT):
            jid = _jid(index)
            messages = [_message(jid, n) for n in range(PER_CHAT)]
            await disk_db.insert_messages_batch(jid, messages)
            await disk_db.upsert_chat(jid, _chat(jid, messages[-1]))

        await disk_db.get_chats()
        large_elapsed = await fastest_of_async(lambda _i: disk_db.get_chats())

        assert len(await disk_db.get_chats()) == CHAT_COUNT
        ratio = large_elapsed / small_elapsed
        print(
            f"\n[load] get_chats() — {quarter} chats: {small_elapsed:.3f}s | "
            f"{CHAT_COUNT} chats: {large_elapsed:.3f}s | ratio {ratio:.1f}x "
            f"(4x the chats)"
        )
        assert ratio < 10, (
            f"the chat list scaled {ratio:.1f}x for 4x the chats — not linear"
        )

    async def test_storing_four_times_the_messages_costs_about_four_times(
        self, disk_db
    ):
        """The shape that would fail this: re-reading a chat's stored messages
        to insert one more (_with_known_video_duration is called per message
        and does touch the table), or a per-batch full scan."""
        # A fresh chat per repeat: inserting the same batch into the same chat
        # twice measures an update, not an insert. Quickest of several runs —
        # see conftest.fastest_of().
        def _insert(base, count):
            async def _run(index):
                jid = _jid(base + index)
                await disk_db.insert_messages_batch(
                    jid, [_message(jid, n) for n in range(count)])
            return _run

        # Warm-up so neither measurement pays for connection setup.
        warm = _jid(90_000)
        await disk_db.insert_messages_batch(
            warm, [_message(warm, n) for n in range(PER_CHAT)])

        small_elapsed = max(
            await fastest_of_async(_insert(91_000, PER_CHAT)), 1e-6)
        large_elapsed = await fastest_of_async(_insert(92_000, PER_CHAT * 4))

        ratio = large_elapsed / small_elapsed
        print(
            f"\n[load] insert {PER_CHAT} msgs: {small_elapsed:.4f}s | "
            f"{PER_CHAT * 4} msgs: {large_elapsed:.4f}s | ratio {ratio:.1f}x"
        )
        assert await disk_db.get_message_count(_jid(92_000)) == PER_CHAT * 4
        # Linear is 4x. The ceiling absorbs timer noise on a loaded machine
        # while still failing a quadratic, which lands near 16x.
        assert ratio < 12, (
            f"storing 4x the messages cost {ratio:.1f}x — that is not linear"
        )

    async def test_reading_one_conversation_does_not_scale_with_the_account(
        self, disk_db
    ):
        """Opening a chat must cost the same on a 20-chat account and a
        1,200-chat one — this is the query behind every conversation the user
        opens, and `remote_jid`/`timestamp` are indexed precisely for it."""
        await _populate(disk_db, min(CHAT_COUNT, 20), PER_CHAT)
        await disk_db.get_messages(_jid(1), limit=PER_CHAT)
        started = time.perf_counter()
        await disk_db.get_messages(_jid(1), limit=PER_CHAT)
        small_elapsed = max(time.perf_counter() - started, 1e-6)

        # Grow the account around that same conversation.
        for index in range(20, CHAT_COUNT):
            jid = _jid(index)
            await disk_db.insert_messages_batch(
                jid, [_message(jid, n) for n in range(PER_CHAT)]
            )

        await disk_db.get_messages(_jid(1), limit=PER_CHAT)
        started = time.perf_counter()
        messages = await disk_db.get_messages(_jid(1), limit=PER_CHAT)
        large_elapsed = time.perf_counter() - started

        assert len(messages) == PER_CHAT
        ratio = large_elapsed / small_elapsed
        print(
            f"\n[load] open one chat — 20 chats: {small_elapsed:.4f}s | "
            f"{CHAT_COUNT} chats: {large_elapsed:.4f}s | ratio {ratio:.1f}x"
        )
        # An index lookup should be flat. Generous, because these are
        # sub-millisecond numbers where timer noise dominates; a full table
        # scan over 60x the rows would not fit under this.
        assert ratio < 12, (
            f"opening one chat got {ratio:.1f}x slower as the account grew — "
            "that reads like a table scan, not an index lookup"
        )
