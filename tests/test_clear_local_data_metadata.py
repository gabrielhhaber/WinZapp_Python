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
             "_media_failed_ids")


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
