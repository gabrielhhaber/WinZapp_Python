"""Tests for the _outgoing_virtual_messages memory leak fix.

Reported live: WinZapp's memory usage kept growing the longer the app ran.
ConversationsPanel._outgoing_virtual_messages indexes every outgoing virtual
message by its local_id (added in _register_pending_message, right when the
composer hands a new message to MessageQueue) so navigate_to_conversation()
can restore a still-pending bubble when the user reopens a conversation
before the DB has caught up. But nothing ever removed an entry once the
message was resolved — sent, permanently failed, left unconfirmed after a
timeout, or cancelled by the user while still in flight — so the dict grew
by one entry, forever, for every single message ever sent in the process's
lifetime.

The lookup is only ever consulted for rows that are still `_local_pending`
(see navigate_to_conversation()), and the WebSocket-echo reconciliation in
MainWindow.on_new_message() matches pending sends by scanning
_sorted_messages/records directly (by message type), never through this
dict — so removing a resolved entry here cannot break either mechanism.

Same test-stub approach as tests/test_quote_lost_drops_reply_header.py:
ConversationsPanel is a wx.Panel and can't be instantiated without a running
wx.App, so its methods are exercised via __new__() against a minimal stub.
"""

from ui.conversations import ConversationsPanel


class _FakeList:
    def SetItemText(self, index, text):
        pass

    def RefreshItem(self, index):
        pass


class _FakeMainWindow:
    def _schedule_save(self, dirty_jid=None):
        pass

    def _schedule_set_chats(self):
        pass


def _make_panel(msg):
    panel = ConversationsPanel.__new__(ConversationsPanel)
    panel._sorted_messages = [msg]
    panel._played_sent_local_ids = set()
    panel._outgoing_virtual_messages = {"loc-1": msg}
    panel._media_upload_progress = {}
    panel._upload_stages_seen = {}
    panel._media_transfer_started = set()
    panel._hide_media_transfer_gauge = lambda: None
    panel.messages_list = _FakeList()
    panel.conversation = {"remoteJid": "j@c.us"}
    panel.main_window = _FakeMainWindow()
    panel._render_message_line = lambda m, **kw: "RENDERED:" + str(m.get("_local_id"))
    return panel


def _make_msg(**overrides):
    msg = {
        "_local_id": "loc-1",
        "_local_pending": True,
        "key": {"id": "loc-1", "fromMe": True, "remoteJid": "j@c.us"},
        "messageType": "conversation",
        "message": {"conversation": "teste"},
        "messageTimestamp": 1,
        "pushName": "",
    }
    msg.update(overrides)
    return msg


class TestMarkMessageSentClearsTheIndex:
    def test_pops_the_local_id_once_sent(self):
        msg = _make_msg()
        panel = _make_panel(msg)

        ConversationsPanel._mark_message_sent(panel, "loc-1", real_id="REAL")

        assert "loc-1" not in panel._outgoing_virtual_messages

    def test_message_row_is_still_updated_in_place(self):
        """The dict was only an index — the message dict itself must keep
        living inside _sorted_messages exactly as before."""
        msg = _make_msg()
        panel = _make_panel(msg)

        ConversationsPanel._mark_message_sent(panel, "loc-1", real_id="REAL")

        assert msg["_local_pending"] is False
        assert msg["key"]["id"] == "REAL"


class TestMarkMessageFailedClearsTheIndex:
    def test_pops_the_local_id_once_failed(self):
        msg = _make_msg()
        panel = _make_panel(msg)

        ConversationsPanel._mark_message_failed(panel, "loc-1")

        assert "loc-1" not in panel._outgoing_virtual_messages
        assert msg["_send_failed"] is True


class TestMarkMessageUnconfirmedClearsTheIndex:
    def test_pops_the_local_id_once_unconfirmed(self):
        msg = _make_msg()
        panel = _make_panel(msg)

        ConversationsPanel._mark_message_unconfirmed(panel, "loc-1")

        assert "loc-1" not in panel._outgoing_virtual_messages
        assert msg["_send_unconfirmed"] is True
