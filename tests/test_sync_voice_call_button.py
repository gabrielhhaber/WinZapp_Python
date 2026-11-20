"""Regression coverage for the voice/video call buttons on chats WhatsApp
cannot call: newsletters, broadcast, and the user's own self-chat ("Eu"),
which has no one on the other end to call. Groups are callable, so they show
both buttons like a one-to-one chat.

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

    def HasFocus(self):
        return False


class _FakeMainWindow:
    def _is_self_jid(self, jid):
        return bool(jid) and jid.split("@", 1)[0] == MY_JID.split("@", 1)[0]


class _Stub:
    _sync_voice_call_button = ConversationsPanel._sync_voice_call_button
    _update_call_start_buttons = ConversationsPanel._update_call_start_buttons
    _chat_call_kind = ConversationsPanel._chat_call_kind
    _same_call_chat = ConversationsPanel._same_call_chat

    def _watch_group_calls(self, jid):
        self.watched_group_jid = jid

    def __init__(self):
        self.main_window = _FakeMainWindow()
        self._voice_call_btn = _FakeWidget()
        self._video_call_btn = _FakeWidget()
        self._group_call_unavailable_btn = _FakeWidget()
        self._group_call_join_btn = _FakeWidget()
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


def test_group_shows_both_call_buttons():
    panel = _Stub()
    panel._sync_voice_call_button("120363012345678901@g.us")
    assert panel._voice_call_btn.IsShown() is True
    assert panel._video_call_btn.IsShown() is True


def test_newsletter_and_broadcast_hide_both_call_buttons():
    panel = _Stub()
    for jid in (
        "1234567890@newsletter",
        "status@broadcast",
    ):
        panel._sync_voice_call_button(jid)
        assert panel._voice_call_btn.IsShown() is False
        assert panel._video_call_btn.IsShown() is False


# ── Groups over the call limit ───────────────────────────────────────────────

from core.call_logic import MAX_GROUP_CALL_PARTICIPANTS, group_too_large_for_call


def test_the_limit_is_thirty_two_participants():
    assert MAX_GROUP_CALL_PARTICIPANTS == 32
    assert not group_too_large_for_call(32)
    assert group_too_large_for_call(33)
    assert not group_too_large_for_call(None)
    assert not group_too_large_for_call(0)


class _LimitStub(_Stub):
    _set_group_call_limit = ConversationsPanel._set_group_call_limit
    _group_call_refused = ConversationsPanel._group_call_refused
    _on_voice_call = ConversationsPanel._on_voice_call
    _on_video_call = ConversationsPanel._on_video_call

    def __init__(self, jid):
        super().__init__()
        self.conversation = {"remoteJid": jid}
        self.conversation_name = "G"
        self._group_call_blocked_jid = ""
        spoken = self.spoken = []
        started = self.started = []

        class _I18n:
            def t(self, key):
                return "note {limit}" if key == "group_call_unavailable_note" else key

        class _MW(_FakeMainWindow):
            i18n = _I18n()

            def output(self, text, interrupt=False):
                spoken.append(text)

            def start_voice_call(self, jid, name):
                started.append(jid)

            start_video_call = start_voice_call

        self.main_window = _MW()


def test_a_group_over_the_limit_swaps_the_call_buttons_for_the_note_button():
    jid = "120363000000000000@g.us"
    panel = _LimitStub(jid)
    panel._set_group_call_limit(jid, 33)
    assert panel._voice_call_btn.IsShown() is False
    assert panel._video_call_btn.IsShown() is False
    assert panel._group_call_unavailable_btn.IsShown() is True


def test_a_group_within_the_limit_keeps_the_call_buttons():
    jid = "120363000000000000@g.us"
    panel = _LimitStub(jid)
    panel._set_group_call_limit(jid, 32)
    assert panel._voice_call_btn.IsShown() is True
    assert panel._group_call_unavailable_btn.IsShown() is False


def test_the_limit_of_a_group_no_longer_open_is_ignored():
    panel = _LimitStub("120363000000000000@g.us")
    panel._set_group_call_limit("999@g.us", 40)
    assert panel._group_call_blocked_jid == ""


def test_the_call_shortcuts_say_why_instead_of_calling_an_oversized_group():
    jid = "120363000000000000@g.us"
    panel = _LimitStub(jid)
    panel._set_group_call_limit(jid, 40)
    panel._on_voice_call()
    panel._on_video_call()
    assert panel.started == []
    assert panel.spoken == ["note 32"] * 2


def test_the_call_shortcuts_still_call_a_group_within_the_limit():
    jid = "120363000000000000@g.us"
    panel = _LimitStub(jid)
    panel._set_group_call_limit(jid, 10)
    panel._on_voice_call()
    assert panel.started == [jid]
