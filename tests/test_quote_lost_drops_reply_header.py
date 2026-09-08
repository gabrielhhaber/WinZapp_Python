"""Tests for the quote-lost fallback in text sends.

Reported live: when a quoted (reply) text send fails server-side,
send_text_message() retries as a plain send without the quote — but the
virtual message kept its reply contextInfo, so the conversation row kept
reading "Eu, respondendo a MK Digital: teste" even though the message was
delivered with no quote at all.

The fix threads a quote_lost flag from send_text_message() (which knows it
stripped the quote) through MessageQueue._on_message_sent() down to
ConversationsPanel._mark_message_sent(), which drops the contextInfo before
re-rendering the row. The echo-matching path in MainWindow.on_new_message()
never touches contextInfo, so without the explicit drop the stale reply
header would live on forever.

MainWindow is a wx.Frame and cannot be instantiated without a running
wx.App, so the chain is exercised against small stubs — same approach as
tests/test_failed_send_preview.py.
"""

from main import MainWindow
from ui.conversations import ConversationsPanel


class _FakeSound:
    def __init__(self):
        self.played = False

    def play(self):
        self.played = True


class _FakeList:
    def __init__(self):
        self.renders = []
        self.set_calls = []

    def SetItemText(self, index, text):
        self.set_calls.append((index, text))
        while len(self.renders) <= index:
            self.renders.append("")
        self.renders[index] = text


class _FakeMainWindow:
    def __init__(self):
        self.message_sent_sound = _FakeSound()
        self.schedule_set_chats_calls = 0

    def _schedule_save(self, dirty_jid=None):
        pass

    def _schedule_set_chats(self):
        self.schedule_set_chats_calls += 1


def _make_panel(msg):
    panel = ConversationsPanel.__new__(ConversationsPanel)
    panel._sorted_messages = [msg]
    panel._played_sent_local_ids = set()
    panel._outgoing_virtual_messages = {}
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
        "contextInfo": {
            "stanzaId": "ABC",
            "participant": "p@s.whatsapp.net",
            "quotedMessage": {"conversation": "citação original"},
            "_quotedFromMe": False,
        },
    }
    msg.update(overrides)
    return msg


class TestMarkMessageSentQuoteLost:
    def test_quote_lost_drops_context_info_from_the_row(self):
        msg = _make_msg()
        panel = _make_panel(msg)

        ConversationsPanel._mark_message_sent(panel, "loc-1", real_id="REAL", quote_lost=True)

        assert "contextInfo" not in msg
        assert msg["_local_pending"] is False

    def test_quote_lost_rereads_the_row(self):
        msg = _make_msg()
        panel = _make_panel(msg)

        ConversationsPanel._mark_message_sent(panel, "loc-1", real_id="REAL", quote_lost=True)

        assert panel.messages_list.set_calls == [(0, "RENDERED:loc-1")]

    def test_without_quote_lost_context_info_is_kept(self):
        msg = _make_msg()
        panel = _make_panel(msg)

        ConversationsPanel._mark_message_sent(panel, "loc-1", real_id="REAL", quote_lost=False)

        assert "contextInfo" in msg
        assert msg["_local_pending"] is False


class TestOnMessageSentForwardsQuoteLost:
    class _FakeConversationsPanel:
        def __init__(self):
            self.calls = []

        def _mark_message_sent(self, local_id, real_id=None, quote_lost=False):
            self.calls.append((local_id, real_id, quote_lost))

        def _is_cancelled_pending(self, local_id):
            # _on_message_sent() asks this first, to route a send that outran
            # the user's own cancellation (see tests/test_message_cancel_race.py).
            return False

    class _Stub:
        _on_message_sent = MainWindow._on_message_sent
        _is_cancelled_send = MainWindow._is_cancelled_send

        def __init__(self):
            self.conversations_panel = TestOnMessageSentForwardsQuoteLost._FakeConversationsPanel()

    def test_quote_lost_is_forwarded_to_the_panel(self):
        mw = self._Stub()
        mw._on_message_sent("loc-1", real_id="REAL", remote_jid="j@c.us", quote_lost=True)
        assert mw.conversations_panel.calls == [("loc-1", "REAL", True)]

    def test_default_quote_lost_is_false(self):
        mw = self._Stub()
        mw._on_message_sent("loc-1", real_id="REAL", remote_jid="j@c.us")
        assert mw.conversations_panel.calls == [("loc-1", "REAL", False)]