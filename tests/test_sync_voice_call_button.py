"""Regression coverage for hiding the voice/video call buttons on chats
WhatsApp can't call: groups, newsletters, broadcast, and the user's own
self-chat ("Eu"), which has no one on the other end to call.

ConversationsPanel is a wx.Panel and can't be instantiated without a running
wx.App, so the method under test is bound onto a plain stub carrying only the
attributes it touches, matching the pattern used throughout this test suite
(see test_close_conversation_for_panel_switch.py).
"""

from ui.conversations import ConversationsPanel

MY_JID = "5511999999999@s.whatsapp.net"


class _FakeWidget:
    def __init__(self):
        self._shown = None

    def Show(self, show=True):
        self._shown = bool(show)

    def IsShown(self):
        return self._shown

    def Layout(self):
        pass


class _FakeMainWindow:
    def _is_self_jid(self, jid):
        return bool(jid) and jid.split("@", 1)[0] == MY_JID.split("@", 1)[0]


class _Stub:
    _sync_voice_call_button = ConversationsPanel._sync_voice_call_button

    def __init__(self):
        self.main_window = _FakeMainWindow()
        self._voice_call_btn = _FakeWidget()
        self._video_call_btn = _FakeWidget()
        self.conversation_panel = _FakeWidget()

    def Layout(self):
        pass


def test_self_chat_hides_both_call_buttons():
    panel = _Stub()
    panel._sync_voice_call_button(MY_JID)
    assert panel._voice_call_btn.IsShown() is False
    assert panel._video_call_btn.IsShown() is False


def test_ordinary_one_to_one_shows_both_call_buttons():
    panel = _Stub()
    panel._sync_voice_call_button("5511888888888@s.whatsapp.net")
    assert panel._voice_call_btn.IsShown() is True
    assert panel._video_call_btn.IsShown() is True


def test_group_newsletter_broadcast_hide_both_call_buttons():
    panel = _Stub()
    for jid in (
        "120363012345678901@g.us",
        "1234567890@newsletter",
        "status@broadcast",
    ):
        panel._sync_voice_call_button(jid)
        assert panel._voice_call_btn.IsShown() is False
        assert panel._video_call_btn.IsShown() is False
