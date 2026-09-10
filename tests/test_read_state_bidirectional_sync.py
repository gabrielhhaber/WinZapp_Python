"""Regression tests for read-state synchronization across linked devices."""

from main import MainWindow, reconcile_snapshot_unread


JID = "5511999999999@s.whatsapp.net"


def test_current_remote_zero_clears_unread_from_phone_even_when_archived():
    assert reconcile_snapshot_unread(0, 4, 2000, 2000) == 0


def test_snapshot_older_than_a_live_arrival_cannot_clear_it():
    assert reconcile_snapshot_unread(0, 1, 1000, 1001) == 1


def test_unsynced_live_chat_keeps_its_local_count():
    assert reconcile_snapshot_unread(0, 2, 2000, 2000, unsynced=True) == 2


def test_higher_remote_count_is_always_accepted():
    assert reconcile_snapshot_unread(5, 2, 2000, 1000) == 5


class _RollbackStub:
    _restore_unread_after_send_seen_failure = (
        MainWindow._restore_unread_after_send_seen_failure
    )
    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    _restore_unread_after_mark_unread_failure = (
        MainWindow._restore_unread_after_mark_unread_failure
    )
    # Undoing a read (or marking a chat unread) also drops the anchor that
    # read installed — see tests/test_unread_reread_race.py.
    _drop_unread_local_read_anchor = MainWindow._drop_unread_local_read_anchor

    def __init__(self, *, archived=False):
        self.chats = {
            JID: {
                "unreadCount": 0,
                "t": 2000,
                "archived": archived,
            }
        }
        self._locally_read_at = {JID: 2000}
        self._new_since_read = {JID: 0}
        self._unread_read_anchors = {JID}
        self.persisted = 0
        self.saved = []
        self.refreshed = []
        self.list_refreshes = 0

    def _persist_locally_read_at(self):
        self.persisted += 1

    def _schedule_save(self, dirty_jid=None):
        self.saved.append(dirty_jid)

    def _refresh_chat_row_in_list(self, jid):
        self.refreshed.append(jid)

    def _schedule_set_chats(self):
        self.list_refreshes += 1


def test_failed_send_seen_restores_normal_chat_unread_state():
    stub = _RollbackStub()

    stub._restore_unread_after_send_seen_failure(JID, 3, 2000)

    assert stub.chats[JID]["unreadCount"] == 3
    assert JID not in stub._locally_read_at
    # The read is being undone, so the ceiling it installed goes with it —
    # otherwise the restored backlog is clamped back down to a counter no read
    # backs (see tests/test_unread_reread_race.py).
    assert JID not in stub._unread_read_anchors
    assert stub.saved == [JID]


def test_failed_send_seen_restores_archived_chat_unread_state():
    stub = _RollbackStub(archived=True)

    stub._restore_unread_after_send_seen_failure(JID, 3, 2000)

    assert stub.chats[JID]["unreadCount"] == 3
    assert stub.chats[JID]["archived"] is True


def test_failed_send_seen_does_not_overwrite_a_new_message():
    stub = _RollbackStub()
    stub.chats[JID]["unreadCount"] = 1
    stub.chats[JID]["t"] = 2001
    stub._new_since_read[JID] = 1

    stub._restore_unread_after_send_seen_failure(JID, 3, 2000)

    assert stub.chats[JID]["unreadCount"] == 1
    assert stub._locally_read_at[JID] == 2000


class _MarkUnreadStub(_RollbackStub):
    mark_conversation_as_unread = MainWindow.mark_conversation_as_unread

    def __init__(self):
        super().__init__()
        self.chats[JID]["unreadCount"] = 0
        self.remote_changes = []
        self.set_chats_calls = 0

    def set_chats(self):
        self.set_chats_calls += 1

    def _sync_conversation_read_state(self, jid, unread, on_failure):
        self.remote_changes.append((jid, unread, on_failure))


def test_mark_unread_clears_stale_read_ack_and_syncs_to_whatsapp(monkeypatch):
    monkeypatch.setattr("main.wx.CallAfter", lambda fn, *args: fn(*args))
    stub = _MarkUnreadStub()

    stub.mark_conversation_as_unread(JID)

    assert stub.chats[JID]["unreadCount"] == 1
    assert JID not in stub._locally_read_at
    assert JID not in stub._unread_read_anchors
    assert stub.remote_changes[0][:2] == (JID, True)
    assert stub.saved == [JID]


def test_failed_remote_mark_unread_rolls_back_if_chat_did_not_change():
    stub = _MarkUnreadStub()
    stub.chats[JID]["unreadCount"] = 1

    stub._restore_unread_after_mark_unread_failure(JID, 0, 2000)

    assert stub.chats[JID]["unreadCount"] == 0
    assert stub.saved == [JID]


def test_failed_remote_mark_unread_does_not_overwrite_new_activity():
    stub = _MarkUnreadStub()
    stub.chats[JID].update(unreadCount=2, t=2001)

    stub._restore_unread_after_mark_unread_failure(JID, 0, 2000)

    assert stub.chats[JID]["unreadCount"] == 2
    assert stub.saved == []


def test_node_read_state_endpoint_supports_both_states_and_checks_result():
    source = (
        __import__("pathlib").Path(__file__).parents[1]
        / "client/api_patches/src/controller/deviceController.ts"
    ).read_text(encoding="utf-8")

    send_seen = source[source.index("export async function sendSeen"):]
    assert "WPP.chat.markIsUnread" in send_seen
    assert "WPP.chat.markIsRead" in send_seen
    assert "if (!result) throw new Error" in send_seen
    assert "markUnread: Boolean(unread)" in send_seen
