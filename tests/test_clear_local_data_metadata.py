"""Tests for the in-memory half of clear_local_data(wipe_metadata=True).

The wipe used to be partial. prepare_sync() reads deleted/archived/pinned/
muted chats, the block list, the presence push-name map, locally_read_at and
my_jid/my_lid OUT of the system_metadata table into RAM at startup, and every
caller of clear_local_data() up to now ran BEFORE that load — so emptying the
table was enough. _wipe_local_data_if_another_number_linked() is the first one
that runs after it, and there the table went while account A's sets stayed
live in the process.

What that produces is not a cosmetic leak. Account A had
5511988887777@s.whatsapp.net deleted (or muted, archived, pinned). Phone B
pairs by QR and talks to that same contact — two phones in one family or one
company, which is the ordinary case of this bug. The database is emptied, then
B's first sync writes A's sets straight back into B's database:
get_remote_chats() persists muted/pinned/archived, the chat-list build persists
the deleted set. That conversation then never appears in B's list at all, or
appears silenced/archived, and it stays that way on disk. my_jid is the same
story one level down — it holds A's JID until the first CONNECTED, and
_resolve_self_referential_jid() sends a conversation of B's with the old number
to the "Eu" self-chat.

wipe_metadata=False (F5/resync) must preserve every one of them — that is the
entire point of the flag, and resyncing used to silently undo all of those
local actions.

The media sweep at the end of the method is here too, for a failure of the
same family: one entry Windows refuses to delete — a voice note BASS still has
open is the measured case — used to abort the sweep of its whole directory
from that point on, leaving the rest of the previous account's files on disk.
"""

import inspect
import threading
from contextlib import contextmanager

import pytest

from core.database_bridge import DatabaseBridgeClosed, DatabaseBridgeTimeout
import main as main_module
from main import MainWindow


@pytest.fixture(autouse=True)
def _media_dirs_elsewhere(tmp_path, monkeypatch):
    """data_path() refuses to answer without an active account, and the tail
    of clear_local_data() deletes media/ and voice_messages/ file by file."""
    monkeypatch.setattr(main_module, "data_path",
                        lambda *parts: str(tmp_path.joinpath(*parts)))


class _FakeDB:
    def __init__(self):
        self.calls = []
        self.metadata = {}

    def save_full_state(self, data, clear_metadata=True):
        self.calls.append(clear_metadata)

    def set_metadata_json(self, key, value):
        self.metadata[key] = value


class _Stub:
    """Minimal stand-in for MainWindow for clear_local_data().

    Carries the metadata that used to survive the wipe, plus the collections
    and locks the method touches on its way there. data_path() is real: the
    media/voice_messages sweep at the end is skipped when the directories do
    not exist, and this stub never creates them.
    """

    def __init__(self):
        self.chats = {"5511988887777@s.whatsapp.net": {}}
        self.contacts = {"5511988887777@s.whatsapp.net": {}}
        self._status_updates = {"a": {}}
        self.db = _FakeDB()
        self.settings = {"privateinfo": {
            "WA_phone_number_linked": "5511999999999",
            "paired": True,
        }}
        self.saved = 0

        # The metadata prepare_sync() loads out of system_metadata.
        self._deleted_chats = {"5511988887777@s.whatsapp.net"}
        self._archived_chats = {"5511988887777@s.whatsapp.net"}
        self._pinned_chats = {"5511988887777@s.whatsapp.net"}
        self._muted_chats = {"5511988887777@s.whatsapp.net": 0}
        self._blocked_contacts = {"5511988887777"}
        self._presence_pushname_map = {"5511988887777@s.whatsapp.net": "Ana"}
        self._locally_read_at = {"5511988887777@s.whatsapp.net": 1700000000}
        self.my_jid = "5511999999999@s.whatsapp.net"
        self.my_lid = "182736450192837@lid"
        self._group_send_perms = {
            "120363000000000000@g.us": {"can_send": True, "announce": False},
        }
        self._last_sync_state = {"mode": "incremental", "chat_count": 155}
        self._exhausted_chats = {"5511988887777@s.whatsapp.net"}
        self._older_requested_chats = {"5511988887777@s.whatsapp.net": 1700000000.0}
        self._media_failed_ids = {"3EB0ABC": 1700000000.0}
        self._chat_verified_at = {"5511988887777@s.whatsapp.net": 1700000000}
        self._opened_conversations = {"5511988887777@s.whatsapp.net"}
        self._verified_activity = {"5511988887777@s.whatsapp.net": 1700000000}

        # Backfill/LID state the method already cleared before this change.
        self._sync_run_id = 3
        self._backfill_thread = object()
        self._chats_awaiting_messages = {"x@s.whatsapp.net"}
        self._partial_history_counts = {"x@s.whatsapp.net": 2}
        self._history_gap_jids = {"x@s.whatsapp.net"}
        self._message_retry_jids = {"x@s.whatsapp.net"}
        self._sync_failed_chats = {"x@s.whatsapp.net"}
        self._delta_unsatisfied_chats = {"x@s.whatsapp.net"}
        self._delta_unsatisfied_attempts = {"x@s.whatsapp.net": 1}
        self._absent_chats = {"x@s.whatsapp.net"}
        self._absent_chat_attempts = {"x@s.whatsapp.net": 1}
        self._lid_to_phone = {"182736450192837@lid": "5511999999999@s.whatsapp.net"}
        self._phone_to_lid = {"5511999999999@s.whatsapp.net": "182736450192837@lid"}
        self._unresolvable_lids = {"1@lid"}
        self._unresolvable_names = {"1@lid"}
        self._resolving_lids = {"1@lid"}
        self._lid_mapping_lock = threading.Lock()
        self._sync_failures_lock = threading.Lock()

    @contextmanager
    def _backfill_state_guard(self):
        yield

    def _persist_backfill_pending_state(self):
        pass

    def _persist_history_gap_jids(self):
        pass

    def _persist_message_retry_jids(self):
        pass

    def save_settings(self):
        self.saved += 1

    clear_local_data = MainWindow.clear_local_data
    # The one caller that reads the answer, bound for real beside it: with a
    # stub on either side of that call nothing tests the two together, and the
    # return value could be dropped without a failure anywhere.
    _apply_another_number_wipe = MainWindow._apply_another_number_wipe
    _MAX_MEDIA_DELETE_ERRORS_LOGGED = MainWindow._MAX_MEDIA_DELETE_ERRORS_LOGGED
    # Bound for real: the exhausted-history pair has had a one-line helper
    # since F5 needed exactly this, and its docstring already describes the
    # damage of keeping them.
    _forget_history_exhaustion = MainWindow._forget_history_exhaustion
    _forget_media_failures = MainWindow._forget_media_failures
    _persist_exhausted_chats = MainWindow._persist_exhausted_chats
    _persist_older_requested = MainWindow._persist_older_requested


_METADATA = ("_deleted_chats", "_archived_chats", "_pinned_chats",
             "_muted_chats", "_blocked_contacts", "_presence_pushname_map",
             "_locally_read_at",
             # Which groups the previous account was in at all.
             "_group_send_perms",
             # The last round's checkpoint, including the force_full_pending
             # latch prepare_sync() restores _force_full_sync from.
             "_last_sync_state",
             # "This chat has no older history", and the requests that
             # concluded it.
             "_exhausted_chats", "_older_requested_chats",
             # Ids of the previous account's messages whose media CDN URL had
             # already expired — the twelfth collection of this same family,
             # and the one that also has a file of its own on disk.
             "_media_failed_ids",
             # When get-messages last really ran for each chat. Left behind, a
             # timestamp of A's keeps a chat B shares with A out of the
             # staleness net for a full _STALE_RECHECK_AFTER — and
             # _persist_chat_verified_at() writes the whole dict, so A's JIDs
             # land in B's freshly emptied chat_verified_at_v1.
             "_chat_verified_at",
             # Which conversations the user opened — the gate on asking the
             # PHONE for older history. Left behind, a chat B shares with A
             # reads as opened, and _backfill_empty_chats() puts the
             # "Synchronizing WhatsApp…"/"Sync paused" pair on B's own lock
             # screen for a conversation B never opened (issue #108), then
             # _note_conversation_opened() makes A's JIDs durable on B's disk.
             "_opened_conversations",
             # Same family and same origin as _chat_verified_at, just never
             # persisted (issue #201) — inert today only because nothing
             # currently writes it to disk, so clearing it here keeps it out
             # of the same leak the moment that changes.
             "_verified_activity")


class TestAnAccountSwitchClearsTheMetadataInMemoryToo:
    def test_every_metadata_collection_is_emptied(self):
        stub = _Stub()

        stub.clear_local_data()

        for name in _METADATA:
            assert not getattr(stub, name), name

    def test_the_previous_accounts_own_jid_goes_with_them(self):
        """Left behind, _resolve_self_referential_jid() redirects a
        conversation of the NEW account with the old number into "Eu"."""
        stub = _Stub()

        stub.clear_local_data()

        assert stub.my_jid == ""
        assert stub.my_lid == ""

    def test_the_database_is_told_to_wipe_its_own_copy(self):
        """The two halves are one wipe: the table and the RAM it was read
        into."""
        stub = _Stub()

        stub.clear_local_data()

        assert stub.db.calls == [True]

    def test_the_chats_and_contacts_still_go(self):
        """Guard against the new block displacing what the method already
        did."""
        stub = _Stub()

        stub.clear_local_data()

        assert stub.chats == {}
        assert stub.contacts == {}
        assert stub._status_updates == {}
        assert stub._lid_to_phone == {}
        assert stub._phone_to_lid == {}


class TestWhatIsOutsideTheDatabaseGoesToo:
    """Two records of the deleted data that live in their own files.

    data/media_failed.json holds message ids whose media CDN URL answered
    403/410, and survived the account switch whole: account B started life
    refusing to download media it had never once tried. F5 has removed it by
    hand since before this flag existed (it passes wipe_metadata=False, and
    keeps everything else here too), so only the account switch changes.

    privateinfo["WA_phone_number_linked"] is the number the deleted data
    belonged to. The divergence check is written around that key describing
    what is on disk, and six call sites in connect.py wipe through here
    without ever having heard of it — leaving it naming an account whose
    database no longer exists, which is read as "no divergence" the next time
    somebody else's phone pairs.
    """

    def test_the_failed_media_file_goes_with_the_media(self, tmp_path):
        (tmp_path / "media_failed.json").write_text('{"3EB0ABC": 1700000000.0}')
        stub = _Stub()

        stub.clear_local_data()

        assert stub._media_failed_ids == {}
        assert not (tmp_path / "media_failed.json").exists()

    def test_the_resync_forgets_them_through_the_same_helper(self):
        """F5 deletes the messages those ids name, so it drops the map too —
        and had its own copy of this removal rather than sharing one. Source
        level: _resync_all_worker() is all wx teardown and cannot be bound to
        a stub the way the rest of this file is."""
        source = inspect.getsource(MainWindow._resync_all_worker)
        assert "self._forget_media_failures()" in source
        assert "media_failed_path" not in source

    def test_a_resync_keeps_the_failed_media_file(self, tmp_path):
        (tmp_path / "media_failed.json").write_text('{"3EB0ABC": 1700000000.0}')
        stub = _Stub()

        stub.clear_local_data(wipe_metadata=False)

        assert stub._media_failed_ids
        assert (tmp_path / "media_failed.json").exists()

    def test_the_recorded_linked_number_goes_with_the_data(self):
        stub = _Stub()

        stub.clear_local_data()

        assert "WA_phone_number_linked" not in stub.settings["privateinfo"]
        # And persisted: most of those call sites never save settings of their
        # own, and a key that survives in settings.json is exactly as wrong as
        # one that survives in memory.
        assert stub.saved == 1

    def test_nothing_else_in_privateinfo_is_touched(self):
        stub = _Stub()

        stub.clear_local_data()

        assert stub.settings["privateinfo"] == {"paired": True}

    def test_no_save_when_there_was_no_number_recorded(self):
        stub = _Stub()
        stub.settings["privateinfo"].pop("WA_phone_number_linked")

        stub.clear_local_data()

        assert stub.saved == 0

    def test_a_resync_keeps_the_recorded_linked_number(self):
        """F5 refetches the same account's chats; the number that account is
        linked to has not changed."""
        stub = _Stub()

        stub.clear_local_data(wipe_metadata=False)

        assert stub.settings["privateinfo"]["WA_phone_number_linked"] == "5511999999999"
        assert stub.saved == 0


class TestTheRecordedNumberOutlivesTheDataItDescribes:
    """The key names what is on disk, so it may only be dropped once what it
    names is really gone.

    A process killed between the two halves is routine here, not exotic: one
    field shutdown_audit.log covering 159 launches held 17 runs that ended
    with no _stop_wpp_server line at all. Killed in that window with the key
    dropped first, settings.json comes back without WA_phone_number_linked
    while messages.db still holds account A's history — so the next launch has
    nothing to compare against, takes the "learn this number, delete nothing"
    branch, and lets account B merge onto A. That is the merge this key exists
    to prevent, disarmed by its own cleanup. The other order costs one
    redundant wipe of an already empty database.
    """

    def _trace(self, stub):
        """The recorded number as each durable step saw it, in order."""
        seen = []

        def _recorded():
            return stub.settings["privateinfo"].get("WA_phone_number_linked")

        emptied = stub.db.save_full_state

        def _save_full_state(data, clear_metadata=True):
            seen.append(("database-emptied", _recorded()))
            return emptied(data, clear_metadata=clear_metadata)

        def _save_settings():
            seen.append(("settings-written", _recorded()))
            stub.saved += 1

        stub.db.save_full_state = _save_full_state
        stub.save_settings = _save_settings
        return seen

    def test_the_number_is_still_on_file_while_the_database_is_emptied(self):
        stub = _Stub()
        seen = self._trace(stub)

        stub.clear_local_data()

        assert seen[0] == ("database-emptied", "5511999999999")
        assert "WA_phone_number_linked" not in stub.settings["privateinfo"]

    def test_settings_are_written_only_after_the_database_is_empty(self):
        """The in-memory pop is not what the next launch reads — settings.json
        is, so it is the write that has to come second."""
        stub = _Stub()
        seen = self._trace(stub)

        stub.clear_local_data()

        assert [step for step, _ in seen] == ["database-emptied",
                                              "settings-written"]
        assert seen[1][1] is None


class TestAWipeThatEmptiedNothingLeavesTheNumberArmed:
    """The key describes what is on disk, so nothing may drop it while the
    data it names is still there — and "the wipe ran" is not the same thing as
    "the database was emptied".

    self.db only exists from prepare_sync() onwards, and all six connect.py
    call sites run before that, inside __init__'s connection dialog. The
    canonical case this feature exists for goes straight through one of them:
    account A is paired, the phone revokes the session, the next launch reads
    401 and clear_local_data() runs with no database open — messages.db keeps
    every chat and message of A (the divergence check's own docstring says so)
    while settings.json loses WA_phone_number_linked. Phone B then pairs,
    _wipe_local_data_if_another_number_linked() reads no recorded number, takes
    the "learn it, delete nothing" branch, and B's sync merges onto A. The
    merge this key exists to prevent, disarmed by its own cleanup, with no
    kill involved anywhere.

    save_full_state() raising is the same fault reached a second way:
    DatabaseBridgeTimeout/DatabaseBridgeClosed are swallowed by design there,
    so the messages stay on disk exactly as above.

    Keeping it armed is strictly better, not a trade: the check that runs
    right after prepare_sync() is the one that owns the no-database case, and
    it can only act on a number it can still read.
    """

    def test_no_database_open_keeps_the_number_and_writes_nothing(self):
        stub = _Stub()
        # Every connect.py call site: __init__ has not reached prepare_sync().
        del stub.db

        stub.clear_local_data()

        assert stub.settings["privateinfo"]["WA_phone_number_linked"] == "5511999999999"
        assert stub.saved == 0

    def test_a_database_that_refused_to_empty_keeps_the_number(self):
        stub = _Stub()

        def _timeout(data, clear_metadata=True):
            raise DatabaseBridgeTimeout("db-asyncio thread is not running")

        stub.db.save_full_state = _timeout

        stub.clear_local_data()

        assert stub.settings["privateinfo"]["WA_phone_number_linked"] == "5511999999999"
        assert stub.saved == 0

    def test_the_rest_of_the_wipe_still_happens_without_a_database(self, tmp_path):
        """Only the recorded number is conditional. The in-memory metadata and
        the media on disk belong to data that is going either way."""
        (tmp_path / "media_failed.json").write_text('{"3EB0ABC": 1700000000.0}')
        stub = _Stub()
        del stub.db

        stub.clear_local_data()

        assert stub.chats == {}
        for name in _METADATA:
            assert not getattr(stub, name), name
        assert not (tmp_path / "media_failed.json").exists()


class TestTheWipeReportsWhetherItEmptiedAnything:
    """One of the two conditions the WA_phone_number_linked drop is gated on
    — not the same one, since that drop also needs wipe_metadata — handed back
    to the caller, because one caller has to make a decision of its own one
    level up: _apply_another_number_wipe() records the NEWLY linked number the
    moment this returns, and a wipe that emptied nothing would leave the key
    naming account B while account A's messages are still in messages.db — no
    divergence left for any later pass to find, after the user has already been
    told A's conversations were deleted.

    Nothing else reads it. F5 (wipe_metadata=False) empties the message tables
    too, so it gets the same True and still drops no key; the flag decides what
    is emptied, not whether that is reported.
    """

    def test_an_emptied_database_reports_true(self):
        stub = _Stub()

        assert stub.clear_local_data() is True

    def test_no_database_open_reports_false(self):
        """Every connect.py call site: __init__ has not reached prepare_sync()
        yet, so messages.db is untouched."""
        stub = _Stub()
        del stub.db

        assert stub.clear_local_data() is False

    def test_a_save_full_state_that_raised_reports_false(self):
        """Routine mid-session: the user closes WinZapp while the daemon
        another-number-check thread is inside the wipe, and the bridge is gone
        before the write lands."""
        stub = _Stub()

        def _closed(data, clear_metadata=True):
            raise DatabaseBridgeClosed("database bridge is closed")

        stub.db.save_full_state = _closed

        assert stub.clear_local_data() is False

    def test_the_resync_path_reports_it_too(self):
        stub = _Stub()

        assert stub.clear_local_data(wipe_metadata=False) is True


class TestTheWipeAndItsOneCallerBoundTogether:
    """The seam: the real clear_local_data() driving the real
    _apply_another_number_wipe(), over a database that refuses the write.

    Both sides already have tests, and each side has its own stub for the
    other — so the wire between them was the one thing nothing covered.
    Deleting `return db_emptied` left every test above green (the caller reads
    None, which is falsy, and takes the same branch for the wrong reason), and
    a future stub of clear_local_data() answering None would do the same to the
    tests one level up. What has to hold is the whole path: a save_full_state()
    that raises reaches the decision not to record the newly linked number.
    """

    _A = "5511999999999"
    _B = "5521988887777"

    def _refusing_db(self, stub):
        """The routine mid-session failure: the user closes WinZapp while the
        daemon another-number-check thread is inside the wipe."""
        def _closed(data, clear_metadata=True):
            raise DatabaseBridgeClosed("database bridge is closed")

        stub.db.save_full_state = _closed

    def test_a_database_that_refused_the_write_keeps_the_previous_number(self):
        stub = _Stub()
        self._refusing_db(stub)

        stub._apply_another_number_wipe(self._B, teardown_ui=False,
                                        previous_digits=self._A)

        assert stub.settings["privateinfo"]["WA_phone_number_linked"] == self._A
        # And nothing was written, since the key already named A: the value on
        # disk is the value in memory.
        assert stub.saved == 0

    def test_no_database_open_keeps_it_too(self):
        """The other real route to False, and the one every connect.py call
        site takes: __init__ has not reached prepare_sync() yet."""
        stub = _Stub()
        del stub.db

        stub._apply_another_number_wipe(self._B, teardown_ui=False,
                                        previous_digits=self._A)

        assert stub.settings["privateinfo"]["WA_phone_number_linked"] == self._A

    def test_an_emptied_database_records_the_new_number(self):
        """The control, and the half that catches the return value going
        missing: with nothing handed back, this path stops recording anything
        at all and the account switch never completes."""
        stub = _Stub()

        stub._apply_another_number_wipe(self._B, teardown_ui=False,
                                        previous_digits=self._A)

        assert stub.db.calls == [True]
        assert stub.settings["privateinfo"]["WA_phone_number_linked"] == self._B

    def test_nothing_is_deleted_before_the_key_names_the_previous_account(
            self, tmp_path, monkeypatch):
        """The ordering, over the real methods: the second pass writes the
        previous number back BEFORE it starts deleting, not after.

        The failure it is written against is not instantaneous.
        save_full_state() raises, and clear_local_data() then sweeps media/ and
        voice_messages/ entry by entry — seconds on a real install — before it
        answers False, all of it on a daemon thread the shutdown does not wait
        for. Repaired afterwards, a process killed anywhere in that window left
        settings.json naming B while A's rows were still in messages.db, which
        every later pass reads as no divergence at all.
        """
        for subdir in ("media", "voice_messages"):
            folder = tmp_path / subdir
            folder.mkdir()
            (folder / "a.bin").write_bytes(b"x")

        stub = _Stub()
        stub.settings["privateinfo"]["WA_phone_number_linked"] = self._B

        persisted = []
        stub.save_settings = lambda: persisted.append(
            stub.settings["privateinfo"].get("WA_phone_number_linked"))

        seen = []

        def _record(label):
            seen.append((
                label,
                stub.settings["privateinfo"].get("WA_phone_number_linked"),
                list(persisted)))

        def _closed(data, clear_metadata=True):
            _record("db")
            raise DatabaseBridgeClosed("database bridge is closed")

        stub.db.save_full_state = _closed
        real_unlink = main_module.os.unlink

        def _unlink(path):
            _record("file")
            real_unlink(path)

        monkeypatch.setattr(main_module.os, "unlink", _unlink)

        stub._apply_another_number_wipe(self._B, teardown_ui=False,
                                        previous_digits=self._A)

        # Every destructive step of the pass, and not one of them ran under a
        # key naming B — in memory or in the file the next launch reads.
        assert [label for label, _, _ in seen] == ["db", "file", "file"]
        assert all(value == self._A for _, value, _ in seen), seen
        assert all(snapshot == [self._A] for _, _, snapshot in seen), seen
        assert persisted == [self._A]

    def test_the_previous_number_is_put_back_when_the_key_had_moved_on(self):
        """The second pass of a mid-session switch, end to end: the key already
        names B (the first pass recorded it), the database refuses, and A's
        rows are still there — so the key has to name A again or no later pass
        ever sees the divergence."""
        stub = _Stub()
        stub.settings["privateinfo"]["WA_phone_number_linked"] = self._B
        self._refusing_db(stub)

        stub._apply_another_number_wipe(self._B, teardown_ui=False,
                                        previous_digits=self._A)

        assert stub.settings["privateinfo"]["WA_phone_number_linked"] == self._A
        assert stub.saved == 1

    def test_the_ui_teardown_waits_for_the_key_too(self):
        """_teardown_conversation_ui() is not free: it ends in a 5 s
        ui_ready.wait(), and the case that spends all five is the user closing
        WinZapp — the MainLoop dies, the wx.CallAfter is never dispatched, and
        the shutdown does not wait for this daemon thread. Run before the
        re-arming, that was five seconds of the second pass with the key naming
        B over A's rows, and a process killed inside it leaves no divergence for
        any later pass to find."""
        stub = _Stub()
        stub.settings["privateinfo"]["WA_phone_number_linked"] = self._B
        self._refusing_db(stub)

        seen = []
        stub._teardown_conversation_ui = lambda: seen.append(
            stub.settings["privateinfo"].get("WA_phone_number_linked"))

        stub._apply_another_number_wipe(self._B, teardown_ui=True,
                                        previous_digits=self._A)

        assert seen == [self._A]


class TestAResyncKeepsEveryLocalActionTheUserTook:
    """wipe_metadata=False is F5. Refetching chats and messages from WhatsApp
    is the whole request; discarding the user's own cleared/deleted/archived/
    muted/blocked state on top of them is not, and used to happen."""

    def test_no_metadata_collection_is_touched(self):
        stub = _Stub()

        stub.clear_local_data(wipe_metadata=False)

        for name in _METADATA:
            assert getattr(stub, name), name

    def test_our_own_jid_survives(self):
        stub = _Stub()

        stub.clear_local_data(wipe_metadata=False)

        assert stub.my_jid == "5511999999999@s.whatsapp.net"
        assert stub.my_lid == "182736450192837@lid"

    def test_the_database_keeps_its_own_copy(self):
        stub = _Stub()

        stub.clear_local_data(wipe_metadata=False)

        assert stub.db.calls == [False]


class TestOneUndeletableFileDoesNotStrandTheRest:
    """The wipe runs on a daemon thread while a voice note may still be
    playing, and os.unlink on a file BASS holds open raises PermissionError.
    Caught around the whole os.listdir loop, that one file used to cost every
    file after it — the previous account's media, still on disk, in a folder
    the user is never shown."""

    def _populate(self, tmp_path):
        for subdir in ("media", "voice_messages"):
            folder = tmp_path / subdir
            folder.mkdir()
            for name in ("a", "b", "c"):
                (folder / f"{name}.bin").write_bytes(b"x")

    def test_every_other_file_still_goes(self, tmp_path, monkeypatch):
        self._populate(tmp_path)
        locked = tmp_path / "voice_messages" / "b.bin"
        real_unlink = main_module.os.unlink

        def _unlink(path):
            if str(path) == str(locked):
                raise PermissionError(32, "file is in use by another process")
            real_unlink(path)

        monkeypatch.setattr(main_module.os, "unlink", _unlink)
        stub = _Stub()

        stub.clear_local_data()

        assert sorted(p.name for p in (tmp_path / "media").iterdir()) == []
        assert sorted(p.name for p in (tmp_path / "voice_messages").iterdir()) == ["b.bin"]

    def test_an_undeletable_file_does_not_stop_the_next_folder(self, tmp_path, monkeypatch):
        """media/ is swept first, so a failure there used to be survivable by
        accident; make it the first folder that fails and the second one still
        has to be cleared."""
        self._populate(tmp_path)
        real_unlink = main_module.os.unlink

        def _unlink(path):
            if path.endswith("media\\a.bin") or path.endswith("media/a.bin"):
                raise PermissionError(32, "file is in use by another process")
            real_unlink(path)

        monkeypatch.setattr(main_module.os, "unlink", _unlink)
        stub = _Stub()

        stub.clear_local_data()

        assert [p.name for p in (tmp_path / "media").iterdir()] == ["a.bin"]
        assert list((tmp_path / "voice_messages").iterdir()) == []

    def test_a_folder_that_fails_whole_does_not_flood_the_log(
            self, tmp_path, monkeypatch, caplog):
        """log.log is truncated every launch and is the one file a user pastes
        into a bug report. A media/ folder an antivirus has locked fails on
        every entry, and there are thousands of them — one line each buries
        the whole rest of the run."""
        folder = tmp_path / "media"
        folder.mkdir()
        for i in range(20):
            (folder / f"{i}.bin").write_bytes(b"x")

        def _unlink(path):
            raise PermissionError(32, "file is in use by another process")

        monkeypatch.setattr(main_module.os, "unlink", _unlink)
        stub = _Stub()

        with caplog.at_level("ERROR"):
            stub.clear_local_data()

        per_file = [r for r in caplog.messages if "Failed to delete" in r]
        assert len(per_file) == MainWindow._MAX_MEDIA_DELETE_ERRORS_LOGGED
        # The count is what is not allowed to go missing with them.
        assert any("except 20 entries" in r for r in caplog.messages)


class TestMediaIsSweptBeforeTheKeyIsDropped:
    """Issue #200: a process killed anywhere between the media sweep and the
    key drop below now leaves the previous account's media already gone, not
    still on disk under a key that has already stopped naming it.

    Swept in the other order (as this method used to), a kill between the key
    drop and the sweep left settings.json with no linked number and
    messages.db already empty while the previous account's media files were
    still on disk — a state the next launch's "learn this number, delete
    nothing" branch (TestAWipeThatEmptiedNothingLeavesTheNumberArmed above)
    never revisits, since an empty database never trips the divergence check
    again: the media orphan was permanent. Swept first, as here, the same
    kill instead leaves the key still naming the previous account, so the
    divergence check fires again on the next launch and repeats the whole
    method — re-sweeping an already-empty media/voice_messages (a per-file
    no-op, nothing left to delete) before it ever reaches the key drop.
    """

    def _trace(self, stub, tmp_path):
        for subdir in ("media", "voice_messages"):
            folder = tmp_path / subdir
            folder.mkdir()
            (folder / "a.bin").write_bytes(b"x")

        seen = []
        real_unlink = main_module.os.unlink

        def _unlink(path):
            seen.append("media-swept")
            real_unlink(path)

        real_save_settings = stub.save_settings

        def _save_settings():
            seen.append("key-dropped")
            real_save_settings()

        return seen, _unlink, _save_settings

    def test_the_success_path_sweeps_media_before_dropping_the_key(
            self, tmp_path, monkeypatch):
        stub = _Stub()
        seen, _unlink, _save_settings = self._trace(stub, tmp_path)
        monkeypatch.setattr(main_module.os, "unlink", _unlink)
        stub.save_settings = _save_settings

        stub.clear_local_data()

        assert seen == ["media-swept", "media-swept", "key-dropped"]

    def test_a_resync_still_sweeps_media_though_no_key_is_ever_dropped(
            self, tmp_path, monkeypatch):
        """wipe_metadata=False: the reordering must not accidentally start
        gating the sweep on the block it was moved out from under."""
        stub = _Stub()
        seen, _unlink, _save_settings = self._trace(stub, tmp_path)
        monkeypatch.setattr(main_module.os, "unlink", _unlink)
        stub.save_settings = _save_settings

        stub.clear_local_data(wipe_metadata=False)

        assert seen == ["media-swept", "media-swept"]
