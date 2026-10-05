"""Tests for core.database_bridge.DatabaseBridge.

DatabaseBridge is the sync facade main.py actually calls: it runs a
background asyncio event loop in its own thread and blocks the calling
thread (often the wx UI thread) on every call. Before this session it had no
timeout at all, so a stuck coroutine froze the entire app forever — the most
commonly reported "WinZapp parou de responder" symptom. These tests cover the
timeout and the close()-race-safety this session added, using the real
background-thread bridge (not mocked) against a temporary on-disk database.
"""

import asyncio
import inspect
import threading
import time

import pytest
from cryptography.fernet import Fernet

from core.database_bridge import (
    DatabaseBridge,
    DatabaseBridgeClosed,
    DatabaseBridgeTimeout,
)


@pytest.fixture
def bridge(tmp_path):
    db_path = str(tmp_path / "test.db")
    key = Fernet.generate_key()
    b = DatabaseBridge(db_path, key)
    yield b
    try:
        b.close()
    except Exception:
        pass


class TestNormalOperation:
    def test_star_flags_round_trip_without_overwriting_body(self, bridge):
        from core.message_stars import confirmed_star_state, remote_star_state
        jid = "synthetic@s.whatsapp.net"
        record = {"key": {"id": "M"}, "messageTimestamp": 1,
                  "message": {"conversation": "keep"}, "starred": True}
        bridge.insert_message(jid, record)
        merged = bridge.merge_message_star_states(jid, [{**record, **remote_star_state({"star": False}, 10)}])
        assert merged[0]["starred"] is True
        bridge.update_message_star_state(jid, "M", confirmed_star_state(False))
        saved = bridge.get_message_by_id(jid, "M")
        assert saved["starred"] is False and saved["message"] == record["message"]

    def test_basic_call_round_trips(self, bridge):
        bridge.upsert_chat("jid@w", {"remoteJid": "jid@w", "pushName": "Foo"})
        chats = bridge.get_chats()
        assert chats["jid@w"]["pushName"] == "Foo"

    def test_get_chats_default_limit_matches_page_size(self, bridge):
        """Same regression this session fixed at the DatabaseManager level:
        the bridge's own default used to be 5 too."""
        bridge.upsert_chat("jid@w", {"remoteJid": "jid@w"})
        for i in range(12):
            bridge.insert_message("jid@w", {
                "key": {"remoteJid": "jid@w", "id": f"m{i}"},
                "messageTimestamp": i,
            })
        chats = bridge.get_chats()
        assert len(chats["jid@w"]["messages"]["messages"]["records"]) == 12


class TestTimeout:
    def test_slow_coroutine_raises_timeout_instead_of_hanging(self, bridge):
        start = time.monotonic()
        with pytest.raises(DatabaseBridgeTimeout):
            bridge._call(asyncio.sleep(0.6), timeout=0.2)
        elapsed = time.monotonic() - start
        # The whole point of the fix: the caller gets control back close to
        # the timeout, not after the full duration the coroutine sleeps.
        assert elapsed < 0.5

    def test_bridge_stays_usable_after_a_timeout(self, bridge):
        """A timed-out call must not corrupt the bridge/loop for later calls
        — the underlying coroutine finishing late on the loop thread is
        expected and must not affect anything after it."""
        with pytest.raises(DatabaseBridgeTimeout):
            bridge._call(asyncio.sleep(0.3), timeout=0.1)
        bridge.upsert_chat("jid@w", {"remoteJid": "jid@w", "pushName": "StillWorks"})
        chats = bridge.get_chats()
        assert chats["jid@w"]["pushName"] == "StillWorks"


class TestClose:
    def test_calls_after_close_raise_immediately(self, bridge):
        bridge.close()
        with pytest.raises(DatabaseBridgeClosed):
            bridge.get_chats()

    def test_close_is_idempotent(self, bridge):
        bridge.close()
        bridge.close()  # must not raise

    def test_close_waits_for_in_flight_call_instead_of_stranding_it(self, tmp_path):
        """A call already running when close() starts must still be allowed
        to finish (close() drains briefly) rather than the loop being pulled
        out from under it, which would otherwise leave that caller's
        future.result() blocked forever with nothing left to resolve it."""
        import threading

        db_path = str(tmp_path / "test2.db")
        b = DatabaseBridge(db_path, Fernet.generate_key())
        result = {}

        def slow_call():
            try:
                result["value"] = b._call(asyncio.sleep(0.3, result=42), timeout=5)
            except Exception as exc:
                result["error"] = exc

        t = threading.Thread(target=slow_call)
        t.start()
        time.sleep(0.05)  # let slow_call actually start before closing
        b.close()
        t.join(timeout=5)

        assert result.get("value") == 42
        assert "error" not in result


class TestATimeoutDoesNotCancelTheCoroutine:
    """A timed-out call leaves its coroutine running, on purpose.

    An earlier revision of this branch cancelled the future instead, so that
    no stale write could land after the caller gave up. Two things killed
    that idea, both measured rather than argued:

    * It corrupts the database. Batch writes run `BEGIN` ... `COMMIT` under
      `except Exception: await conn.rollback()`, and CancelledError is a
      BaseException, so the rollback is skipped and the half-finished
      transaction stays open on the single shared connection for the next
      write to commit. Cancelling `import_from_dict(clear_first=True)`
      mid-flight left the old chats deleted and 285 of 4000 new ones
      committed.
    * It did not even work as advertised. `run_coroutine_threadsafe` queues
      the task creation before the cancel, so a "still queued" coroutine
      with no await before its side effect runs to completion anyway — while
      `future.cancel()` returns True, because the concurrent future never
      leaves PENDING. The log line said "cancel succeeded" on the same line
      the coroutine ran.

    See DatabaseBridgeTimeout's docstring for what would have to change in
    database.py before cancelling could be reconsidered."""

    def test_the_coroutine_still_completes_after_its_caller_gave_up(self, bridge):
        ran = threading.Event()

        async def hog():
            # Genuinely blocks the loop thread, unlike asyncio.sleep (which
            # cooperatively yields and would let "second" run interleaved,
            # defeating the point of this test).
            time.sleep(0.4)

        async def second():
            ran.set()

        t = threading.Thread(target=lambda: bridge._call(hog(), timeout=5))
        t.start()
        time.sleep(0.05)  # let hog actually start running on the loop

        with pytest.raises(DatabaseBridgeTimeout):
            bridge._call(second(), timeout=0.1)

        t.join(timeout=5)
        time.sleep(0.1)
        assert ran.is_set(), (
            "the timeout must not cancel the coroutine — a cancelled write "
            "can strand an open transaction the next write then commits"
        )

    def test_a_coroutine_suspended_on_an_await_is_not_interrupted(self, bridge):
        """The test that actually GUARDS the decision.

        The queued-behind-a-hog case above documents it but cannot enforce it:
        that coroutine has no await point before its side effect, so it runs to
        completion whether or not the future was cancelled. This one suspends
        on a real await with the loop free — precisely where a cancel WOULD
        land, and where landing means a half-finished transaction left open on
        the shared connection for the next write to commit.
        """
        ran = threading.Event()

        async def slow():
            await asyncio.sleep(0.4)
            ran.set()

        with pytest.raises(DatabaseBridgeTimeout):
            bridge._call(slow(), timeout=0.1)

        time.sleep(0.6)
        assert ran.is_set(), (
            "the timed-out coroutine was cancelled mid-await — that is what "
            "skips database.py's `except Exception: rollback()`, since "
            "CancelledError is a BaseException"
        )

    def test_bridge_stays_usable_afterward(self, bridge):
        async def hog():
            time.sleep(0.3)

        t = threading.Thread(target=lambda: bridge._call(hog(), timeout=5))
        t.start()
        time.sleep(0.05)

        with pytest.raises(DatabaseBridgeTimeout):
            bridge._call(asyncio.sleep(0, result="never"), timeout=0.05)

        t.join(timeout=5)
        bridge.upsert_chat("jid@w", {"remoteJid": "jid@w", "pushName": "StillWorks"})
        assert bridge.get_chats()["jid@w"]["pushName"] == "StillWorks"


class TestCloseDoesNotRaceANewCall:
    """Regression: _closing and _inflight used to be checked/updated under
    two separate locks — a caller could pass the _closing check in _call(),
    and before it incremented _inflight, close() could see _inflight == 0
    (nothing counted yet), consider everything already drained, and stop
    the loop out from under that caller — which then failed with
    DatabaseBridgeTimeout for a reason that had nothing to do with a slow
    query. One lock around both makes "still open, and now counted" a
    single atomic step close() cannot slip through."""

    def test_the_shared_lock_is_actually_used(self):
        """Structural pin: all three call sites this race depends on must
        agree on one lock — same style as test_lid_merge_keeps_messages.py's
        own structural test for something not practical to force
        deterministically end to end."""
        for method in (DatabaseBridge._call, DatabaseBridge._call_unchecked, DatabaseBridge.close):
            assert "self._state_lock" in inspect.getsource(method), method.__name__

    def test_no_call_times_out_due_to_the_loop_stopping_under_it(self, tmp_path):
        """Hammers _call()/close() concurrently, with no artificial delay,
        to maximise the chance of hitting the race window if it still
        existed. A trivially-fast coroutine timing out here (rather than
        succeeding or being cleanly rejected as closed) would mean the loop
        was pulled out from under a call close() should have counted."""
        db_path = str(tmp_path / "race.db")
        b = DatabaseBridge(db_path, Fernet.generate_key())
        results: list[tuple[str, object]] = []
        results_lock = threading.Lock()

        def caller():
            try:
                value = b._call(asyncio.sleep(0, result="ok"), timeout=2)
                with results_lock:
                    results.append(("ok", value))
            except DatabaseBridgeClosed:
                with results_lock:
                    results.append(("closed", None))
            except Exception as exc:  # pragma: no cover - failure path only
                with results_lock:
                    results.append(("error", exc))

        threads = [threading.Thread(target=caller) for _ in range(30)]
        for t in threads:
            t.start()
        b.close()
        for t in threads:
            t.join(timeout=5)

        outcomes = {kind for kind, _ in results}
        assert outcomes <= {"ok", "closed"}, f"unexpected outcomes: {results}"
        assert len(results) == 30
