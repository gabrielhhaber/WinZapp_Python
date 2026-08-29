"""Tests for contactMessage list actions in ui/conversations.py:

- Enter/Space (_do_activate_message) opens a conversation with the contact,
  same as the "Conversar" button — previously a no-op for contactMessage.
- The new "Salvar contato" button / Ctrl+Shift+S (_on_action_save_as)
  opens NewContactDialog pre-filled from the message, to add the contact
  locally in WinZapp.

ConversationsPanel is a wx.Panel and cannot be instantiated without a running
wx.App, so the methods under test are bound to a small stub carrying only the
attributes they touch — same approach as tests/test_mass_selection.py.
"""

import re

import pytest

from ui.conversations import ConversationsPanel


class _FakeI18n:
    _STRINGS = {"unknown_contact": "Unnamed contact"}

    def t(self, key):
        return self._STRINGS.get(key, key)


class _FakeMainWindow:
    def __init__(self):
        self.i18n = _FakeI18n()
        self.chats = {}
        self.settings = {"user_interface": {}}

    def get_chat(self, jid):
        return self.chats.get(jid)


class _FakeList:
    def __init__(self, focused=-1):
        self._focused = focused

    def GetFirstSelected(self):
        return self._focused


class _Panel:
    _is_separator = ConversationsPanel._is_separator
    _jid_from_vcard = ConversationsPanel._jid_from_vcard
    _contact_display_name = ConversationsPanel._contact_display_name
    _on_contact_converse = ConversationsPanel._on_contact_converse
    _on_save_contact_message = ConversationsPanel._on_save_contact_message
    _on_action_save_as = ConversationsPanel._on_action_save_as
    save_media_message = ConversationsPanel.save_media_message
    _do_activate_message = ConversationsPanel._do_activate_message
    activate_message = ConversationsPanel.activate_message
    _bulk_shortcuts_enabled = ConversationsPanel._bulk_shortcuts_enabled

    def __init__(self, messages=(), focused=-1):
        self.main_window = _FakeMainWindow()
        self._sorted_messages = list(messages)
        self.messages_list = _FakeList(focused=focused)
        self.selected_messages = set()
        self.navigated_to = []
        self._contact_msg_jid = None

    def navigate_to_conversation(self, chat):
        self.navigated_to.append(chat)


def _contact_msg(msg_id, display_name="", vcard="", jid=""):
    if not vcard and jid:
        phone = jid.split("@", 1)[0]
        vcard = f"BEGIN:VCARD\nVERSION:3.0\nFN:{display_name or 'X'}\nTEL;waid={phone}:+{phone}\nEND:VCARD"
    return {
        "key": {"id": msg_id},
        "messageType": "contactMessage",
        "message": {"contactMessage": {"displayName": display_name, "vcard": vcard}},
    }


class TestContactDisplayName:
    def test_uses_display_name_when_present(self):
        panel = _Panel()
        msg = _contact_msg("m1", display_name="Alice")
        assert panel._contact_display_name(msg) == "Alice"

    def test_falls_back_to_vcard_fn(self):
        panel = _Panel()
        vcard = "BEGIN:VCARD\nVERSION:3.0\nFN:Bob Builder\nEND:VCARD"
        msg = _contact_msg("m1", display_name="", vcard=vcard)
        assert panel._contact_display_name(msg) == "Bob Builder"

    def test_unknown_when_nothing_available(self):
        panel = _Panel()
        msg = _contact_msg("m1")
        assert panel._contact_display_name(msg) == "Unnamed contact"


class TestEnterOrSpaceOpensConversation:
    def test_activating_a_contact_message_navigates_to_its_chat(self):
        panel = _Panel(messages=[_contact_msg("m1", jid="5511999999999@s.whatsapp.net")])
        chat = {"remoteJid": "5511999999999@s.whatsapp.net"}
        panel.main_window.chats["5511999999999@s.whatsapp.net"] = chat
        panel._do_activate_message(0)
        assert panel.navigated_to == [chat]

    def test_no_known_chat_for_the_contact_is_a_no_op(self):
        panel = _Panel(messages=[_contact_msg("m1", jid="5511999999999@s.whatsapp.net")])
        panel._do_activate_message(0)  # not in main_window.chats
        assert panel.navigated_to == []

    def test_a_contact_message_with_no_resolvable_jid_is_a_no_op(self):
        panel = _Panel(messages=[_contact_msg("m1", display_name="No phone", vcard="BEGIN:VCARD\nEND:VCARD")])
        panel._do_activate_message(0)
        assert panel.navigated_to == []


class TestSaveContactMessage:
    def test_opens_new_contact_dialog_prefilled_from_the_message(self, monkeypatch):
        calls = []

        class _FakeDialog:
            def __init__(self, main_window, parent, prefill_phone="", prefill_name="", prefill_surname=""):
                calls.append((prefill_phone, prefill_name, prefill_surname))

            def ShowModal(self):
                return 0

            def Destroy(self):
                pass

        import ui.dialogs.new_contact as new_contact_module
        monkeypatch.setattr(new_contact_module, "NewContactDialog", _FakeDialog)

        panel = _Panel(
            messages=[_contact_msg("m1", display_name="Alice Silva", jid="5511999999999@s.whatsapp.net")],
            focused=0,
        )
        panel._on_save_contact_message(None)

        (phone, name, surname), = calls
        assert re.sub(r"\D", "", phone) == "5511999999999"
        assert name == "Alice"
        assert surname == "Silva"

    def test_a_contact_message_with_no_resolvable_jid_opens_no_dialog(self, monkeypatch):
        import ui.dialogs.new_contact as new_contact_module
        monkeypatch.setattr(
            new_contact_module, "NewContactDialog",
            lambda *a, **k: pytest.fail("opened dialog"),
        )
        panel = _Panel(
            messages=[_contact_msg("m1", vcard="BEGIN:VCARD\nEND:VCARD")],
            focused=0,
        )
        panel._on_save_contact_message(None)

    def test_ctrl_shift_s_routes_a_contact_message_to_save_contact_not_the_file_dialog(self, monkeypatch):
        calls = []

        class _FakeDialog:
            def __init__(self, *a, **k):
                calls.append(True)

            def ShowModal(self):
                return 0

            def Destroy(self):
                pass

        import ui.dialogs.new_contact as new_contact_module
        monkeypatch.setattr(new_contact_module, "NewContactDialog", _FakeDialog)

        panel = _Panel(
            messages=[_contact_msg("m1", display_name="Alice", jid="5511999999999@s.whatsapp.net")],
            focused=0,
        )
        panel._on_action_save_as(None)
        assert calls == [True]
