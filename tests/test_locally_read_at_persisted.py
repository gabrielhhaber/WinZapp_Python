"""Tests for the read-ack map (_locally_read_at) surviving a restart.

mark_conversation_as_read() records, per chat, the last-activity timestamp
the conversation had at the moment WinZapp cleared its badge locally.
on_chat_unread_update() and get_remote_chats() both consult it to tell a
genuinely new unread count apart from WhatsApp Web's own server-side read
receipt simply not having caught up with our /send-seen yet.

That map used to live only in memory. The stale server-side count it
protects against does NOT disappear when WinZapp quits, so after a restart
the guard was gone while the stale number was still there: the next
list-chats snapshot reported the pre-read total against a local 0, which is
higher, so it was accepted — and messages the user had already read came
back as unread. Reported live as "algumas mensagens ja lidas voltam a
aparecer como nao lidas".

MainWindow is a wx.Frame and can't be instantiated without a running
wx.App, so the methods under test are bound onto a plain stub — same
approach as the rest of this suite.
"""

import pytest

from main import MainWindow


@pytest.fixture(autouse=True)
def _no_send_seen(monkeypatch):
    """mark_conversation_as_read() fires its /send-seen POST on a background
    thread whenever the chat had unread messages. None of these tests are
    about that call, and letting it run would aim a real HTTP request at
    whatever happens to be listening on port 6300."""
    class _NoopThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr("main.threading.Thread", _NoopThread)
    monkeypatch.setattr("main.wx.CallAfter", lambda *a, **k: None)


class _FakeDB:
    def __init__(self, metadata=None):
        self.metadata = dict(metadata or {})
        self.writes = []

    def set_metadata_json(self, key, value):
        self.writes.append((key, value))
        self.metadata[key] = value

    def get_metadata_json(self, key, default=None):
        return self.metadata.get(key, default)


class _Stub:
    mark_conversation_as_read = MainWindow.mark_conversation_as_read
    _sync_conversation_read_state = MainWindow._sync_conversation_read_state
    _persist_locally_read_at = MainWindow._persist_locally_read_at
    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    # on_chat_unread_update looks the chat up through this rather than
    # self.chats.get() directly, so the @lid / phone identities of an incoming
    # event get bridged first — see tests/test_chats_update_lid_resolution.py.
    # The stub carries no _lid_to_phone/_phone_to_lid, which the real method
    # reads via getattr defaults; a plain phone JID resolves on the first
    # lookup anyway.
    _resolve_chat_for_event = MainWindow._resolve_chat_for_event
    # mark_conversation_as_read() also records that _new_since_read now counts
    # from a real read — see tests/test_unread_reread_race.py.
    _anchor_unread_to_local_read = MainWindow._anchor_unread_to_local_read
    _unread_anchored_to_local_read = MainWindow._unread_anchored_to_local_read

    def __init__(self, chat, db=None):
        self.chats = {JID: chat}
        self.db = db
        self.settings = {}
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.token = "tok"

    def _schedule_save(self, dirty_jid=None):
        pass

    def _refresh_chat_row_in_list(self, jid):
        pass

    def _schedule_set_chats(self):
        pass


JID = "5511999999999@s.whatsapp.net"


def _chat(t=1000, unread=3):
    return {"unreadCount": unread, "t": t, "messages": {"messages": {"records": []}}}


class TestMarkAsReadPersistsTheAck:
    def test_the_read_ack_is_written_to_db_metadata(self):
        db = _FakeDB()
        stub = _Stub(_chat(t=1234), db=db)

        stub.mark_conversation_as_read(JID, force=False)

        assert ("locally_read_at", {JID: 1234}) in db.writes

    def test_the_ack_records_the_chats_own_last_activity_timestamp(self):
        db = _FakeDB()
        stub = _Stub(_chat(t=777), db=db)

        stub.mark_conversation_as_read(JID, force=False)

        assert stub._locally_read_at == {JID: 777}
        assert db.metadata["locally_read_at"] == {JID: 777}
        # The other half of what this read establishes: from here on the
        # arrivals counter for this chat means "since a read", which is what
        # lets on_chat_unread_update() clamp a server total to it. Asserted
        # here because this is the only site that sets it — without this line
        # the call could be deleted with the whole suite still green, and the
        # clamp would silently stop protecting anything.
        assert stub._unread_anchored_to_local_read(JID)

    def test_a_chat_with_no_timestamp_records_zero_instead_of_crashing(self):
        db = _FakeDB()
        chat = _chat()
        chat.pop("t")
        stub = _Stub(chat, db=db)

        stub.mark_conversation_as_read(JID, force=False)

        assert db.metadata["locally_read_at"] == {JID: 0}


class TestPersistIsSafeWithoutADatabase:
    def test_no_database_yet_is_a_silent_no_op(self):
        stub = _Stub(_chat(), db=None)
        stub._locally_read_at = {JID: 5}

        stub._persist_locally_read_at()  # must not raise

    def test_a_failing_database_write_does_not_propagate(self):
        class _BrokenDB:
            def set_metadata_json(self, key, value):
                raise RuntimeError("db is gone")

        stub = _Stub(_chat(), db=_BrokenDB())
        stub._locally_read_at = {JID: 5}

        stub._persist_locally_read_at()  # logged and swallowed


class TestRestoredAckStillSuppressesAStaleServerCount:
    """End-to-end shape of the bug: the ack is written in one run, read back
    in the next, and the stale count the server is still reporting is
    rejected exactly as it would have been before the restart."""

    def test_a_restored_ack_is_honoured_by_on_chat_unread_update(self):
        # Run 1 — user opens and reads the conversation.
        db = _FakeDB()
        first = _Stub(_chat(t=1000, unread=3), db=db)
        first.mark_conversation_as_read(JID)
        assert first.chats[JID]["unreadCount"] == 0

        # Run 2 — fresh process, map restored from the same DB metadata.
        restored = {
            k: int(v or 0)
            for k, v in dict(db.get_metadata_json("locally_read_at", {})).items()
        }
        assert restored == {JID: 1000}

        chat = _chat(t=1000, unread=0)
        second = _Stub(chat, db=db)
        second._locally_read_at = restored
        second._new_since_read = {}
        second._initial_sync_running = False
        second._sync_completed = True
        second.conversations_panel = None
        second.on_chat_unread_update = MainWindow.on_chat_unread_update.__get__(second)
        second._remote_read_confirmed = MainWindow._remote_read_confirmed
        second._schedule_set_chats = lambda: None

        # WhatsApp Web still reports the pre-read total for a chat whose own
        # last activity has not moved since the read.
        second.on_chat_unread_update(JID, 3)

        assert chat["unreadCount"] == 0

    def test_without_the_restored_ack_the_stale_count_comes_back(self):
        """The failure mode itself, pinned so the fix can't silently regress
        into "the map is written but never read back"."""
        chat = _chat(t=1000, unread=0)
        stub = _Stub(chat, db=_FakeDB())
        stub._locally_read_at = {}  # what a restart used to leave behind
        stub._new_since_read = {}
        stub._initial_sync_running = False
        stub._sync_completed = True
        stub.conversations_panel = None
        stub.on_chat_unread_update = MainWindow.on_chat_unread_update.__get__(stub)
        stub._remote_read_confirmed = MainWindow._remote_read_confirmed
        stub._schedule_set_chats = lambda: None

        stub.on_chat_unread_update(JID, 3)

        assert chat["unreadCount"] == 3
