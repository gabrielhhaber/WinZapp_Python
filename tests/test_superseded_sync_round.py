"""A sync round superseded mid-flight stops, and writes nothing on the way out.

Issue #198. When QR pairing links a different phone,
_wipe_local_data_if_another_number_linked() empties the account while the
round started by the pairing event is still running, and clear_local_data()
bumps _sync_run_id to mark that round stale. _run_sync() only read the counter
at its very last commit, so the stale round ran to its natural end — minutes —
and every self.chats assignment and set_chats() in it put the PREVIOUS
account's conversations back on screen. A screen-reader user heard "as
conversas do número anterior foram apagadas", then heard the list fill up with
those conversations again, then heard it empty a second time when
_restart_sync_after_another_number_wipe() repeated the wipe, with nothing said.

Three things a fix must not break, each pinned below:

  * a superseded round must write NEITHER outcome. True would undo the wipe's
    own _sync_completed=False, and a False or a set of failed chats would feed
    "Sync completion — the trap that keeps being rediscovered" (CLAUDE.md):
    one chat in the durable retry list holds the next round "not synced";
  * start_sync()'s finally must still clear _initial_sync_running, because the
    join loop in _restart_sync_after_another_number_wipe() waits on it;
  * start_sync() bumps _sync_run_id itself before calling _run_sync(), so an
    unsuperseded round must never read that bump as its own supersession.

Each guard in _run_sync() has its own test here, placed where only that guard
can catch the wipe, so removing any one of them turns exactly that test red.

The _run_sync() harness is tests/test_run_sync_broken_store.py's, imported
rather than copied for the reason that module's own docstring gives.
"""

import threading
import types

import pytest

import main
from main import MainWindow
from tests.test_run_sync_broken_store import _fast, _make  # noqa: F401  (_fast is an autouse fixture)

# Captured at import, before any fixture has replaced it.
_REAL_THREAD = threading.Thread


@pytest.fixture(autouse=True)
def _inline_call_after(_fast, monkeypatch):
    # set_chats() and the announcements reach the UI through wx.CallAfter,
    # which _fast turns into a no-op. Running them inline is what makes "the
    # list was shown again after the wipe" observable at all. Depends on _fast
    # so this patch is applied after it, not overwritten by it.
    monkeypatch.setattr(main.wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))


_TRACKED = ("get_remote_contacts", "get_block_list",
            "_resolve_missing_group_names", "start_periodic_contacts_sync",
            "set_chats")


def _round(cold=False):
    """A settled three-chat account, instrumented to see what happens after a
    wipe. ``cold`` latches full mode, which is what gives phase 1 targets."""
    stub = _make([3, 3], wa_web=3, local_chats=3)
    stub._sync_run_id = 1
    stub._force_full_sync = cold
    # Sentinels, not False: a superseded round must leave these exactly as the
    # wipe left them, and False would hide a round that wrote False.
    stub._sync_completed = "sentinel"
    stub._sync_retry_count = "sentinel_retry"
    stub.unnamed = ["1203630001@lid"]  # gives the backfill a reason to start
    stub.wiped = False
    stub.after_wipe = []
    stub.commits = []
    stub.run_ids_seen = []
    stub._persist_successful_sync_state = lambda *a, **kw: stub.commits.append("state")
    stub._schedule_save = lambda **kw: stub.commits.append("save")

    for name in _TRACKED:
        def _tracked(*a, _name=name, **kw):
            if stub.wiped:
                stub.after_wipe.append(_name)
        setattr(stub, name, _tracked)

    def _sync_remote(target_chats=None, incremental=False, expected_run_id=None):
        stub.message_sync_ran += 1
        stub.run_ids_seen.append(expected_run_id)
        return set()

    stub.sync_remote_chats = _sync_remote
    return stub


def _wipe(stub):
    """What clear_local_data() does to a round in flight, as far as the round
    can see: a new generation, and an empty chat list."""
    stub._sync_run_id += 1
    stub.chats = {}
    stub.wiped = True


def _wipe_during(stub, name, when=lambda *a, **kw: True):
    """Run the wipe inside *name*, the first time *when* accepts its call."""
    inner = getattr(stub, name)

    def _hooked(*a, **kw):
        if not stub.wiped and when(*a, **kw):
            _wipe(stub)
        return inner(*a, **kw)

    setattr(stub, name, _hooked)


def _assert_abandoned(stub):
    assert stub.chats == {}, "the previous account's chats were written back after the wipe"
    assert "set_chats" not in stub.after_wipe, "the chat list was shown again after the wipe"
    assert stub._sync_completed == "sentinel"
    assert stub._sync_retry_count == "sentinel_retry"
    assert stub.commits == []
    assert stub.backfill_started is False


class TestASupersededRoundStopsWriting:
    def test_a_wipe_during_list_chats_is_not_undone_by_the_merge(self):
        """get_remote_chats() merges over the dict taken BEFORE the call, so
        assigning its result after a wipe restores the wiped chats."""
        stub = _round()
        _wipe_during(stub, "get_remote_chats")

        stub._run_sync()

        _assert_abandoned(stub)
        assert stub.message_sync_ran == 0

    def test_a_wipe_while_contacts_load_does_not_show_the_list(self):
        stub = _round()
        _wipe_during(stub, "get_remote_contacts")

        stub._run_sync()

        _assert_abandoned(stub)
        assert stub.message_sync_ran == 0

    def test_a_wipe_during_the_connection_wait_announces_and_latches_nothing(self):
        """The round's very first steps. With self.chats emptied under it, a
        round that went on would latch full mode as "empty-local-cache" — the
        wrong reason — and speak "synchronization_started" with interrupt=True
        over the announcement that the conversations were deleted."""
        stub = _round()
        stub.statuses, stub.latches, stub.spoken = [], [], []
        stub._set_status = lambda text: stub.statuses.append(text)
        stub._persist_full_sync_pending = lambda reason: stub.latches.append(reason)
        stub.output = lambda text, **kw: stub.spoken.append(text)
        stub.background_mode = False
        stub.check_wa_connection_http = lambda *a, **kw: stub.wiped or _wipe(stub)

        stub._run_sync()

        _assert_abandoned(stub)
        assert (stub.statuses, stub.latches, stub.spoken) == ([], [], [])
        assert stub.fetches == 0
        assert stub._force_full_sync is False

    def test_a_wipe_while_the_list_is_normalized_is_not_undone_by_it(self):
        """normalize_chats() reads self.chats; assigning its result after a
        wipe that landed inside it restores the wiped chats."""
        stub = _round()
        _wipe_during(stub, "normalize_chats")

        stub._run_sync()

        _assert_abandoned(stub)

    def _waiting_cold_round(self, wait):
        stub = _round(cold=True)
        stub._recent_history_needs_wait = MainWindow._recent_history_needs_wait
        stub._history_wait_outcome = ""
        stub.unblock_history_sync = lambda timeout=60: {
            "restarted": True, "recentCompleted": False}
        stub.wait_for_restarted_history_sync = wait(stub)
        return stub

    def test_a_wipe_during_the_recent_history_wait_ends_the_round_before_the_refresh(self):
        """The cold path's wait runs for up to ten minutes — the likeliest
        place for a pairing-time wipe to land. The list-chats refresh after it
        is one more request for an account that is gone."""
        def _wait(stub):
            def _inner(timeout=600, should_stop=None):
                _wipe(stub)
                stub.fetches_at_wipe = stub.fetches
                stub._history_wait_outcome = "completed"
                return True
            return _inner

        stub = self._waiting_cold_round(_wait)

        stub._run_sync()

        _assert_abandoned(stub)
        assert stub.fetches == stub.fetches_at_wipe
        assert stub.message_sync_ran == 0

    def test_the_wait_is_handed_a_way_to_stop_on_a_supersession(self):
        """Without it a superseded round sits out the whole budget before any
        check can see the wipe — which is what forced #199 to wait 720 s."""
        answers = []

        def _wait(stub):
            def _inner(timeout=600, should_stop=None):
                answers.append(should_stop())
                _wipe(stub)
                answers.append(should_stop())
                stub._history_wait_outcome = "superseded"
                return False
            return _inner

        stub = self._waiting_cold_round(_wait)

        stub._run_sync()

        assert answers == [False, True]

    def test_a_wipe_during_the_post_wait_refresh_is_not_undone_by_it(self):
        stub = self._waiting_cold_round(lambda stub: lambda timeout=600, should_stop=None: True)
        # The settle loop passes prune_stale=True; on the cold path the first
        # call without it is the refresh after the wait.
        _wipe_during(stub, "get_remote_chats",
                     when=lambda chats, **kw: not kw.get("prune_stale"))

        stub._run_sync()

        _assert_abandoned(stub)
        assert stub.message_sync_ran == 0

    def test_a_wipe_during_the_message_phase_ends_the_round_there(self):
        """Phase 1 is where the minutes go. Its failures must decide nothing,
        and nothing after it may run: contacts, block list, group names."""
        stub = _round(cold=True)

        def _sync_remote(target_chats=None, incremental=False, expected_run_id=None):
            stub.run_ids_seen.append(expected_run_id)
            _wipe(stub)
            return {c["remoteJid"] for c in target_chats or []}

        stub.sync_remote_chats = _sync_remote

        stub._run_sync()

        _assert_abandoned(stub)
        assert stub.after_wipe == []

    def test_a_wipe_while_duplicates_are_merged_is_not_undone_by_it(self):
        stub = _round(cold=True)
        _wipe_during(stub, "deduplicate_chats")

        stub._run_sync()

        _assert_abandoned(stub)
        assert stub.after_wipe == []

    def test_a_wipe_during_the_media_phase_says_nothing_more(self):
        """Phase 2 runs only after the commit, so the round was current when it
        started — and it can run for minutes. It must stop downloading, and
        must not announce "concluído" for data that is gone."""
        stub = _round(cold=True)
        jid = next(iter(stub.chats))
        stub.chats[jid]["messages"] = {"messages": {"records": [
            {"key": {"id": "IMG1", "remoteJid": jid}, "messageType": "imageMessage"}]}}
        stub.settings["storage"]["auto_download_media"] = True
        stub.background_mode = False
        stub.spoken = []
        stub.output = lambda text, **kw: stub.spoken.append(text)
        answers = []

        def _media(jids=None, should_stop=None):
            _wipe(stub)
            answers.append(should_stop())
            return 0

        stub.sync_media_for_all_chats = _media

        stub._run_sync()

        assert answers == [True]
        assert "sync_media_started" in stub.spoken  # the phase really began
        # "Started" with no "completed" is deliberate, not a missing pair to
        # fix: the data is gone, and the corrective sync (or F5) that follows
        # a wipe announces itself.
        assert "sync_media_completed" not in stub.spoken
        assert "sync_media_failed" not in stub.spoken
        assert "set_chats" not in stub.after_wipe

    def test_the_message_phase_is_handed_the_round_s_run_id(self):
        """What lets sync_remote_chats() stop mid-phase instead of after it."""
        stub = _round(cold=True)

        stub._run_sync()

        assert stub.run_ids_seen == [1]

    def test_a_wipe_during_the_chat_state_refresh_is_not_undone_by_it(self):
        stub = _round()
        # The settle loop passes prune_stale=True; the refresh after the
        # message phase is the only warm-path call that does not.
        _wipe_during(stub, "get_remote_chats",
                     when=lambda chats, **kw: not kw.get("prune_stale"))

        stub._run_sync()

        _assert_abandoned(stub)

    def test_a_wipe_at_the_last_moment_starts_no_backfill(self):
        """The commit guard used to log and fall through. The backfill thread
        reads _sync_run_id when it starts, so one started here would adopt the
        NEWER run's id and walk the previous account's history as current."""
        stub = _round()
        stub.preselect_conversations = lambda: _wipe(stub)

        stub._run_sync()

        assert stub._sync_completed == "sentinel"
        assert stub.commits == []
        assert stub.backfill_started is False
        assert stub.after_wipe == []


class TestTheSameRoundNotSupersededIsUnchanged:
    """Controls for every test above: identical setup, no wipe."""

    def test_the_warm_round_commits_and_shows_the_list(self):
        stub = _round()
        shown = []
        stub.set_chats = lambda: shown.append(True)

        stub._run_sync()

        assert len(stub.chats) == 3
        assert stub._sync_completed is True
        assert stub.commits == ["state", "save"]
        assert stub.backfill_started is True
        assert len(shown) == 3  # before phase 1, after it, after media

    def test_the_cold_round_runs_its_message_phase_and_commits(self):
        stub = _round(cold=True)

        stub._run_sync()

        assert stub.message_sync_ran == 1
        assert stub._sync_completed is True


class TestStartSyncAroundASupersededRound:
    def _bound(self, stub):
        stub._ui_ready_event = threading.Event()
        stub._ui_ready_event.set()
        stub.start_sync = types.MethodType(MainWindow.start_sync, stub)
        return stub

    def test_the_running_flag_is_released_so_the_join_loop_can_proceed(self):
        stub = self._bound(_round())
        stub._initial_sync_running = False
        _wipe_during(stub, "get_remote_chats")

        stub.start_sync()

        assert stub._initial_sync_running is False
        assert stub._sync_completed == "sentinel"

    def test_start_sync_s_own_bump_does_not_supersede_the_round_it_starts(self):
        stub = self._bound(_round())

        stub.start_sync()

        assert stub._sync_run_id == 2
        assert stub._sync_completed is True

    def test_a_bump_between_start_sync_and_the_round_is_not_adopted(self, monkeypatch):
        """_run_sync() used to read the id afresh, so a wipe landing between
        start_sync()'s assignment and that read became this round's own id and
        the round ran on as current. The first time.time() call in start_sync()
        comes right after the assignment, which is where the wipe is injected."""
        stub = self._bound(_round())
        real_time = main.time.time

        def _time():
            if not stub.wiped:
                _wipe(stub)
            return real_time()

        monkeypatch.setattr(main.time, "time", _time)

        stub.start_sync()

        assert stub._sync_run_id == 3
        assert stub._sync_completed == "sentinel"
        assert stub.fetches == 0


class TestTheRecentWaitStopsOnASupersession:
    def test_it_returns_on_the_next_poll_instead_of_the_deadline(self, monkeypatch):
        from tests.test_history_sync_on_demand import _Stub as _HistoryStub

        clock = [0.0]
        monkeypatch.setattr(main.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(main.time, "sleep",
                            lambda seconds: clock.__setitem__(0, clock[0] + seconds))
        polls = []
        monkeypatch.setattr(_HistoryStub, "fetch_history_sync_status",
                            lambda self, timeout=10: polls.append(1) or {
                                "unprocessedChunks": 5, "recentCompleted": False,
                                "storeCounts": {"message": 100}})
        stub = _HistoryStub()
        asked = []

        def _should_stop():
            asked.append(1)
            return len(asked) > 1

        assert stub.wait_for_restarted_history_sync(timeout=600,
                                                    should_stop=_should_stop) is False
        assert stub._history_wait_outcome == "superseded"
        assert len(polls) == 1
        assert clock[0] < 600


class TestTheMediaPhaseStopsOnASupersession:
    @pytest.fixture(autouse=True)
    def _real_threads(self, _fast, monkeypatch):
        # See TestASupersededMessagePhaseRecordsNoFailures below.
        monkeypatch.setattr(main.threading, "Thread", _REAL_THREAD)

    def _stub(self):
        from tests.test_media_sync_count import _Stub as _MediaStub, _chat as _media_chat, _media_msg

        stub = _MediaStub(chats={"a@s.whatsapp.net": _media_chat(
            _media_msg("A"), _media_msg("B"), _media_msg("C"))})
        # One worker, so "queued after the stop" is deterministic.
        stub._MEDIA_SYNC_WORKERS = 1
        stub._normalize_jid = MainWindow._normalize_jid
        stub.stopped = False
        inner = stub.sync_if_media

        def _download(msg, timeout=60):
            stub.stopped = True  # the round is superseded mid-download
            return inner(msg, timeout)

        stub.sync_if_media = _download
        return stub

    def test_queued_downloads_do_not_start_and_nothing_is_saved(self):
        stub = self._stub()

        stub.sync_media_for_all_chats(should_stop=lambda: stub.stopped)

        assert len(stub.seen) == 1
        assert stub._saved is False

    def test_without_should_stop_everything_runs_as_before(self):
        stub = self._stub()

        stub.sync_media_for_all_chats()

        assert len(stub.seen) == 3
        assert stub._saved is True


class TestAMessageTaskDiscardsWhatItFetchedForASupersededRound:
    """The workers already fetching when the bump lands. Nothing else cleans
    up after them on a confirmed logout, which has no second wipe."""

    JID = "5511900000000@s.whatsapp.net"

    def _stub(self, monkeypatch, bump_on_fetch):
        from tests.test_incremental_delta_outcomes import _DeltaStub, _Resp

        monkeypatch.setattr(main.time, "sleep", lambda *_: None)
        stub = _DeltaStub(normalized=[{
            "key": {"remoteJid": self.JID, "id": "m1", "fromMe": False},
            "message": {"conversation": "oi"},
            "messageType": "conversation",
            "messageTimestamp": 500,
        }])
        stub._sync_run_id = 1
        # Reached only by the control test's successful write; missing, they
        # make that write report failure and hide what it is there to show.
        stub._note_verified_activity = lambda *a, **kw: None
        stub._note_chat_verified_now = lambda *a, **kw: None

        def _get(url, **kwargs):
            if bump_on_fetch:
                stub._sync_run_id = 2
            return _Resp(200, {"response": [{"id": "m1"}]})

        monkeypatch.setattr(main.requests, "get", _get)
        return stub

    def _assert_nothing_written(self, stub, result):
        assert result is False
        assert stub.chats == {}
        assert stub.db.calls == []
        assert stub._sync_failed_chats == set()
        assert stub._delta_unsatisfied_chats == set()

    def test_a_bump_during_the_fetch_writes_nothing(self, monkeypatch):
        stub = self._stub(monkeypatch, bump_on_fetch=True)

        result = MainWindow.sync_chat_messages(stub, {"remoteJid": self.JID, "t": 500},
                                               expected_run_id=1)

        self._assert_nothing_written(stub, result)

    def test_a_bump_during_the_gap_widening_writes_nothing(self, monkeypatch):
        stub = self._stub(monkeypatch, bump_on_fetch=False)
        monkeypatch.setattr(main, "history_gap_detected", lambda *a, **kw: True)

        def _widen(*a, **kw):
            stub._sync_run_id = 2
            return []

        stub._refetch_history_gap = _widen

        result = MainWindow.sync_chat_messages(stub, {"remoteJid": self.JID, "t": 500},
                                               expected_run_id=1)

        self._assert_nothing_written(stub, result)

    def test_the_same_task_not_superseded_writes_as_before(self, monkeypatch):
        """Control for both: no bump, so the chat and its messages land."""
        stub = self._stub(monkeypatch, bump_on_fetch=False)
        monkeypatch.setattr(main, "history_gap_detected", lambda *a, **kw: True)

        result = MainWindow.sync_chat_messages(stub, {"remoteJid": self.JID, "t": 500},
                                               expected_run_id=1)

        assert result is True
        assert self.JID in stub.chats
        assert "insert_messages_batch" in stub.db.calls


A = "5511900000000@s.whatsapp.net"
B = "5511911111111@s.whatsapp.net"
C = "5511922222222@s.whatsapp.net"


class _PhaseStub:
    """Just what the real sync_remote_chats() reads."""

    def __init__(self):
        self.chats = {}
        self._sync_run_id = 1
        self._sync_failures_lock = threading.Lock()
        # C belongs to no target: an entry this pass has no business clearing.
        self._sync_failed_chats = {C}
        self._delta_unsatisfied_chats = set()
        self._absent_chats = set()
        self._message_retry_jids = set()
        self._history_still_landing = False
        self.calls = []
        self.persists = 0
        self.wipe_on_first_call = False

    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    # A voice call stands the recurring background work down while it is
    # up. Bound from the real class rather than left to whatever a stub's
    # __getattr__ would invent: a truthy answer makes the pause permanent,
    # which is how one guard turned a test file into a multi-hour CI run.
    _voice_call_in_progress = MainWindow._voice_call_in_progress
    _VOICE_CALL_PAUSE_MAX_SECONDS = MainWindow._VOICE_CALL_PAUSE_MAX_SECONDS
    _active_voice_call = None
    _voice_call_pause_since = 0.0

    def sync_chat_messages(self, chat, expected_run_id=None, sync_mode="full"):
        with self._sync_failures_lock:
            self.calls.append((chat["remoteJid"], expected_run_id, sync_mode))
            if self.wipe_on_first_call and len(self.calls) == 1:
                self._sync_run_id += 1
                # A worker already fetching when the bump landed, whose fetch
                # genuinely failed: it records itself the way the real one does.
                self._sync_failed_chats.add(chat["remoteJid"])
        # Every other chat meets the real method's stale check and returns False.
        return False

    def _persist_message_retry_jids(self):
        self.persists += 1

    def _persist_backfill_pending_state(self):
        self.persists += 1

    def _persist_history_gap_jids(self):
        self.persists += 1


def _targets():
    return [{"remoteJid": A, "t": 100}, {"remoteJid": B, "t": 90}]


class TestASupersededMessagePhaseRecordsNoFailures:
    @pytest.fixture(autouse=True)
    def _real_threads(self, _fast, monkeypatch):
        # _fast replaces main.threading.Thread with a synchronous fake so
        # _run_sync() never spawns anything; that is the same module object
        # ThreadPoolExecutor starts its workers from, so the real
        # sync_remote_chats() cannot run under it.
        monkeypatch.setattr(main.threading, "Thread", _REAL_THREAD)

    @pytest.mark.parametrize("incremental", [False, True])
    def test_its_failures_reach_neither_the_caller_nor_the_retry_list(self, incremental):
        stub = _PhaseStub()
        stub.wipe_on_first_call = True

        result = MainWindow.sync_remote_chats(stub, _targets(), incremental=incremental,
                                              expected_run_id=1)

        assert result == set()
        assert stub._message_retry_jids == set()
        assert stub._sync_failed_chats == {C}
        assert stub.persists == 0

    def test_the_run_id_reaches_every_chat_so_queued_ones_stop(self):
        stub = _PhaseStub()

        MainWindow.sync_remote_chats(stub, _targets(), incremental=False, expected_run_id=1)
        MainWindow.sync_remote_chats(stub, _targets(), incremental=True, expected_run_id=1)

        assert {run_id for _, run_id, _ in stub.calls} == {1}

    def test_a_pass_that_starts_already_superseded_fetches_nothing(self):
        stub = _PhaseStub()
        stub._sync_run_id = 2

        assert MainWindow.sync_remote_chats(stub, _targets(), expected_run_id=1) == set()
        assert stub.calls == []

    def test_without_a_run_id_the_same_failures_still_count(self):
        """Control, and the periodic poll's path: it passes no run id, so a
        bump in the middle of it changes nothing it reports."""
        stub = _PhaseStub()
        stub.wipe_on_first_call = True

        result = MainWindow.sync_remote_chats(stub, _targets(), incremental=True)

        assert result == {A, B}
        assert stub._message_retry_jids == {A, B}
        assert stub.persists == 3
