"""Copying a message, or reading it with Alt+C, names the people it mentions.

Reported 2026-09-23: the message list read a mention as "@Maria", but Ctrl+C
copied "@5511999999999" and Alt+C showed the same -- or the @lid digits, which
are not even a phone number. The row resolves mentions through
_resolve_mentions_in_text(); Ctrl+C, Alt+C and the bulk copy read the raw body.

All three now go through _message_text_with_names(), and the row takes its
mention list from the same _message_mentioned_jids(), so they cannot drift
apart again.

ConversationsPanel needs a wx.App, so the methods run against a stub. Alt+C
builds a real wx.Frame, so it is checked structurally.
"""

import inspect

import ui.conversations as conversations
from ui.conversations import ConversationsPanel

PHONE_JID = "5511999999999@s.whatsapp.net"
LID_JID = "123456789012345@lid"


class _I18n:
    def t(self, key):
        return f"[{key}]"


class _MainWindow:
    def __init__(self):
        self.i18n = _I18n()
        self.settings = {"user_interface": {"show_link_previews": True}}
        self._lid_to_phone = {}
        self.spoken = []

    def _is_self_jid(self, jid):
        return False

    def self_reference_label(self):
        return "Você"

    def output(self, text, *args, **kwargs):
        self.spoken.append(text)


class _Panel:
    _message_text_with_names = ConversationsPanel._message_text_with_names
    _message_mentioned_jids = staticmethod(ConversationsPanel._message_mentioned_jids)
    _resolve_mentions_in_text = ConversationsPanel._resolve_mentions_in_text
    _get_message_content = ConversationsPanel._get_message_content
    _on_menu_copy_message = ConversationsPanel._on_menu_copy_message

    NAMES = {PHONE_JID: "Maria", LID_JID: "João"}

    def __init__(self):
        self.main_window = _MainWindow()

    def _get_participant_name(self, jid):
        return self.NAMES.get(jid, jid)


def _mention(text, jids, where="ext"):
    ctx = {"mentionedJid": list(jids)}
    ext = {"text": text}
    msg = {"key": {"id": "M1", "fromMe": False}, "messageType": "extendedTextMessage",
           "message": {"extendedTextMessage": ext}}
    if where == "ext":
        ext["contextInfo"] = ctx
    elif where == "top":
        msg["contextInfo"] = ctx
    else:
        msg["message"]["contextInfo"] = ctx
    return msg


class TestTheTextHandedOver:
    def test_a_phone_mention_becomes_the_name(self):
        text = _Panel()._message_text_with_names(_mention("@5511999999999 olha isso", [PHONE_JID]))
        assert text == "@Maria olha isso"

    def test_a_lid_mention_becomes_the_name(self):
        text = _Panel()._message_text_with_names(_mention("oi @123456789012345", [LID_JID]))
        assert text == "oi @João"

    def test_mentions_are_found_wherever_the_api_put_them(self):
        panel = _Panel()
        for where in ("ext", "top", "message"):
            msg = _mention("@5511999999999 oi", [PHONE_JID], where=where)
            assert panel._message_text_with_names(msg) == "@Maria oi", where

    def test_it_matches_what_the_row_reads(self):
        panel = _Panel()
        msg = _mention("@5511999999999 e @123456789012345, vejam", [PHONE_JID, LID_JID])
        assert panel._message_text_with_names(msg) == panel._get_message_content(msg)

    def test_a_plain_message_is_unchanged(self):
        msg = {"messageType": "conversation", "message": {"conversation": "bom dia"}}
        assert _Panel()._message_text_with_names(msg) == "bom dia"

    def test_a_message_without_mentions_is_unchanged(self):
        msg = _mention("sem menções aqui", [])
        assert _Panel()._message_text_with_names(msg) == "sem menções aqui"

    def test_anything_else_has_no_text(self):
        msg = {"messageType": "imageMessage", "message": {"imageMessage": {}}}
        assert _Panel()._message_text_with_names(msg) == ""


class TestCtrlC:
    def test_it_copies_the_names(self, monkeypatch):
        copied = []
        monkeypatch.setattr(conversations.pyperclip, "copy", copied.append)
        panel = _Panel()

        panel._on_menu_copy_message(_mention("@5511999999999 olha isso", [PHONE_JID]))

        assert copied == ["@Maria olha isso"]
        assert panel.main_window.spoken == ["[msg_copied]"]


class TestEveryCopyPathUsesIt:
    def test_alt_c_reads_the_named_text(self):
        src = inspect.getsource(ConversationsPanel._show_message_text_popup)
        assert "self._message_text_with_names(msg)" in src

    def test_the_bulk_copy_reads_the_named_text(self):
        src = inspect.getsource(ConversationsPanel._on_mass_copy_messages)
        assert "self._message_text_with_names(m)" in src

    def test_the_row_takes_its_mentions_from_the_same_place(self):
        src = inspect.getsource(ConversationsPanel._get_message_content)
        assert "self._message_mentioned_jids(msg)" in src
