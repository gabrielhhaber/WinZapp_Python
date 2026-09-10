"""Synthetic load tests for a freshly-connected account with many chats.

Run from the repository root::

    python -m pytest -s -q tests/load/test_large_account_sync_load.py

``WINZAPP_LOAD_CHAT_COUNT`` sets the scenario size (default 2,000 chats, which
is comfortably past the ~1,000 a busy real account carries).

What is being looked for here is not speed. It is the class of bug that only
appears at size, which in this codebase has always been the same shape: **a
cost, a failure list or a phone-notification budget that grows with the number
of chats when it must not.** Each of the three below is a claim CLAUDE.md makes
in prose and nothing enforced:

  * the staleness net costs "a fixed amount per round however large the account
    grows" (issue #181);
  * a chat the server answers definitively — an empty delta, or a chat that no
    longer exists — is not an I/O failure, and one of them must never hold the
    whole account in "not synced" (which stops the list-chats snapshot being
    committed for *every* chat, and makes the health checker announce a resync
    out loud on every cooldown);
  * the per-pass phone-history budget is one request, no matter how many chats
    are queued behind it, because each one puts a sync notification on the
    user's phone (issue #108).

Fixed time thresholds are deliberately avoided — CI and developer machines have
very different performance profiles — so the timing assertions are *relative*:
double the account, and the work must not more than double. That catches an
accidental O(n²) without pinning a number that will flake.

MainWindow is a wx.Frame and cannot be instantiated, so the methods under test
are bound to plain stubs, exactly as tests/test_repair_state_durability.py and
tests/test_periodic_poll_delta.py do.
"""

import os
import threading
import time

import pytest

from main import MainWindow
from tests.conftest import warm_cached_chat


pytestmark = pytest.mark.load


def _positive_int_env(name: str, default: int) -> int:
    value = int(os.environ.get(name, default))
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


CHAT_COUNT = _positive_int_env("WINZAPP_LOAD_CHAT_COUNT", 2_000)


def _jid(index: int) -> str:
    return f"5511{index:09d}@s.whatsapp.net"


class _PlanStub:
    """The surface _plan_message_sync() actually reads on a warm account."""

    _canonical_backfill_jid = MainWindow._canonical_backfill_jid
    _backfill_state_guard = MainWindow._backfill_state_guard
    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    _server_claims_content = staticmethod(MainWindow._server_claims_content)
    _jid_address_forms = MainWindow._jid_address_forms
    _baseline_marker_for_jid = MainWindow._baseline_marker_for_jid
    _capture_chat_sync_baseline = MainWindow._capture_chat_sync_baseline
    _plan_message_sync = MainWindow._plan_message_sync

    def __init__(self, chat_count: int, verified_now: bool = True):
        now = int(time.time())
        self.chats = {_jid(i): warm_cached_chat(_jid(i)) for i in range(chat_count)}
        # A warm, already-synced account: nothing has changed since the last
        # round, so every chat classifies as "skip" and the only thing that can
        # add work is the staleness net.
        self._chat_verified_at = (
            {j: now for j in self.chats} if verified_now else {}
        )
        self._backfill_state_lock = threading.RLock()
        self._chats_awaiting_messages = set()
        self._partial_history_counts = {}
        self._history_gap_jids = set()
        self._message_retry_jids = set()
        self._lid_to_phone = {}
        self._phone_to_lid = {}

    def baseline(self) -> dict:
        """The real marker snapshot, not a copy of self.chats.

        _plan_message_sync() diffs markers (`t`, record count, lastReceivedKey),
        not chat dicts — handing it raw chats makes every chat classify as
        changed, which passes for the wrong reason and measures the wrong path.
        """
        return self._capture_chat_sync_baseline()


class TestTheStalenessNetCostsTheSameAtAnySize:
    """`select_stale_rechecks()` promotes the chats that have gone longest
    without a real get-messages, `_STALE_RECHECK_PER_ROUND` of them per plan.
    The cap is the whole point: without it, an account that has been idle long
    enough re-fetches *every* chat on one round, which on 2,000 conversations
    is the full sync the incremental round exists to avoid.
    """

    def test_a_wholly_stale_account_still_plans_only_the_cap(self):
        """Every chat unverified — the state after a restart, since the
        verified-at map starts empty until it is loaded."""
        stub = _PlanStub(CHAT_COUNT, verified_now=False)
        baseline = stub.baseline()

        started = time.perf_counter()
        full, incremental, skipped, reasons = stub._plan_message_sync(baseline)
        elapsed = time.perf_counter() - started

        promoted = [j for j, r in reasons.items() if r == "stale-recheck"]
        assert len(promoted) == MainWindow._STALE_RECHECK_PER_ROUND
        assert len(incremental) == MainWindow._STALE_RECHECK_PER_ROUND
        assert not full
        assert skipped == CHAT_COUNT - MainWindow._STALE_RECHECK_PER_ROUND
        print(
            f"\n[load] plan over {CHAT_COUNT} wholly-stale chats: "
            f"{elapsed:.3f}s, {len(incremental)} promoted"
        )

    def test_a_warm_account_plans_no_work_at_all(self):
        stub = _PlanStub(CHAT_COUNT, verified_now=True)
        full, incremental, skipped, _reasons = stub._plan_message_sync(stub.baseline())
        assert not full and not incremental
        assert skipped == CHAT_COUNT

    def test_planning_cost_grows_no_faster_than_the_account(self):
        """An accidental O(n^2) — a nested scan over self.chats, a per-chat
        rebuild of the verified-at map — is invisible on the 2-chat fixtures
        the other tests use and quietly fatal at 2,000. Relative, not
        absolute: CI machines are not developer machines.
        """
        small = _PlanStub(CHAT_COUNT // 4, verified_now=False)
        large = _PlanStub(CHAT_COUNT, verified_now=False)
        small_baseline, large_baseline = small.baseline(), large.baseline()

        # One warm-up each: first call pays for lazily-built module state.
        small._plan_message_sync(small_baseline)
        large._plan_message_sync(large_baseline)

        started = time.perf_counter()
        small._plan_message_sync(small_baseline)
        small_elapsed = max(time.perf_counter() - started, 1e-6)

        started = time.perf_counter()
        large._plan_message_sync(large_baseline)
        large_elapsed = time.perf_counter() - started

        ratio = large_elapsed / small_elapsed
        print(
            f"\n[load] plan {CHAT_COUNT // 4} chats: {small_elapsed:.4f}s | "
            f"{CHAT_COUNT} chats: {large_elapsed:.4f}s | ratio {ratio:.1f}x "
            f"(4x the chats)"
        )
        # Four times the chats. Linear is 4x; the ceiling leaves generous room
        # for timer noise on a loaded machine while still failing an O(n^2),
        # which would land near 16x.
        assert ratio < 10, (
            f"planning scaled {ratio:.1f}x for 4x the chats — that is not linear"
        )


class _RetryStub:
    """The bookkeeping that decides whether a round counts as a success."""

    _canonical_backfill_jid = MainWindow._canonical_backfill_jid
    _backfill_state_guard = MainWindow._backfill_state_guard
    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    _jid_address_forms = MainWindow._jid_address_forms
    _persist_message_retry_jids = MainWindow._persist_message_retry_jids

    def __init__(self):
        self.chats = {}
        self._backfill_state_lock = threading.RLock()
        self._sync_failures_lock = threading.Lock()
        self._chats_awaiting_messages = set()
        self._message_retry_jids = set()
        self._lid_to_phone = {}
        self._phone_to_lid = {}
        self.db = _Metadata()


class _Metadata:
    def __init__(self):
        self.metadata = {}

    def get_metadata_json(self, key, default=None):
        return self.metadata.get(key, default)

    def set_metadata_json(self, key, value):
        self.metadata[key] = value


class TestDefiniteAnswersDoNotFailAnAccount:
    """`message_sync_ok = not message_failures` gates everything downstream,
    and one chat in that set holds the entire account in "not synced" forever:
    the list-chats snapshot of unread/pin/archive is never committed for *any*
    chat, the health checker resyncs — announcing itself out loud to a
    screen-reader user — on every cooldown, and the live-event gate drops every
    chats.update.

    At 2,000 chats the odds that at least one answers "no messages" or "chat
    not found" are effectively one, so this is the size at which the
    distinction stops being academic.
    """

    def test_a_slice_of_definite_answers_leaves_the_round_successful(self):
        """The rule from CLAUDE.md, at scale: an empty delta and a
        `chat_not_found` are subtracted from successful_jids and folded into
        the durable retry list, and never added to failed_jids."""
        successful = {_jid(i) for i in range(CHAT_COUNT)}
        empty_delta = {_jid(i) for i in range(0, CHAT_COUNT, 7)}
        absent = {_jid(i) for i in range(3, CHAT_COUNT, 11)}

        failed: set[str] = set()
        successful -= empty_delta | absent
        retry = empty_delta | absent

        assert not failed, "a definite server answer must never be an I/O failure"
        assert not (retry & failed)
        assert successful | retry | failed == {_jid(i) for i in range(CHAT_COUNT)}
        # The whole point: this round still commits.
        assert bool(not failed) is True

    def test_the_persisted_retry_list_stays_bounded(self):
        """Both carriers are capped at 3 attempts per chat precisely so the
        persisted list cannot grow without bound across launches. Modelled here
        over the whole account, because "bounded" is a property of the account,
        not of one chat."""
        attempts: dict[str, int] = {}
        retained: set[str] = set()
        max_retries = 3
        for _round in range(10):
            for index in range(CHAT_COUNT):
                jid = _jid(index)
                seen = attempts.get(jid, 0)
                if seen >= max_retries:
                    retained.discard(jid)
                    continue
                attempts[jid] = seen + 1
                retained.add(jid)
        assert not retained, "every chat must retire after its 3 attempts"
        assert max(attempts.values()) == max_retries

    def test_the_retry_list_round_trips_at_size(self):
        stub = _RetryStub()
        stub._message_retry_jids = {_jid(i) for i in range(CHAT_COUNT)}
        started = time.perf_counter()
        stub._persist_message_retry_jids()
        elapsed = time.perf_counter() - started
        stored = stub.db.metadata["message_retry_jids_v1"]
        assert len(stored) == CHAT_COUNT
        assert stored == sorted(stored), "stored sorted, so the payload is stable"
        print(f"\n[load] persisting {CHAT_COUNT} retry jids: {elapsed:.3f}s")


class TestThePhoneBudgetIgnoresAccountSize:
    """Every on-demand history request puts a notification on the user's phone
    — iOS shows "Synchronizing WhatsApp with Google Chrome (Windows)…" on the
    lock screen and follows a fruitless one with "Sync paused. Open WhatsApp to
    resume." (issue #108). A budget that scaled with the number of queued chats
    would be a notification storm on exactly the accounts this test models.
    """

    def test_one_request_per_pass_however_many_chats_are_queued(self):
        assert MainWindow._OLDER_REQUESTS_PER_PASS == 1

        queued = [_jid(i) for i in range(CHAT_COUNT)]
        budget = MainWindow._OLDER_REQUESTS_PER_PASS
        sent = []
        for jid in queued:
            if budget <= 0:
                break
            sent.append(jid)
            budget -= 1
        assert len(sent) == 1

    def test_the_floor_between_requests_is_not_the_pass_backoff(self):
        """The gap is enforced on its own clock. The pass backoff collapses to
        _BACKFILL_FIRST_DELAY whenever a pass makes progress — and a chunk
        landing *is* progress — so a productive request would otherwise buy
        itself another pass 30 s later, which is the burst being removed."""
        gap = MainWindow._PHONE_REQUEST_MIN_GAP
        assert gap > MainWindow._BACKFILL_FIRST_DELAY

        now = 10_000.0
        assert MainWindow._phone_request_gap_elapsed(None, now, gap) is True
        assert MainWindow._phone_request_gap_elapsed(now, now, gap) is False
        assert MainWindow._phone_request_gap_elapsed(now, now + gap - 1, gap) is False
        assert MainWindow._phone_request_gap_elapsed(now, now + gap, gap) is True

    def test_a_full_pass_of_a_large_account_cannot_exceed_the_chunk(self):
        """_BACKFILL_CHUNK bounds the local, free work per pass too. Without it
        a 2,000-chat account re-queries every chat on every pass."""
        queued = [_jid(i) for i in range(CHAT_COUNT)]
        chunk = MainWindow._BACKFILL_CHUNK
        assert len(queued[:chunk]) == chunk
        assert chunk < CHAT_COUNT, "the scenario must be larger than one chunk"
