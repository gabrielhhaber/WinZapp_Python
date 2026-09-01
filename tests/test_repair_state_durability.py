"""Everything a sync round has to remember across a restart, and who reads it.

An incremental round decides what to fetch by comparing the server's chat list
against the activity markers already on disk. That comparison is only safe
while the client also remembers what it still *owes*: a chat whose first page
never arrived, a known hole in the middle of a conversation, a delta whose
request failed after list-chats had already advanced the marker. Forget any of
those on restart and the next launch sees an ordinary unchanged chat and never
queries it again — silently, and for good.

So each of them is written under its own metadata key and each is read back by
prepare_sync(). These cover the writing half and the consumer that acts on what
was restored; the reading half is pinned separately at the bottom, and that
test explains why it is the shape it is.

MainWindow is a wx.Frame and cannot be instantiated without a running wx.App,
so the methods are bound to plain stubs — the pattern the other main.py tests
use.
"""

import pathlib
import threading
import types

from main import MainWindow
from tests.conftest import warm_cached_chat as _chat


LID = "111222333@lid"
PHONE = "5511900000000@s.whatsapp.net"
OTHER = "5511911111111@s.whatsapp.net"


class _Db:
    def __init__(self):
        self.metadata = {}

    def set_metadata_json(self, key, value):
        self.metadata[key] = value


class _StateStub:
    """The state the persistence helpers read, and nothing else."""

    _canonical_backfill_jid = MainWindow._canonical_backfill_jid
    _backfill_state_guard = MainWindow._backfill_state_guard
    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    _server_claims_content = staticmethod(MainWindow._server_claims_content)
    _jid_address_forms = MainWindow._jid_address_forms
    _baseline_marker_for_jid = MainWindow._baseline_marker_for_jid
    _plan_message_sync = MainWindow._plan_message_sync
    _note_verified_activity = MainWindow._note_verified_activity
    _persist_message_retry_jids = MainWindow._persist_message_retry_jids
    _persist_history_gap_jids = MainWindow._persist_history_gap_jids
    _persist_backfill_pending_state = MainWindow._persist_backfill_pending_state
    _persist_full_sync_pending = MainWindow._persist_full_sync_pending
    _persist_successful_sync_state = MainWindow._persist_successful_sync_state

    def __init__(self):
        self.db = _Db()
        self.chats = {}
        self._backfill_state_lock = threading.RLock()
        # Taken by _persist_message_retry_jids() before it reads the set.
        self._sync_failures_lock = threading.Lock()
        self._chats_awaiting_messages = set()
        self._partial_history_counts = {}
        self._history_gap_jids = set()
        self._message_retry_jids = set()
        self._lid_to_phone = {}
        self._phone_to_lid = {}
        self._last_sync_state = {}


class TestTheShortHistoryQueueIsWritten:
    def test_a_queued_chat_is_stored_with_how_far_it_got(self):
        """The count is what makes the next pass able to tell "it grew" from
        "this conversation really is that short"."""
        stub = _StateStub()
        stub._chats_awaiting_messages = {PHONE}
        stub._partial_history_counts = {PHONE: 15}
        stub._persist_backfill_pending_state()
        assert stub.db.metadata["backfill_pending_v1"] == {PHONE: 15}

    def test_a_chat_queued_under_its_lid_is_stored_under_its_phone_jid(self):
        """deduplicate_chats() re-keys the chat between sessions, so a queue
        entry saved under the @lid would come back naming a conversation that
        no longer exists by that name and never be matched again."""
        stub = _StateStub()
        stub._lid_to_phone = {LID: PHONE}
        stub._chats_awaiting_messages = {LID}
        stub._partial_history_counts = {LID: 15}
        stub._persist_backfill_pending_state()
        assert stub.db.metadata["backfill_pending_v1"] == {PHONE: 15}

    def test_an_empty_queue_is_written_as_empty_and_not_skipped(self):
        """A drained queue has to overwrite the previous session's payload, or
        every restart resurrects chats that were repaired long ago."""
        stub = _StateStub()
        stub._persist_backfill_pending_state()
        assert stub.db.metadata["backfill_pending_v1"] == {}


class TestTheFailedDeltaLatchIsWritten:
    def test_the_retry_list_is_stored_under_its_own_key(self):
        stub = _StateStub()
        stub._message_retry_jids = {PHONE, OTHER}
        stub._persist_message_retry_jids()
        assert stub.db.metadata["message_retry_jids_v1"] == sorted([PHONE, OTHER])

    def test_a_known_history_gap_is_stored_too(self):
        stub = _StateStub()
        stub._history_gap_jids = {PHONE}
        stub._persist_history_gap_jids()
        assert stub.db.metadata["history_gap_jids_v1"] == [PHONE]


class TestWhatIsRestoredActuallyDrivesTheNextRound:
    """Persisting the latch is only half of it — _plan_message_sync() is what
    turns it back into a request. Without this the chat would be classified
    "unchanged" against a marker list-chats had already advanced past."""

    def test_a_chat_on_the_retry_list_is_fetched_even_when_its_marker_matches(self):
        stub = _StateStub()
        stub.chats = {PHONE: _chat(PHONE)}
        baseline = {PHONE: MainWindow._capture_chat_sync_baseline(stub)[PHONE]}
        stub._message_retry_jids = {PHONE}

        full, incremental, skipped, reasons = stub._plan_message_sync(baseline)

        assert [c["remoteJid"] for c in incremental] == [PHONE]
        assert reasons[PHONE] == "retry-failed"
        assert skipped == 0

    def test_the_same_chat_with_no_local_history_gets_a_full_page(self):
        stub = _StateStub()
        stub.chats = {PHONE: _chat(PHONE, records=0)}
        baseline = {PHONE: MainWindow._capture_chat_sync_baseline(stub)[PHONE]}
        stub._message_retry_jids = {PHONE}

        full, incremental, _skipped, reasons = stub._plan_message_sync(baseline)

        assert [c["remoteJid"] for c in full] == [PHONE]
        assert reasons[PHONE] == "retry-empty-cache"

    def test_a_restored_short_history_queue_forces_a_full_repair(self):
        stub = _StateStub()
        stub.chats = {PHONE: _chat(PHONE)}
        baseline = {PHONE: MainWindow._capture_chat_sync_baseline(stub)[PHONE]}
        stub._chats_awaiting_messages = {PHONE}

        full, _incremental, _skipped, reasons = stub._plan_message_sync(baseline)

        assert [c["remoteJid"] for c in full] == [PHONE]
        assert reasons[PHONE] == "history-repair"

    def test_an_unencumbered_chat_is_still_skipped(self):
        """The control: without a latch, an unchanged chat costs no request at
        all — which is the entire reason the incremental path exists."""
        stub = _StateStub()
        stub.chats = {PHONE: _chat(PHONE)}
        baseline = {PHONE: MainWindow._capture_chat_sync_baseline(stub)[PHONE]}

        full, incremental, skipped, _reasons = stub._plan_message_sync(baseline)

        assert (full, incremental, skipped) == ([], [], 1)


class TestTheOneMarkerThatIsDeliberatelyNotDurable:
    """The counterpart to everything above: the per-session record of which
    chats a get-messages already covered.

    It exists because a chat can legitimately claim activity newer than any
    message that will ever be stored (a filtered protocol row), and the
    content-based signal would otherwise re-query it every round. It is
    _not_ persisted precisely because the state it suppresses — an activity
    marker that reached the database without its message — is exactly the one
    a restart must be free to look at again.
    """

    @staticmethod
    def _behind(jid):
        """A chat whose newest stored message is older than what `t` claims."""
        chat = _chat(jid, t=100)
        chat["messages"]["messages"]["records"][0]["messageTimestamp"] = 90
        return chat

    def test_a_chat_whose_stored_history_lags_its_marker_is_queried(self):
        stub = _StateStub()
        stub.chats = {PHONE: self._behind(PHONE)}
        baseline = {PHONE: MainWindow._capture_chat_sync_baseline(stub)[PHONE]}

        _full, incremental, _skipped, reasons = stub._plan_message_sync(baseline)

        assert [c["remoteJid"] for c in incremental] == [PHONE]
        assert reasons[PHONE] == "local-behind-server"

    def test_a_completed_fetch_this_session_settles_it(self):
        stub = _StateStub()
        stub.chats = {PHONE: self._behind(PHONE)}
        baseline = {PHONE: MainWindow._capture_chat_sync_baseline(stub)[PHONE]}
        stub._note_verified_activity(PHONE, stub.chats[PHONE])

        full, incremental, skipped, _reasons = stub._plan_message_sync(baseline)

        assert (full, incremental, skipped) == ([], [], 1)

    def test_it_is_recorded_under_the_normalized_jid(self):
        """The legacy @c.us form reaches sync_chat_messages() too, and the plan
        only ever looks the chat up under its @s.whatsapp.net form."""
        stub = _StateStub()
        stub._note_verified_activity(PHONE.replace("@s.whatsapp.net", "@c.us"),
                                     {"t": 100})
        assert stub._verified_activity == {PHONE: 100}

    def test_a_newer_fetch_never_lowers_it(self):
        stub = _StateStub()
        stub._note_verified_activity(PHONE, {"t": 100})
        stub._note_verified_activity(PHONE, {"t": 90})
        assert stub._verified_activity == {PHONE: 100}


class TestTheFullSyncLatchSurvivesAFailedRound:
    """F5 and a first pairing both ask for a full rebuild, and both can be
    interrupted. The latch is what stops the *next* attempt from quietly
    downgrading itself to a warm refresh over a half-rebuilt cache."""

    def test_the_reason_is_recorded_with_the_pending_flag(self):
        stub = _StateStub()
        stub._persist_full_sync_pending("manual-resync")
        state = stub.db.metadata["sync_state_v1"]
        assert state["force_full_pending"] is True
        assert state["full_pending_reason"] == "manual-resync"

    def test_a_completed_round_clears_it(self):
        stub = _StateStub()
        stub._persist_full_sync_pending("empty-local-cache")
        stub._persist_successful_sync_state("full", 12, 0, 0)
        assert stub.db.metadata["sync_state_v1"]["force_full_pending"] is False


class TestF5LatchesTheFullRebuild:
    """_resync_all_worker() is the F5 handler's background half. It wipes the
    local database, so the round that follows has no cache to compare against
    and must never be planned incrementally — the latch is set here, before
    the wipe, rather than being inferred later from an empty cache."""

    def _worker_stub(self, monkeypatch, tmp_path):
        import main

        panel = types.SimpleNamespace(
            _stop_audio=lambda: None,
            close_conversation=lambda: None,
            conversations_list=types.SimpleNamespace(DeleteAllItems=lambda: None),
        )
        stub = types.SimpleNamespace(
            conversations_panel=panel,
            _initial_sync_running=False,
            _sync_completed=True,
            _sync_retry_count=7,
            _force_full_sync=False,
            _media_failed_ids={"x": 1},
            latches=[],
            cleared=[],
            started=[],
        )
        stub._persist_full_sync_pending = stub.latches.append
        stub.clear_local_data = lambda wipe_metadata=True: stub.cleared.append(wipe_metadata)
        stub._forget_history_exhaustion = lambda: None
        stub._try_start_sync_thread = lambda: stub.started.append(True)

        # The handler hands its UI teardown to wx.CallAfter and then blocks on
        # an Event for 5 s; with no event loop running, nothing would ever set
        # it. Run it inline instead of waiting out the timeout.
        monkeypatch.setattr(main.wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
        # data_path() resolves to the real installation's data folder on a dev
        # machine, and this method deletes a file inside it.
        monkeypatch.setattr(main, "data_path", lambda *parts: str(tmp_path.joinpath(*parts)))
        return stub

    def test_the_full_sync_latch_is_set_and_recorded(self, monkeypatch, tmp_path):
        stub = self._worker_stub(monkeypatch, tmp_path)
        MainWindow._resync_all_worker(stub)
        assert stub._force_full_sync is True
        assert stub.latches == ["manual-resync"]

    def test_clear_local_data_keeps_the_users_own_metadata(self, monkeypatch, tmp_path):
        """wipe_metadata=False: cleared/deleted/archived/muted/blocked are the
        user's own decisions on top of the chat list, not part of it."""
        stub = self._worker_stub(monkeypatch, tmp_path)
        MainWindow._resync_all_worker(stub)
        assert stub.cleared == [False]

    def test_the_sync_is_restarted_with_a_fresh_retry_budget(self, monkeypatch, tmp_path):
        stub = self._worker_stub(monkeypatch, tmp_path)
        MainWindow._resync_all_worker(stub)
        assert stub._sync_completed is False
        assert stub._sync_retry_count == 0
        assert stub.started == [True]


def test_prepare_sync_reads_the_same_metadata_keys_the_round_writes():
    """The one assertion here that reads source instead of running it.

    The restoring half lives in prepare_sync(), which opens a real
    DatabaseBridge against messages.db, spawns maintenance threads and touches
    the installation's data folder — it cannot be bound to a stub the way every
    other method in this file is. What it can still be held to is the thing
    that actually breaks: a writer and a reader that stop agreeing on the key
    name. That is a rename away and would surface only as history quietly
    failing to come back after a restart, so it is worth pinning by name even
    from the outside. Nothing about formatting or code shape is asserted.
    """
    source = (pathlib.Path(__file__).parents[1] / "client" / "main.py").read_text(
        encoding="utf-8")
    for key in ("backfill_pending_v1", "message_retry_jids_v1",
                "history_gap_jids_v1", "sync_state_v1"):
        assert f'set_metadata_json("{key}"' in source, f"nothing writes {key}"
        assert f'get_metadata_json("{key}"' in source, (
            f"{key} is written but never restored — the state is lost on restart"
        )
