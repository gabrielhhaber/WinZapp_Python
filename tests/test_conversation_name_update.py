"""Tests for a group rename reaching the conversation currently on screen.

MainWindow applies a rename to its own state and then calls
ConversationsPanel.update_conversation_name() via wx.CallAfter so the open
conversation's header follows. That method guarded on a `self.current_jid`
attribute ConversationsPanel never assigns, so hasattr() was always False and
it returned on its first line every time — the rename reached the chat list
and nothing else, with no error to notice. These tests pin the guard against
the attribute the panel actually keeps (self.conversation) and cover the label
composition the method shares with navigate_to_conversation().

ConversationsPanel is a wx.Panel and cannot be instantiated without a running
wx.App, so the methods under test are bound to a small stub carrying only the
attributes they touch — same approach as tests/test_message_bookmarks.py.
"""

import pytest

from ui.conversations import ConversationsPanel


class _FakeI18n:
    _STRINGS = {
        "channel_read_only": "Canal somente leitura",
        "group_admins_only": "Somente administradores podem enviar mensagens",
        "type_message_group": "Digite uma mensagem para o grupo",
        "type_message": "Digite uma mensagem para",
        "phone_label": "Telefone",
    }

    def t(self, key):
        return self._STRINGS[key]


class _FakeWidget:
    def __init__(self):
        self.note = None
        self.label = None
        self.layouts = 0

    def SetNote(self, text):
        self.note = text

    def SetLabel(self, text):
        self.label = text

    def Layout(self):
        self.layouts += 1


class _FakeMainWindow:
    def __init__(self, chats=None, restricted=()):
        self.i18n = _FakeI18n()
        self.chats = chats or {}
        self._restricted = set(restricted)

    def _is_group_send_restricted(self, chat):
        return chat.get("remoteJid", "") in self._restricted


class _Panel:
    """Stub carrying exactly what the three methods under test touch."""

    _conversation_note_text = ConversationsPanel._conversation_note_text
    _message_label_text = ConversationsPanel._message_label_text
    _channel_role = ConversationsPanel._channel_role
    _channel_admin = ConversationsPanel._channel_admin
    update_conversation_name = ConversationsPanel.update_conversation_name

    def __init__(self, open_jid=None, main_window=None):
        self.main_window = main_window or _FakeMainWindow()
        self.conversation = {"remoteJid": open_jid} if open_jid else None
        self.conversation_name = "nome antigo"
        self._conv_data_btn = _FakeWidget()
        self.message_label = _FakeWidget()
        self.conversation_panel = _FakeWidget()


GROUP = "123456789@g.us"
OTHER_GROUP = "987654321@g.us"
CONTACT = "5511999999999@s.whatsapp.net"
CHANNEL = "111@newsletter"


class TestTheGuard:
    def test_a_rename_of_the_open_group_is_applied(self):
        """The whole point, and what never once happened before."""
        panel = _Panel(open_jid=GROUP)
        panel.update_conversation_name(GROUP, "nome novo")
        assert panel.conversation_name == "nome novo"
        assert panel._conv_data_btn.note == "nome novo"
        assert panel.conversation_panel.layouts == 1

    def test_a_rename_of_a_different_group_is_ignored(self):
        panel = _Panel(open_jid=GROUP)
        panel.update_conversation_name(OTHER_GROUP, "nome novo")
        assert panel.conversation_name == "nome antigo"
        assert panel._conv_data_btn.note is None
        assert panel.conversation_panel.layouts == 0

    def test_a_rename_with_no_conversation_open_is_ignored(self):
        """The panel exists before any chat is opened; self.conversation is
        None then and must not be dereferenced."""
        panel = _Panel(open_jid=None)
        panel.update_conversation_name(GROUP, "nome novo")
        assert panel.conversation_name == "nome antigo"
        assert panel.conversation_panel.layouts == 0

    def test_a_conversation_dict_without_a_jid_is_ignored(self):
        panel = _Panel(open_jid=None)
        panel.conversation = {}
        panel.update_conversation_name(GROUP, "nome novo")
        assert panel.conversation_name == "nome antigo"


class TestTheComposerLabel:
    def test_a_renamed_group_is_named_in_the_label(self):
        panel = _Panel(open_jid=GROUP)
        panel.update_conversation_name(GROUP, "nome novo")
        assert panel.message_label.label == "Digite uma mensagem para o grupo nome novo"

    def test_an_admins_only_group_keeps_saying_why_it_is_read_only(self):
        """A rename must not overwrite the reason the field cannot be used —
        that sentence is the only thing telling a screen-reader user why."""
        mw = _FakeMainWindow(chats={GROUP: {"remoteJid": GROUP}}, restricted={GROUP})
        panel = _Panel(open_jid=GROUP, main_window=mw)
        panel.update_conversation_name(GROUP, "nome novo")
        assert panel.message_label.label == "Somente administradores podem enviar mensagens"

    def test_a_channel_keeps_its_read_only_label(self):
        panel = _Panel(open_jid=CHANNEL)
        panel.update_conversation_name(CHANNEL, "canal novo")
        assert panel.message_label.label == "Canal somente leitura"

    def test_a_one_to_one_chat_uses_the_non_group_wording(self):
        panel = _Panel(open_jid=CONTACT)
        panel.update_conversation_name(CONTACT, "Fulano")
        assert panel.message_label.label == "Digite uma mensagem para Fulano"


class TestTheDataButtonNote:
    def test_a_group_name_is_used_as_is(self):
        panel = _Panel(open_jid=GROUP)
        assert panel._conversation_note_text("Equipe", is_group=True) == "Equipe"

    def test_a_bare_phone_number_is_labelled(self):
        """Otherwise the screen reader reads the digits as if they were a
        contact name."""
        note = _Panel()._conversation_note_text("+55 11 99999-9999", is_group=False)
        assert note == "Telefone: +55 11 99999-9999"

    def test_a_real_contact_name_is_left_alone(self):
        assert _Panel()._conversation_note_text("Fulano", is_group=False) == "Fulano"

    def test_a_group_whose_name_looks_like_a_number_is_not_labelled(self):
        """is_group short-circuits before the phone check — a group called
        "11999999999" is still a group."""
        assert _Panel()._conversation_note_text("11999999999", is_group=True) == "11999999999"
