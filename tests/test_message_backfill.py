"""Tests for retrying chats whose history WhatsApp Web had not loaded yet.

get-messages reads WhatsApp Web's *in-memory* store. Right after pairing that
store is still empty for most chats, so the call answers 200 with an empty list
— indistinguishable from a genuinely empty conversation. Measured on a real
539-chat account: 514 chats came back empty during the sync, and the sync never
ran again because _sync_completed gates it.

The consequences all compound: no local records means effective_unread_count()
clamps every badge to zero (it never claims more unread than records exist), and
a chat with no records, no lastMessage (list-chats returns `msgs: null`) and no
name is dropped from the conversation list entirely.

The same chats answered with 1 message ~40 minutes later and 15-16 shortly
after, so the store does fill in — it just needs to be asked again.
_note_backfill_state() decides who gets asked; that decision is tested here.
"""

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from main import MainWindow


note = MainWindow._note_backfill_state
claims = MainWindow._server_claims_content


class _Stub:
    def __init__(self, page_size=1, chunks_pending=False):
        self._backfill_state_lock = threading.RLock()
        self._chats_awaiting_messages = set()
        # Most cases here predate paging and care only about empty-vs-not, so
        # the default target of 1 keeps a single record meaning "done".
        self.settings = {"user_interface": {"messages_page_size": page_size}}
        self._history_still_landing = chunks_pending

    _server_claims_content = staticmethod(MainWindow._server_claims_content)
    _note_backfill_state = MainWindow._note_backfill_state
    _backfill_state_guard = MainWindow._backfill_state_guard
    _canonical_backfill_jid = MainWindow._canonical_backfill_jid
    _collapse_and_list_backfill_pending = MainWindow._collapse_and_list_backfill_pending
    _remove_backfill_pending = MainWindow._remove_backfill_pending
    _is_backfill_pending = MainWindow._is_backfill_pending
    _completed_backfill_targets = MainWindow._completed_backfill_targets
    _jid_address_forms = MainWindow._jid_address_forms
    _resolve_backfill_target = MainWindow._resolve_backfill_target
    history_page_target = MainWindow.history_page_target
    _local_record_count = MainWindow._local_record_count


class TestBackfillGeneration:
    def test_stale_worker_stops_before_touching_the_chat(self):
        stub = type("Stub", (), {
            "_sync_run_id": 8,
            "sync_chat_messages": MainWindow.sync_chat_messages,
        })()

        result = stub.sync_chat_messages(
            {"remoteJid": "5511999999999@s.whatsapp.net"},
            expected_run_id=7,
        )

        assert result is False


def _chat(records=(), unread=0, t=0):
    c = {"unreadCount": unread, "t": t}
    if records:
        c["messages"] = {"messages": {"records": list(records)}}
    return c


def _records(n):
    return [{"key": {"id": f"M{i}"}} for i in range(n)]


class TestServerClaimsContent:
    def test_unread_count_counts(self):
        assert claims({"unreadCount": 3}) is True

    def test_last_activity_timestamp_counts(self):
        assert claims({"t": 1785147636}) is True

    def test_a_truly_blank_chat_does_not(self):
        assert claims({"unreadCount": 0, "t": 0}) is False

    @pytest.mark.parametrize("chat", [
        {}, {"unreadCount": None, "t": None}, {"unreadCount": "x", "t": "y"},
    ])
    def test_survives_missing_or_junk_values(self, chat):
        assert claims(chat) is False


class TestBackfillBookkeeping:
    def test_marks_a_chat_the_server_says_has_history(self):
        """The 514-chat case: API answered fine, gave nothing, but the chat
        record itself carries unread/activity."""
        s = _Stub()
        s._note_backfill_state("a@lid", _chat(unread=2, t=1785147636), api_ok=True)
        assert s._chats_awaiting_messages == {"a@lid"}

    def test_does_not_mark_a_genuinely_empty_chat(self):
        s = _Stub()
        s._note_backfill_state("a@lid", _chat(), api_ok=True)
        assert s._chats_awaiting_messages == set()

    def test_a_chat_left_with_nothing_stays_queued_when_the_api_never_answered(self):
        """This used to retire the chat, on the reasoning that a failed call
        belonged to "the retry loop". There is no such loop by this point:
        sync_chat_messages() has already exhausted its retries, and the failure
        returns normally rather than raising, so sync_remote_chats() does not
        see it either. The conversation was dropped from the queue with nothing
        coming back for it, and the sync still reported success.

        Bounded by the backfill loop's own pacing (offline check, chunking and
        30s→300s backoff), which is what keeps a re-queue from hammering an API
        that is down — the concern this test originally encoded."""
        s = _Stub()
        s._note_backfill_state("a@lid", _chat(unread=2), api_ok=False)
        assert s._chats_awaiting_messages == {"a@lid"}

    def test_a_chat_that_already_has_history_is_not_requeued_by_a_failure(self):
        """The other half of the rule: failing to REFRESH loses nothing, so a
        transient error must not put fully-synced chats back in the loop."""
        s = _Stub()
        s._note_backfill_state(
            "a@lid", _chat(records=_records(200), t=1), api_ok=False)
        assert s._chats_awaiting_messages == set()

    def test_clears_the_mark_once_history_arrives(self):
        s = _Stub()
        s._note_backfill_state("a@lid", _chat(unread=2, t=1), api_ok=True)
        assert "a@lid" in s._chats_awaiting_messages
        s._note_backfill_state("a@lid", _chat(records=[{"key": {"id": "X"}}], unread=2, t=1), api_ok=True)
        assert s._chats_awaiting_messages == set()

    def test_a_failed_retry_does_not_resurrect_a_recovered_chat(self):
        """Once a chat has history it must leave the pending set and stay out,
        even if a later call fails."""
        s = _Stub()
        s._chats_awaiting_messages = set()
        s._note_backfill_state("a@lid", _chat(records=[{"key": {"id": "X"}}], t=1), api_ok=True)
        s._note_backfill_state("a@lid", _chat(records=[{"key": {"id": "X"}}], t=1), api_ok=False)
        assert s._chats_awaiting_messages == set()

    def test_the_set_is_created_on_first_use(self):
        """sync_chat_messages() runs on worker threads before any explicit
        initialisation, so the first caller must not crash."""
        class _Bare:
            _server_claims_content = staticmethod(MainWindow._server_claims_content)
            _note_backfill_state = MainWindow._note_backfill_state
            _backfill_state_guard = MainWindow._backfill_state_guard
            _canonical_backfill_jid = MainWindow._canonical_backfill_jid
            _jid_address_forms = MainWindow._jid_address_forms
        b = _Bare()
        b._note_backfill_state("a@lid", _chat(unread=1), api_ok=True)
        assert b._chats_awaiting_messages == {"a@lid"}

    def test_tracks_many_chats_independently(self):
        s = _Stub()
        for i in range(514):
            s._note_backfill_state(f"c{i}@lid", _chat(t=1785147636), api_ok=True)
        assert len(s._chats_awaiting_messages) == 514
        # Half of them recover on the next pass.
        for i in range(0, 514, 2):
            s._note_backfill_state(f"c{i}@lid", _chat(records=[{"key": {"id": i}}], t=1), api_ok=True)
        assert len(s._chats_awaiting_messages) == 257


class TestShortOfAPage:
    """A chat can be short because it *is* short, or because it is early.

    While WhatsApp Web is still decoding history-sync chunks its store keeps
    growing for minutes after the sync ends — measured on this account, 1.2k to
    60k messages over about ten minutes. So a chat that answers with 15 messages
    during that window is not a 15-message conversation, and the sync should
    come back for it until it holds a full page (messages_page_size) or stops
    growing. With the chunk queue drained there is nothing to come back for:
    get-messages reads the same store the chunks feed.
    """

    def test_a_short_chat_is_retried_while_chunks_are_decoding(self):
        s = _Stub(page_size=200, chunks_pending=True)
        s._note_backfill_state("a@lid", _chat(records=_records(15), t=1), api_ok=True)
        assert s._chats_awaiting_messages == {"a@lid"}

    def test_a_full_page_is_done(self):
        s = _Stub(page_size=200, chunks_pending=True)
        s._note_backfill_state("a@lid", _chat(records=_records(200), t=1), api_ok=True)
        assert s._chats_awaiting_messages == set()

    def test_more_than_a_page_is_done(self):
        s = _Stub(page_size=200, chunks_pending=True)
        s._note_backfill_state("a@lid", _chat(records=_records(640), t=1), api_ok=True)
        assert s._chats_awaiting_messages == set()

    def test_a_short_chat_gets_one_background_confirmation_after_queue_drains(self):
        """Below the configured target is ambiguous until the phone answers."""
        s = _Stub(page_size=200, chunks_pending=False)
        s._note_backfill_state("a@lid", _chat(records=_records(15), t=1), api_ok=True)
        assert s._chats_awaiting_messages == {"a@lid"}

    def test_a_gap_chat_gets_one_confirming_retry_after_queue_drains(self):
        s = _Stub(page_size=200, chunks_pending=False)
        s._history_gap_jids = {"a@lid"}
        s._note_backfill_state("a@lid", _chat(records=_records(15), t=1), api_ok=True)
        assert s._chats_awaiting_messages == {"a@lid"}
        s._note_backfill_state("a@lid", _chat(records=_records(15), t=1), api_ok=True)
        assert s._chats_awaiting_messages == set()

    def test_a_chat_that_grew_keeps_its_slot_after_the_queue_drains(self):
        """The ramp has to finish: history landed for this one, so ask once more."""
        s = _Stub(page_size=200, chunks_pending=True)
        s._note_backfill_state("a@lid", _chat(records=_records(15), t=1), api_ok=True)
        s._history_still_landing = False
        s._note_backfill_state("a@lid", _chat(records=_records(90), t=1), api_ok=True)
        assert s._chats_awaiting_messages == {"a@lid"}

    def test_a_pass_that_adds_nothing_ends_the_retries(self):
        s = _Stub(page_size=200, chunks_pending=True)
        s._note_backfill_state("a@lid", _chat(records=_records(15), t=1), api_ok=True)
        s._history_still_landing = False
        s._note_backfill_state("a@lid", _chat(records=_records(15), t=1), api_ok=True)
        assert s._chats_awaiting_messages == set()

    def test_growth_bookkeeping_survives_the_lid_rekey(self):
        """deduplicate_chats() re-keys the chat between passes; the count must
        follow it, or every pass would look like the first one and never stop."""
        s = _Stub(page_size=200, chunks_pending=True)
        s._lid_to_phone = {"111@lid": "5511999999999@s.whatsapp.net"}
        s._phone_to_lid = {"5511999999999@s.whatsapp.net": "111@lid"}
        s._note_backfill_state("111@lid", _chat(records=_records(15), t=1), api_ok=True)
        s._history_still_landing = False
        s._note_backfill_state(
            "5511999999999@s.whatsapp.net", _chat(records=_records(15), t=1), api_ok=True)
        assert s._chats_awaiting_messages == set()
        assert s._partial_history_counts == {}

    def test_a_failed_call_still_belongs_to_the_retry_loop(self):
        s = _Stub(page_size=200, chunks_pending=True)
        s._note_backfill_state("a@lid", _chat(records=_records(15), t=1), api_ok=False)
        assert s._chats_awaiting_messages == set()

    def test_page_size_comes_from_settings(self):
        s = _Stub(page_size=50, chunks_pending=True)
        assert s.history_page_target() == 50
        s._note_backfill_state("a@lid", _chat(records=_records(50), t=1), api_ok=True)
        assert s._chats_awaiting_messages == set()

    @pytest.mark.parametrize("settings", [
        {}, {"user_interface": {}}, {"user_interface": {"messages_page_size": "junk"}},
        {"user_interface": {"messages_page_size": 0}},
    ])
    def test_page_target_survives_junk_settings(self, settings):
        s = _Stub()
        s.settings = settings
        assert s.history_page_target() >= 1


class TestJidBridge:
    """sync_remote_chats() records pending JIDs, and deduplicate_chats() then
    re-keys self.chats from @lid to phone JIDs. Looking a pending JID up
    verbatim afterwards misses.

    Measured live: pass 11 reported "retrying 7 of 267" — only 7 of 267 pending
    JIDs still existed under the name they were recorded under. The 260 that did
    not were every individual chat; only groups, which dedup never renames, were
    ever retried. That is why a real account sat frozen at 399 visible
    conversations while every pass reported progress.
    """

    def _stub(self):
        s = _Stub()
        s.chats = {}
        s._lid_to_phone = {"111@lid": "5511999999999@s.whatsapp.net"}
        s._phone_to_lid = {"5511999999999@s.whatsapp.net": "111@lid"}
        return s

    def test_finds_a_chat_that_was_rekeyed_to_its_phone_jid(self):
        s = self._stub()
        s.chats["5511999999999@s.whatsapp.net"] = {"remoteJid": "5511999999999@s.whatsapp.net"}
        key, chat = s._resolve_backfill_target("111@lid")
        assert key == "5511999999999@s.whatsapp.net"
        assert chat is not None

    def test_finds_a_chat_still_under_its_lid(self):
        s = self._stub()
        s.chats["111@lid"] = {"remoteJid": "111@lid"}
        key, _ = s._resolve_backfill_target("111@lid")
        assert key == "111@lid"

    def test_resolves_in_the_other_direction_too(self):
        s = self._stub()
        s.chats["111@lid"] = {"remoteJid": "111@lid"}
        key, _ = s._resolve_backfill_target("5511999999999@s.whatsapp.net")
        assert key == "111@lid"

    def test_reports_a_chat_that_is_really_gone(self):
        s = self._stub()
        key, chat = s._resolve_backfill_target("999@lid")
        assert (key, chat) == (None, None)

    def test_groups_resolve_unchanged(self):
        s = self._stub()
        s.chats["120363@g.us"] = {"remoteJid": "120363@g.us"}
        key, _ = s._resolve_backfill_target("120363@g.us")
        assert key == "120363@g.us"

    def test_recovery_clears_both_address_forms(self):
        """Marked under its @lid, re-synced under its phone JID — the @lid entry
        must not linger and be retried forever."""
        s = self._stub()
        s._chats_awaiting_messages = {"111@lid"}
        s._note_backfill_state(
            "5511999999999@s.whatsapp.net",
            _chat(records=[{"key": {"id": "X"}}], t=1), api_ok=True)
        assert s._chats_awaiting_messages == set()

    def test_snapshot_collapses_lid_and_phone_into_one_queue_entry(self):
        s = self._stub()
        phone = "5511999999999@s.whatsapp.net"
        s._chats_awaiting_messages = {"111@lid", phone}
        s._partial_history_counts = {"111@lid": 15, phone: 90}

        assert s._collapse_and_list_backfill_pending() == [phone]
        assert s._chats_awaiting_messages == {phone}
        assert s._partial_history_counts == {phone: 90}

    def test_parallel_workers_cannot_leave_both_address_forms_pending(self):
        s = _Stub(page_size=200, chunks_pending=True)
        s.chats = {}
        s._lid_to_phone = {"111@lid": "5511999999999@s.whatsapp.net"}
        s._phone_to_lid = {"5511999999999@s.whatsapp.net": "111@lid"}
        phone = "5511999999999@s.whatsapp.net"
        chat = _chat(records=_records(15), t=1)

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(s._note_backfill_state,
                                   "111@lid" if i % 2 == 0 else phone,
                                   chat, True)
                       for i in range(200)]
            for future in futures:
                future.result()

        assert s._collapse_and_list_backfill_pending() == [phone]

    def test_completed_count_ignores_unrelated_concurrent_arrivals(self):
        s = self._stub()
        s._chats_awaiting_messages = {"done@lid", "still@lid", "new@lid"}
        s._remove_backfill_pending("done@lid")

        assert s._completed_backfill_targets(["done@lid", "still@lid"]) == 1


class TestSweepCoverage:
    """The window advances by remembering what was tried, not by an index.

    `pending` shrinks as chats recover, so index arithmetic over it skipped
    entries outright — some chats were never retried at all.
    """

    @staticmethod
    def _sweep(pending, chunk, recover_per_pass):
        """Run the sweep rule, removing `recover_per_pass` chats each pass."""
        pending = list(pending)
        attempted, tried_ever, passes = set(), set(), 0
        while pending and passes < 200:
            untried = [j for j in pending if j not in attempted]
            if not untried:
                attempted.clear()
                untried = list(pending)
            window = untried[:chunk]
            attempted.update(window)
            tried_ever.update(window)
            for j in window[:recover_per_pass]:
                pending.remove(j)
            passes += 1
        return tried_ever, passes

    def test_every_chat_gets_tried_even_as_the_set_shrinks(self):
        original = [f"c{i}@lid" for i in range(463)]
        tried, _ = self._sweep(original, chunk=60, recover_per_pass=17)
        assert tried == set(original), "no chat may be skipped by the sweep"

    def test_it_terminates_when_nothing_ever_recovers(self):
        original = [f"c{i}@lid" for i in range(120)]
        tried, passes = self._sweep(original, chunk=60, recover_per_pass=0)
        assert tried == set(original)
        assert passes == 200, "loop is bounded by the caller's budget, not by progress"


class TestBackfillPacing:
    """The loop is adaptive: quick retries while passes recover chats, backing
    off when one recovers nothing, and a hard overall budget.

    A fixed schedule was tried first and abandoned one real account's remaining
    260 chats while their history was still arriving — one pass had just
    recovered 203 of 463.
    """

    def test_constants_are_sane(self):
        assert MainWindow._BACKFILL_FIRST_DELAY <= 60, "first retry must be soon"
        assert 0 < MainWindow._BACKFILL_CHUNK_DELAY < MainWindow._BACKFILL_FIRST_DELAY
        assert MainWindow._BACKFILL_MAX_DELAY >= MainWindow._BACKFILL_FIRST_DELAY
        assert MainWindow._BACKFILL_BUDGET >= 15 * 60, "must outlast a slow warm-up"
        assert MainWindow._BACKFILL_BUDGET <= 2 * 60 * 60, "must not poll forever"

    def test_short_page_confirmation_starts_without_initial_delay(self):
        assert MainWindow._initial_backfill_delay(short_chats_pending=True) == 0

    def test_background_only_work_keeps_the_initial_delay(self):
        assert MainWindow._initial_backfill_delay(
            short_chats_pending=False) == MainWindow._BACKFILL_FIRST_DELAY

    def test_short_pages_block_names_and_deep_history(self):
        assert MainWindow._background_backfill_work_allowed(True, False) is False

    def test_partial_short_sweep_still_blocks_background_work(self):
        assert MainWindow._background_backfill_work_allowed(False, True) is False

    def test_background_work_starts_after_short_pages_finish(self):
        assert MainWindow._background_backfill_work_allowed(False, False) is True

    @staticmethod
    def _next_delay(delay, recovered):
        """The pacing rule the loop applies after each pass."""
        if recovered > 0:
            return MainWindow._BACKFILL_FIRST_DELAY
        return min(delay * 2, MainWindow._BACKFILL_MAX_DELAY)

    def test_progress_keeps_retries_fast(self):
        d = self._next_delay(240, recovered=203)
        assert d == MainWindow._BACKFILL_FIRST_DELAY

    def test_no_progress_backs_off(self):
        first = MainWindow._BACKFILL_FIRST_DELAY
        assert self._next_delay(first, 0) == first * 2

    def test_backoff_is_capped(self):
        d = MainWindow._BACKFILL_FIRST_DELAY
        for _ in range(20):
            d = self._next_delay(d, 0)
        assert d == MainWindow._BACKFILL_MAX_DELAY

    def test_each_pass_is_a_bounded_chunk(self):
        """An unchunked pass fired 463 get-messages calls in ~6 s through the
        single Puppeteer page, on top of the media phase. This is background
        history nobody is waiting on — it must not burst."""
        assert MainWindow._BACKFILL_CHUNK <= 100
        assert MainWindow._BACKFILL_WORKERS <= 6, "must not exceed the sync's own cap"

    def test_large_first_sweep_does_not_pay_the_retry_delay_per_chunk(self):
        pending = 896
        chunks = -(-pending // MainWindow._BACKFILL_CHUNK)
        old_wait = chunks * MainWindow._BACKFILL_FIRST_DELAY
        new_wait = (MainWindow._BACKFILL_FIRST_DELAY
                    + (chunks - 1) * MainWindow._BACKFILL_CHUNK_DELAY)

        assert new_wait < old_wait / 2

    def test_partial_sweep_keeps_backoff_and_uses_chunk_delay(self):
        sleep, retry = MainWindow._backfill_short_queue_delays(
            retry_delay=120, sweep_finished=False, sweep_made_progress=False)

        assert sleep == MainWindow._BACKFILL_CHUNK_DELAY
        assert retry == 120

    def test_finished_sweep_applies_backoff_once(self):
        sleep, retry = MainWindow._backfill_short_queue_delays(
            retry_delay=120, sweep_finished=True, sweep_made_progress=False)

        assert sleep == retry == 240

    def test_finished_sweep_with_progress_resets_backoff(self):
        sleep, retry = MainWindow._backfill_short_queue_delays(
            retry_delay=240, sweep_finished=True, sweep_made_progress=True)

        assert sleep == retry == MainWindow._BACKFILL_FIRST_DELAY

    def test_the_window_rotates_so_every_chat_gets_tried(self):
        """`pending` is sorted, so slicing from the front every pass would retry
        the same chats forever and never reach the rest."""
        pending = [f"c{i}@lid" for i in range(463)]
        chunk = MainWindow._BACKFILL_CHUNK
        cursor, seen, passes = 0, set(), 0
        while len(seen) < len(pending) and passes < 100:
            cursor %= len(pending)
            ordered = pending[cursor:] + pending[:cursor]
            cursor += chunk
            seen.update(ordered[:chunk])
            passes += 1
        assert seen == set(pending), "rotation must cover every pending chat"
        assert passes == -(-len(pending) // chunk), "and cover it without wasted passes"

    def test_a_stuck_account_cannot_burn_the_budget_in_a_few_passes(self):
        """Backing off means an account that never warms up costs few passes."""
        d, spent, passes = MainWindow._BACKFILL_FIRST_DELAY, 0, 0
        while spent < MainWindow._BACKFILL_BUDGET:
            spent += d
            passes += 1
            d = self._next_delay(d, 0)
        assert passes <= 15, f"{passes} passes for a store that never warms up"

    def test_a_full_chat_does_not_stay_in_the_re_query_list(self):
        s = _Stub(page_size=200, chunks_pending=True)
        s._note_backfill_state("a@lid", _chat(records=_records(640), t=1), api_ok=True)
        assert s._chats_awaiting_messages == set()

    def test_a_short_chat_is_taken_at_face_value_once_the_queue_is_drained(self):
        """Nothing left to decode means a re-query returns the identical list."""
        s = _Stub(page_size=200, chunks_pending=False)
        s._note_backfill_state("a@lid", _chat(records=_records(15), t=1), api_ok=True)
        assert s._chats_awaiting_messages == {"a@lid"}
        s._note_backfill_state("a@lid", _chat(records=_records(15), t=1), api_ok=True)
        assert s._chats_awaiting_messages == set()

    def test_a_chat_that_grew_keeps_its_slot_after_the_queue_drains(self):
        """The ramp has to finish: history landed for this one, so ask once more."""
        s = _Stub(page_size=200, chunks_pending=True)
        s._note_backfill_state("a@lid", _chat(records=_records(15), t=1), api_ok=True)
        s._history_still_landing = False
        s._note_backfill_state("a@lid", _chat(records=_records(90), t=1), api_ok=True)
        assert s._chats_awaiting_messages == {"a@lid"}

    def test_a_pass_that_adds_nothing_ends_the_retries(self):
        s = _Stub(page_size=200, chunks_pending=True)
        s._note_backfill_state("a@lid", _chat(records=_records(15), t=1), api_ok=True)
        s._history_still_landing = False
        s._note_backfill_state("a@lid", _chat(records=_records(15), t=1), api_ok=True)
        assert s._chats_awaiting_messages == set()

    def test_growth_bookkeeping_survives_the_lid_rekey(self):
        """deduplicate_chats() re-keys the chat between passes; the count must
        follow it, or every pass would look like the first one and never stop."""
        s = _Stub(page_size=200, chunks_pending=True)
        s._lid_to_phone = {"111@lid": "5511999999999@s.whatsapp.net"}
        s._phone_to_lid = {"5511999999999@s.whatsapp.net": "111@lid"}
        s._note_backfill_state("111@lid", _chat(records=_records(15), t=1), api_ok=True)
        s._history_still_landing = False
        s._note_backfill_state(
            "5511999999999@s.whatsapp.net", _chat(records=_records(15), t=1), api_ok=True)
        assert s._chats_awaiting_messages == set()
        assert s._partial_history_counts == {}

    def test_a_failed_call_still_belongs_to_the_retry_loop(self):
        s = _Stub(page_size=200, chunks_pending=True)
        s._note_backfill_state("a@lid", _chat(records=_records(15), t=1), api_ok=False)
        assert s._chats_awaiting_messages == set()


class TestChatsBlockingBackgroundWork:
    """Issue #747: unopened empty chats must not starve deep history."""

    def test_unopened_chat_already_tried_does_not_block(self):
        out = MainWindow._chats_blocking_background_work(
            ["a", "b"], {"a", "b"}, lambda j: False)
        assert out == []

    def test_untried_chat_still_blocks(self):
        out = MainWindow._chats_blocking_background_work(
            ["a", "b"], {"a"}, lambda j: False)
        assert out == ["b"]

    def test_opened_chat_still_blocks(self):
        out = MainWindow._chats_blocking_background_work(
            ["a"], {"a"}, lambda j: True)
        assert out == ["a"]
