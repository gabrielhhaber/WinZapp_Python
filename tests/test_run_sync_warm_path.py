"""The warm half of _run_sync()'s history gate — the branch almost every
launch actually takes, and the one nothing exercised.

The gate has two sides. The cold one (_force_full_sync=True: first pairing, F5,
an empty local cache) unblocks WhatsApp Web's history queue, waits for the
RECENT transfer and re-reads whether history is still landing; that side is
covered by tests/test_run_sync_history_gate.py, which sets _force_full_sync
True in its own stub precisely so it keeps reaching it. The warm one skips all
of that and asserts _history_still_landing = False.

Nothing was left watching the warm side. A regression there does not raise and
does not fail a round: it declares the synchronization finished while history
is still arriving, and the user sees conversations missing history and unread
badges that are simply wrong — the exact failure the gate was written for, and
one that only shows up in real use. So what these pin down is not that the warm
path runs, but that skipping the unblock costs it nothing: a chat that comes
back short is still repaired, the backfill still starts, and the completion
marker still waits for the message delta.

Writing them turned up one place where skipping the gate did cost the warm
round something, and it is fixed in the same change: the warm branch used to
*assert* _history_still_landing = False instead of reading it.
_note_backfill_state()'s short-page rule is
`still_landing or grew or first_short_page`, and a chat restored from
backfill_pending_v1 has first_short_page False — it was seen last session — so
one that answered short without growing had all three terms False and was
retired from the repair queue while WhatsApp Web was still decoding it. The
warm branch now reads the flag (one GET) while still skipping what is actually
expensive: the RECENT restart and the ten-minute wait.
TestARestoredShortChatIsNotRetiredEarly is that regression, both directions.

The stub harness is tests/test_run_sync_broken_store.py's, imported rather than
copied for the reason that module's own docstring gives.
"""

import types

import pytest

import main
from main import MainWindow
from tests.conftest import warm_cached_chat as _chat
from tests.test_run_sync_broken_store import _fast, _make  # noqa: F401  (_fast is an autouse fixture)


_JIDS = [f"55119{i:08d}@s.whatsapp.net" for i in range(3)]


def _instrumented(stub):
    """Counters for everything the gate and its downstream decisions touch."""
    stub.unblocks = 0
    stub.waits = 0
    stub.landing_contexts = []
    stub.landing_at_message_phase = None
    stub.message_rounds = []
    stub.media_scopes = []
    stub.saves = 0
    stub.statuses = []
    stub.spoken = []
    stub.full_latches = []
    stub.failing_jids = set()
    # What /history-sync-status would answer this round. None means the call
    # could not be read at all — an older client/api/ has no such route.
    stub.status_landing = False

    def _unblock(timeout=60):
        stub.unblocks += 1
        # A result that demands a wait: if the warm path ever consulted the
        # gate, both counters below would move instead of staying at zero.
        return {"restarted": True, "recentCompleted": False}

    def _wait(timeout=600):
        stub.waits += 1
        stub._history_wait_outcome = "completed"
        return True

    def _refresh(context=""):
        # Mirrors the real method's contract: it *writes* the flag from the
        # status endpoint and returns it. A stub that only returned a value
        # would let a caller that asserts the flag instead of reading it pass.
        stub.landing_contexts.append(context)
        if stub.status_landing is None:
            # Unreadable status, session still alive — the real method
            # preserves the previous value and defaults to True when there is
            # none (main.py: getattr(self, "_history_still_landing", True)).
            landing = bool(getattr(stub, "_history_still_landing", True))
        else:
            landing = stub.status_landing
        stub._history_still_landing = landing
        return landing

    def _sync_remote(target_chats=None, incremental=False):
        stub.message_sync_ran += 1
        # Captured here rather than after the run: refresh_history_still_landing()
        # re-reads the flag from the API later on, so the value the message
        # phase actually saw is only observable from inside it.
        stub.landing_at_message_phase = stub._history_still_landing
        stub.message_rounds.append((
            incremental,
            sorted(c.get("remoteJid") for c in (target_chats or [])),
        ))
        return set(stub.failing_jids)

    def _media(jids=None):
        stub.media_sync_ran += 1
        stub.media_scopes.append(None if jids is None else set(jids))
        return 0

    stub._recent_history_needs_wait = MainWindow._recent_history_needs_wait
    stub._history_wait_outcome = ""
    stub.unblock_history_sync = _unblock
    stub.wait_for_restarted_history_sync = _wait
    stub.refresh_history_still_landing = _refresh
    stub.sync_remote_chats = _sync_remote
    stub.sync_media_for_all_chats = _media
    stub._schedule_save = lambda **kw: setattr(stub, "saves", stub.saves + 1)
    stub._set_status = lambda text: stub.statuses.append(text)
    stub.output = lambda text, **kw: stub.spoken.append(text)
    stub._persist_full_sync_pending = lambda reason: stub.full_latches.append(reason)
    return stub


def _warm_stub():
    """A settled reconnection with a warm cache — the ordinary launch."""
    stub = _make([len(_JIDS)] * 2, wa_web=len(_JIDS), local_chats=len(_JIDS))
    stub.chats = {jid: _chat(jid) for jid in _JIDS}
    stub._force_full_sync = False
    return _instrumented(stub)


def _cold_stub():
    """Same account, but the round was latched full (F5 / first pairing)."""
    stub = _warm_stub()
    stub._force_full_sync = True
    return stub


def _empty_cache_stub():
    """First launch after pairing: nothing cached locally, the flag off.

    The chats only exist on the server, so they arrive through list-chats —
    which is what makes this the boundary case: the cache is empty at the
    moment the mode is chosen and full by the time the plan is built.
    """
    stub = _make([2, 2], wa_web=2, local_chats=0)
    stub.chats = {}
    stub._force_full_sync = False
    stub = _instrumented(stub)
    _server_returns(stub, {jid: _chat(jid) for jid in _JIDS[:2]})
    return stub


def _never_read(stub, name):
    """Make *name* genuinely absent, the way it is in a fresh process.

    _Stub answers every unknown attribute with a lambda, so `del` alone leaves
    hasattr() True — and the fallback under test is keyed on exactly that
    question. MainWindow has no __getattr__, so there hasattr() is a real one.
    """
    stub.__dict__.pop(name, None)
    stub.__dict__.setdefault("_absent", set()).add(name)


def _server_returns(stub, extra):
    """Make list-chats answer with *extra* merged over what it was handed."""
    inner = stub.get_remote_chats

    def _fetch(chats, **kwargs):
        merged = inner(chats, **kwargs)
        for jid, chat in extra.items():
            merged[jid] = dict(chat)
        return merged

    stub.get_remote_chats = _fetch


def _server_bumps_activity(stub, jid, new_t=500):
    """The one thing a warm round reacts to: a chat whose marker moved."""
    bumped = dict(stub.chats[jid])
    bumped["t"] = new_t
    _server_returns(stub, {jid: bumped})


class TestTheGateIsNotConsultedAtAll:
    def test_a_warm_round_never_unblocks_or_waits_for_recent_history(self):
        stub = _warm_stub()
        stub._run_sync()
        assert (stub.unblocks, stub.waits) == (0, 0)
        assert stub._sync_completed is True

    def test_the_cold_round_still_does(self):
        """The negative above is only worth anything against this."""
        stub = _cold_stub()
        stub._run_sync()
        assert (stub.unblocks, stub.waits) == (1, 1)

    def test_the_warm_round_reads_the_landing_flag_instead_of_asserting_it(self):
        """It used to assign False here without asking anything.

        One GET is the whole difference: with the phone still pushing history,
        a warm round that assumed otherwise retired chats from the repair queue
        that were merely early — see TestARestoredShortChatIsNotRetiredEarly.
        """
        stub = _warm_stub()
        stub.status_landing = True
        _server_bumps_activity(stub, _JIDS[0])
        stub._run_sync()
        assert stub.landing_at_message_phase is True

    def test_and_believes_it_when_the_answer_is_that_nothing_is_landing(self):
        """The common case, and the one the old assignment happened to get
        right — which is why the bug survived."""
        stub = _warm_stub()
        stub.status_landing = False
        _server_bumps_activity(stub, _JIDS[0])
        stub._run_sync()
        assert stub.landing_at_message_phase is False

    def test_both_rounds_read_the_flag_and_stay_apart_in_the_log(self):
        """Both paths make the status call, and the log has to keep saying
        which one did. [history-sync] lines are the first thing read when a
        user reports missing history, and a warm round is a different diagnosis
        from a cold one — CLAUDE.md is explicit that these breadcrumbs are the
        tool, so a shared context string would cost a real investigation."""
        warm = _warm_stub()
        warm._run_sync()
        assert warm.landing_contexts[0] == "before warm message sync"

        cold = _cold_stub()
        cold._run_sync()
        assert cold.landing_contexts[0] == "before message sync"

        # Unchanged on both: the read after the sync is what decides whether
        # the backfill thread is worth starting.
        assert "after initial sync" in warm.landing_contexts
        assert "after initial sync" in cold.landing_contexts


class TestTheWarmFallbackWhenTheStatusCannotBeRead:
    """What the warm round does when there is no answer to read.

    /history-sync-status is one of WinZapp's own api_patches routes, so an
    installation whose client/api/ predates it gets None from every call. The
    real refresh_history_still_landing() answers that by preserving the last
    value it knew and defaulting to True, which is the right instinct for a
    fresh pairing and the wrong one here: nothing would ever bring it back
    down, and phase 2 media auto-download is skipped for as long as it is set.

    So the warm branch seeds False first, and only when the flag has never been
    read — which is both what this branch assumed before it read anything, and
    what every reader of the flag already assumes when it is missing.
    """

    def test_an_api_without_the_route_does_not_latch_history_as_landing(self):
        stub = _warm_stub()
        stub.settings["storage"]["auto_download_media"] = True
        # A fresh process: nothing has ever set the flag. _Stub seeds it for
        # the other tests' convenience, so undo that to reproduce the real
        # precondition — it is the absence the fallback turns on.
        _never_read(stub, "_history_still_landing")
        stub.status_landing = None
        _server_bumps_activity(stub, _JIDS[0])

        stub._run_sync()

        assert stub._history_still_landing is False

    def test_and_the_media_phase_still_runs(self):
        """The consequence, not just the flag: a latched True defers phase 2
        (main.py's `elif getattr(self, "_history_still_landing", False)`), and
        _start_deferred_media_sync() only fires from the backfill loop once a
        later read comes back False — which on this build it never does."""
        stub = _warm_stub()
        stub.settings["storage"]["auto_download_media"] = True
        _never_read(stub, "_history_still_landing")
        stub.status_landing = None
        _server_bumps_activity(stub, _JIDS[0])

        stub._run_sync()

        assert stub.media_scopes == [{_JIDS[0]}]
        # __dict__ and not getattr(): the stub answers every unknown attribute
        # with a lambda, which is truthy, so a default would never be reached.
        assert "_media_sync_deferred" not in stub.__dict__

    def test_the_cold_round_keeps_the_conservative_default(self):
        """The seed belongs to the warm branch, and only to it.

        Moving it out of the `else` — one dedent, and the obvious tidy-up for
        anyone reorganising this hunk — would hand the cold path False too. On
        a build without the route that is the difference between a first
        pairing keeping its short chats queued while the phone decodes, and
        _note_backfill_state() retiring every one of them on the first pass
        minutes after pairing. Which is the same class of bug the warm fix
        exists to remove, introduced at the other end.
        """
        stub = _cold_stub()
        _never_read(stub, "_history_still_landing")
        stub.status_landing = None

        stub._run_sync()

        assert stub._history_still_landing is True

    def test_a_value_already_read_this_session_is_not_thrown_away(self):
        """The half that makes the seed conditional. A cold round earlier in
        this session (or a warm one while the route was answering) learned that
        history is still arriving; a status that goes briefly unreadable is
        exactly when carrying that over is worth most, so a blind False every
        round would discard it at the worst possible moment."""
        stub = _warm_stub()
        stub._history_still_landing = True
        stub.status_landing = None

        stub._run_sync()

        assert stub._history_still_landing is True


class TestSkippingTheUnblockCostsTheRoundNothing:
    """The heart of the issue: everything downstream of the gate reads either
    _history_still_landing or the plan built after it. If the warm round could
    only be safe *because* the unblock ran, skipping it would silently accept
    half-landed history as final."""

    def test_a_chat_left_short_by_an_earlier_round_is_still_repaired(self):
        """_chats_awaiting_messages is the durable record of "this chat owes us
        history". _plan_message_sync() reads it as repair_needed and demands a
        FULL page — which is what makes a short chat provisional on the warm
        path too, with no unblock and no wait involved."""
        stub = _warm_stub()
        stub._chats_awaiting_messages = {_JIDS[1]}
        stub._run_sync()

        full_rounds = [jids for incremental, jids in stub.message_rounds if not incremental]
        assert full_rounds == [[_JIDS[1]]], (
            "a chat still owing history was accepted as complete by the warm round"
        )

    def test_the_backfill_thread_still_starts_for_it(self):
        """Repairing it once is not enough — the queue is what comes back for
        the chat as the rest of its history lands, and on the warm path the
        'still landing' reason for starting it is switched off by design."""
        stub = _warm_stub()
        stub._chats_awaiting_messages = {_JIDS[1]}
        stub._run_sync()
        assert stub.backfill_started is True

    def _queue_reader(self, landing=False):
        """A stub with the real short-page rule bound, at the flag a warm round
        would have left False."""
        stub = _warm_stub()
        stub._history_still_landing = landing
        stub._note_backfill_state = types.MethodType(
            MainWindow.__dict__["_note_backfill_state"], stub)
        stub._server_claims_content = MainWindow._server_claims_content
        stub.history_page_target = lambda: 200
        return stub

    def test_a_short_chat_seen_now_is_queued_without_the_landing_flag(self):
        """The consumer side of the same invariant, at the level that decides
        it. _note_backfill_state() queues a short first page whether or not
        history is still landing — see
        tests/test_message_backfill.py::TestShortOfAPage — so a warm round that
        never learned the flag cannot mistake "early" for "finished"."""
        stub = self._queue_reader()

        stub._note_backfill_state(_JIDS[2], _chat(_JIDS[2], records=15), api_ok=True)

        assert _JIDS[2] in stub._chats_awaiting_messages

    def test_a_chat_that_has_told_us_everything_it_has_is_still_retired(self):
        """Termination, at the same level. The queue has to drain: a chat kept
        in it merely for being in it is read by _plan_message_sync() as
        repair_needed, i.e. a FULL re-sync of that conversation on every round
        of every session — the gap branch of _note_backfill_state() carries the
        long version of that argument. Nothing landing, nothing gained, so it
        goes."""
        stub = self._queue_reader(landing=False)
        stub._chats_awaiting_messages = {_JIDS[2]}
        stub._partial_history_counts = {_JIDS[2]: 15}

        stub._note_backfill_state(_JIDS[2], _chat(_JIDS[2], records=15), api_ok=True)

        assert _JIDS[2] not in stub._chats_awaiting_messages


class TestTheWarmRoundStillDoesItsWork:
    def test_a_chat_whose_activity_moved_gets_an_incremental_delta(self):
        stub = _warm_stub()
        _server_bumps_activity(stub, _JIDS[0])
        stub._run_sync()
        assert stub.message_rounds == [(True, [_JIDS[0]])]

    def test_the_baseline_is_the_cache_as_it_was_before_list_chats_merged(self):
        """list-chats overwrites `t`/lastReceivedKey on the very dicts the plan
        compares against. Capturing the baseline after that merge is not a
        smaller optimisation — it makes every chat look unchanged, so no delta
        is ever fetched and the round above would sync nothing."""
        stub = _warm_stub()
        _server_bumps_activity(stub, _JIDS[0])
        stub._run_sync()
        assert stub.message_sync_ran == 1
        assert stub.chats[_JIDS[0]]["t"] == 500

    def test_an_unchanged_account_asks_for_no_messages_at_all(self):
        stub = _warm_stub()
        stub._run_sync()
        assert stub.message_rounds == []
        assert stub._sync_completed is True

    def test_the_completion_marker_waits_for_the_delta_to_succeed(self):
        stub = _warm_stub()
        _server_bumps_activity(stub, _JIDS[0])
        stub.failing_jids = {_JIDS[0]}
        stub._run_sync()
        assert stub._sync_completed is False
        assert stub.saves == 0, "committed the chat-list snapshot over a failed delta"

    def test_the_cold_round_defers_its_snapshot_too(self):
        """The cold path makes one chat-list fetch the warm path never does —
        the refresh after the RECENT wait — and it has the same rule: nothing
        list-chats learns reaches disk until the message phase has succeeded."""
        stub = _cold_stub()
        stub.failing_jids = {_JIDS[0]}
        stub._run_sync()
        assert stub._sync_completed is False
        assert stub.saves == 0

    def test_a_successful_delta_commits_the_deferred_snapshot(self):
        stub = _warm_stub()
        _server_bumps_activity(stub, _JIDS[0])
        stub._run_sync()
        assert stub._sync_completed is True
        assert stub.saves == 1

    def test_the_media_scan_is_scoped_to_the_chats_that_changed(self):
        stub = _warm_stub()
        stub.settings["storage"]["auto_download_media"] = True
        _server_bumps_activity(stub, _JIDS[0])
        stub._run_sync()
        assert stub.media_scopes == [{_JIDS[0]}]

    def test_a_warm_round_with_no_changes_scans_no_media(self):
        stub = _warm_stub()
        stub.settings["storage"]["auto_download_media"] = True
        stub._run_sync()
        assert stub.media_sync_ran == 0

    def test_the_cold_round_scans_everything(self):
        stub = _cold_stub()
        stub.settings["storage"]["auto_download_media"] = True
        stub._run_sync()
        assert stub.media_scopes == [None]


class TestTheAnnouncementIsTheQuietOne:
    """A warm refresh happens on essentially every launch. Announcing it as a
    full "Sincronizando ... Sincronização concluída" is what made the client
    talk over the user every time it reconnected, so the two paths deliberately
    use different strings — a distinction that only exists at runtime, since
    both call the same i18n lookup."""

    @pytest.fixture(autouse=True)
    def _run_call_after_inline(self, monkeypatch):
        # The announcements live in closures handed to wx.CallAfter, so the
        # suite-wide no-op stub hides them completely.
        monkeypatch.setattr(main.wx, "CallAfter",
                            lambda fn, *a, **kw: fn(*a, **kw))

    def test_the_warm_round_says_it_is_updating_conversations(self):
        stub = _warm_stub()
        stub.background_mode = False
        stub._run_sync()
        assert stub.statuses[0] == "updating_conversations"
        assert "conversations_update_started" in stub.spoken
        assert "synchronization_started" not in stub.spoken

    def test_the_warm_round_ends_on_the_matching_string(self):
        stub = _warm_stub()
        stub.background_mode = False
        stub._run_sync()
        assert "conversations_update_complete" in stub.spoken
        assert "sync_complete" not in stub.spoken

    def test_the_cold_round_keeps_the_full_synchronization_wording(self):
        stub = _cold_stub()
        stub.background_mode = False
        stub._run_sync()
        assert stub.statuses[0] == "synchronizing"
        assert "synchronization_started" in stub.spoken
        assert "sync_complete" in stub.spoken


class TestWarmIsNeverChosenWithoutACache:
    """The boundary between the two paths. It exists only as three lines at the
    top of _run_sync(), and getting it wrong is invisible: an empty cache
    running the warm plan finds nothing changed against an empty baseline, syncs
    no messages, and reports success over an empty account."""

    def test_an_empty_local_cache_forces_the_full_path_anyway(self):
        stub = _empty_cache_stub()
        stub._run_sync()
        assert stub.unblocks == 1, "chose the warm path with nothing cached"
        assert stub.landing_contexts[0] == "before message sync"

    def test_the_latch_records_why_so_it_survives_a_failed_round(self):
        stub = _empty_cache_stub()
        stub._run_sync()
        assert stub.full_latches == ["empty-local-cache"]

    def test_every_chat_the_server_returns_is_fetched_in_full(self):
        stub = _empty_cache_stub()
        stub._run_sync()
        assert stub.message_rounds == [(False, sorted(_JIDS[:2]))]

    def test_a_broken_store_promotes_a_warm_round_to_a_full_one(self):
        """The other way a warm round becomes full, and the only one that is
        not the user's decision: list-chats stopped answering, so absent
        activity markers prove nothing and every known chat has to be queried.
        get-messages still works — it reads IndexedDB, not the chat store."""
        stub = _make([0] * 60, wa_web=937, local_chats=len(_JIDS), high_water=935)
        stub.chats = {jid: _chat(jid) for jid in _JIDS}
        stub._force_full_sync = False
        _instrumented(stub)
        stub._run_sync()

        assert stub._broken_store_rounds == 1
        assert stub.message_rounds == [(False, sorted(_JIDS))]
        assert stub.unblocks == 0, "a broken store is not a reason to restart history sync"


class TestARestoredShortChatIsNotRetiredEarly:
    """The regression the warm gate carried, end to end.

    prepare_sync() restores backfill_pending_v1 into BOTH
    _chats_awaiting_messages and _partial_history_counts, so a chat carried
    over from the previous session has a previous count — `grew` and
    `first_short_page` are both False for it. That left
    _note_backfill_state()'s short-page rule resting entirely on
    _history_still_landing, which the warm branch used to assert rather than
    read. The chat was dropped from the repair queue, and nothing puts it back:
    _backfill_empty_chats() only re-reads the queue it was just removed from.

    Driven through the real _run_sync() rather than by calling
    _note_backfill_state() directly, because the wiring between the gate and
    the queue is the part that was wrong — the rule itself was always right.
    """

    def _round(self, landing, answered_records=15):
        stub = _warm_stub()
        stub.status_landing = landing
        # The state a restart restores: queued last session, 15 records deep.
        stub._chats_awaiting_messages = {_JIDS[1]}
        stub._partial_history_counts = {_JIDS[1]: 15}
        stub._note_backfill_state = types.MethodType(
            MainWindow.__dict__["_note_backfill_state"], stub)
        stub._server_claims_content = MainWindow._server_claims_content
        stub.history_page_target = lambda: 200

        inner = stub.sync_remote_chats

        def _sync_and_report(target_chats=None, incremental=False):
            failures = inner(target_chats, incremental)
            # What the real sync_chat_messages() does at the end of every chat:
            # hand the answer it got to the queue bookkeeping.
            for chat in target_chats or []:
                stub._note_backfill_state(
                    chat["remoteJid"],
                    _chat(chat["remoteJid"], records=answered_records),
                    api_ok=True,
                )
            return failures

        stub.sync_remote_chats = _sync_and_report
        stub._run_sync()
        return stub

    def test_it_stays_queued_while_the_phone_is_still_pushing_history(self):
        stub = self._round(landing=True)
        assert stub._chats_awaiting_messages == {_JIDS[1]}, (
            "a chat whose history is still arriving was retired by a warm round"
        )

    def test_the_round_still_repaired_it_this_time_too(self):
        """The queue slot is about the *next* attempt; this one had to happen
        as well, or the fix would just be bookkeeping."""
        stub = self._round(landing=True)
        assert stub.message_rounds == [(False, [_JIDS[1]])]

    def test_it_is_retired_once_history_has_finished_landing(self):
        """The other half, and the reason not to simply keep pending chats
        pending: with nothing left to decode, a pass that adds nothing is the
        answer — otherwise that chat is re-synced in full forever."""
        stub = self._round(landing=False)
        assert stub._chats_awaiting_messages == set()

    def test_a_chat_that_grew_keeps_its_slot_either_way(self):
        """Unchanged by the fix: growth is its own reason to come back, and it
        is what lets the ramp finish instead of stopping one pass short."""
        stub = self._round(landing=False, answered_records=90)
        assert stub._chats_awaiting_messages == {_JIDS[1]}

    def test_the_round_completes_with_the_chat_still_queued(self):
        """Documents a cost, and does not claim it is free.

        The repair queue deliberately does not block completion, so a session
        whose RECENT pass was interrupted — where `recentCompleted` stays false
        for ever and every warm round therefore reads landing=True — finishes
        each round with the queue still full. It is persisted to
        backfill_pending_v1 from there, and the next launch plans every one of
        those chats as a FULL page (test_repair_state_durability.py's
        test_a_restored_short_history_queue_forces_a_full_repair), which is the
        incremental round's whole saving spent. It terminates via the in-page
        recentCompleted repair that request_older_messages() triggers, in
        another module and after this phase — see _run_sync()'s own comment on
        the read for why narrowing the signal here was refused instead.
        """
        stub = self._round(landing=True)
        assert stub._sync_completed is True
        assert stub._chats_awaiting_messages == {_JIDS[1]}

    def test_the_fix_did_not_reintroduce_the_recent_restart_or_wait(self):
        """The line between reading the status and undoing PR #138. If this
        goes red the fix went too far: unblock_history_sync() restarts the
        phone's RECENT pass and the wait blocks for up to ten minutes."""
        stub = self._round(landing=True)
        assert (stub.unblocks, stub.waits) == (0, 0)
