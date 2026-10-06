"""Alt+Shift+P: jump to the previous message that replies to me.

The counterpart of Alt+Shift+M (previous mention). It walks backwards through
the messages that quote one of MY messages and wraps to the newest after the
oldest. What counts as a reply to me follows _get_quoted_sender(): the quote's
participant says who wrote the quoted message; without one, the quoted
message's own fromMe decides, and in a group a quote that cannot be resolved
does not count (a reply to a third member must never be reported as mine).

ConversationsPanel is a wx.Panel, so the methods run on a small stub, as in
tests/test_mentions_and_reactions.py.
"""

import pytest
import wx

from main import MainWindow
from main_window.message_rules import quote_is_of_my_message
from tests.mnemonics import built_table, load_strings
from ui.conversation_panel.accelerators import AcceleratorsMixin
from ui.conversations import ConversationsPanel
from ui.dialogs.shortcuts_dialog import ShortcutsDialog
from tests.test_mentions_and_reactions import ME, SOMEONE, _FakeList, _FakeMainWindow

GROUP = "grupo@g.us"
PRIVATE = "5511911111111@s.whatsapp.net"


class _Panel:
    _is_separator = ConversationsPanel._is_separator
    _get_context_info = ConversationsPanel._get_context_info
    _is_reply_to_me = ConversationsPanel._is_reply_to_me
    _on_accel_replies = ConversationsPanel._on_accel_replies

    def __init__(self, messages=(), focused=-1, chat=GROUP, open_chat=True):
        self.main_window = _FakeMainWindow()
        self.main_window._is_self_jid = lambda jid: jid == ME
        self._sorted_messages = list(messages)
        self.messages_list = _FakeList(focused=focused)
        self.conversation = {"remoteJid": chat} if open_chat else None


def _mine(mid="mine"):
    return {"key": {"id": mid, "fromMe": True}, "message": {"conversation": "oi"}}


def _reply(stanza, participant=None, mid="r", from_me=False):
    ctx = {"stanzaId": stanza, "quotedMessage": {"conversation": "oi"}}
    if participant:
        ctx["participant"] = participant
    return {"key": {"id": mid, "fromMe": from_me},
            "message": {"extendedTextMessage": {"text": "sim", "contextInfo": ctx}}}


def _plain(mid="p"):
    return {"key": {"id": mid}, "message": {"conversation": "oi"}}


class TestWhatCountsAsAReplyToMe:
    def test_the_quote_names_me_as_its_author(self):
        assert _Panel()._is_reply_to_me(_reply("m1", participant=ME)) is True

    def test_the_quote_names_someone_else(self):
        assert _Panel()._is_reply_to_me(_reply("m1", participant=SOMEONE)) is False

    def test_without_a_participant_the_quoted_message_decides(self):
        panel = _Panel(messages=[_mine("m1")])
        assert panel._is_reply_to_me(_reply("m1")) is True
        other = {"key": {"id": "m2", "fromMe": False}, "message": {"conversation": "x"}}
        panel = _Panel(messages=[other])
        assert panel._is_reply_to_me(_reply("m2")) is False

    def test_a_private_reply_to_a_quote_that_is_not_loaded_is_to_me(self):
        """Baileys leaves the participant empty for 1:1 replies, and there the
        other party can only be answering me."""
        assert _Panel(chat=PRIVATE)._is_reply_to_me(_reply("gone")) is True

    def test_a_group_reply_to_a_quote_that_is_not_loaded_is_not_counted(self):
        """Guessing "to me" here is how a reply to a third member was once
        shown as a reply to the user."""
        assert _Panel(chat=GROUP)._is_reply_to_me(_reply("gone")) is False

    def test_my_own_replies_never_count(self):
        assert _Panel()._is_reply_to_me(_reply("m1", participant=ME, from_me=True)) is False

    def test_a_message_that_quotes_nothing_does_not_count(self):
        assert _Panel()._is_reply_to_me(_plain()) is False
        assert _Panel()._is_reply_to_me("not a message") is False

    def test_an_image_reply_counts_like_a_text_one(self):
        msg = {"key": {"id": "r"},
               "message": {"imageMessage": {"contextInfo": {
                   "stanzaId": "m1", "participant": ME,
                   "quotedMessage": {"conversation": "oi"}}}}}
        assert _Panel()._is_reply_to_me(msg) is True


class TestTheSharedRule:
    """message_rules.quote_is_of_my_message(), also used by the notification
    side (MainWindow._is_reply_or_mention_of_me)."""

    @staticmethod
    def _never(stanza_id):
        raise AssertionError("the participant settles it")

    def test_a_participant_settles_it_without_looking_the_message_up(self):
        is_me = lambda jid: jid == ME
        assert quote_is_of_my_message({"participant": ME, "stanzaId": "x"}, is_me, self._never) is True
        assert quote_is_of_my_message({"participant": SOMEONE, "stanzaId": "x"}, is_me, self._never) is False

    def test_without_a_participant_the_lookup_answers(self):
        for answer in (True, False, None):
            assert quote_is_of_my_message({"stanzaId": "x"}, lambda j: False,
                                          lambda stanza_id: answer) is answer

    def test_no_participant_and_no_id_cannot_be_told(self):
        assert quote_is_of_my_message({"quotedMessage": {}}, lambda j: False, self._never) is None


class TestMeUnderEveryJidForm:
    """The quote's participant arrives as a phone JID, an @lid or with a
    Baileys device suffix; the real MainWindow._is_self_jid has to see me in
    each, or a reply to me is missed."""

    class _MW:
        _is_self_jid = MainWindow._is_self_jid
        _phone_digits_equivalent = staticmethod(MainWindow._phone_digits_equivalent)
        my_jid = "5511900000000@s.whatsapp.net"
        my_lid = "777000111@lid"
        _lid_to_phone = {"888000222@lid": "5511900000000@s.whatsapp.net"}
        i18n = None

    @pytest.mark.parametrize("participant", [
        "5511900000000@s.whatsapp.net",
        "5511900000000:12@s.whatsapp.net",
        "5511900000000@c.us",
        "551100000000@s.whatsapp.net",      # the same number without the 9th digit
        "777000111@lid",
        "777000111:3@lid",
        "888000222@lid",
    ])
    def test_a_reply_to_me_is_found(self, participant):
        panel = _Panel()
        panel.main_window = self._MW()
        assert panel._is_reply_to_me(_reply("m1", participant=participant)) is True

    @pytest.mark.parametrize("participant", ["5511911111111@s.whatsapp.net", "999@lid"])
    def test_a_reply_to_someone_else_is_not(self, participant):
        panel = _Panel()
        panel.main_window = self._MW()
        assert panel._is_reply_to_me(_reply("m1", participant=participant)) is False


class TestTheJump:
    def test_nothing_to_find_says_so(self):
        panel = _Panel(messages=[_plain(), _reply("m1", participant=SOMEONE)])
        panel._on_accel_replies(None)
        assert panel.main_window.announced == ["no_replies_found"]
        assert panel.messages_list.focus_calls == []

    def test_a_reply_is_focused_and_announced(self):
        panel = _Panel(messages=[_plain(), _reply("m1", participant=ME)])
        panel._on_accel_replies(None)
        assert panel.messages_list.focus_calls == [1]
        assert panel.main_window.announced == ["jumped_to_reply"]

    def test_repeated_presses_go_back_and_wrap_to_the_newest(self):
        messages = [_reply("a", participant=ME, mid="r0"), _plain(),
                    _reply("b", participant=ME, mid="r2"), _plain()]
        panel = _Panel(messages=messages, focused=3)
        for _ in range(3):
            panel._on_accel_replies(None)
        assert panel.messages_list.focus_calls == [2, 0, 2]

    def test_separator_rows_are_skipped(self):
        separator = {"_type": "unread_separator", "count": 2}
        panel = _Panel(messages=[separator, _reply("a", participant=ME)])
        panel._on_accel_replies(None)
        assert panel.messages_list.focus_calls == [1]

    def test_no_open_conversation_does_nothing(self):
        panel = _Panel(messages=[_reply("a", participant=ME)], open_chat=False)
        panel._on_accel_replies(None)
        assert panel.main_window.announced == [] and panel.messages_list.focus_calls == []


class TestTheKeyIsWiredAndDocumented:
    def test_alt_shift_p_reaches_the_handler(self, monkeypatch):
        """The open conversation's real table, built on a recording stub."""
        panel = built_table(AcceleratorsMixin.create_accel_conversation,
                            load_strings("pt-BR"), monkeypatch)
        alt_shift = wx.ACCEL_ALT | wx.ACCEL_SHIFT
        assert panel.handler_for(alt_shift, ord("P")) is panel._on_accel_replies
        assert panel.handler_for(alt_shift, ord("M")) is panel._on_accel_mentions

    def test_it_is_listed_in_the_f1_shortcuts(self):
        class _I18n:
            def t(self, key): return key
        text = ShortcutsDialog._build_text(_I18n())
        assert "shortcut_alt_shift_p_label" in text
        assert text.index("shortcut_alt_shift_m_label") < text.index("shortcut_alt_shift_p_label")

    @pytest.mark.parametrize("key", ["shortcut_alt_shift_p_label", "no_replies_found",
                                     "jumped_to_reply"])
    def test_the_texts_exist_in_every_locale(self, key):
        import json
        from app_paths import resource_path
        with open(resource_path("languages", "language_map.json"), encoding="utf-8") as f:
            locales = list(json.load(f))
        for locale in locales:
            with open(resource_path("languages", f"{locale}.json"), encoding="utf-8") as f:
                assert json.load(f).get(key, "").strip(), (locale, key)
