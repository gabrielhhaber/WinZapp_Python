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
"""

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

    def save_full_state(self, data, clear_metadata=True):
        self.calls.append(clear_metadata)


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

    clear_local_data = MainWindow.clear_local_data


_METADATA = ("_deleted_chats", "_archived_chats", "_pinned_chats",
             "_muted_chats", "_blocked_contacts", "_presence_pushname_map",
             "_locally_read_at")


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
