"""Tests for MainWindow.navigate_to_conversation_jid()'s panel-visibility fix.

Opening a conversation from a toast click (or the participant-list dialog)
can happen while Status or the Archived list is the panel actually shown.
navigate_to_jid()/navigate_to_conversation() both SetFocus()/Select() controls
inside conversations_panel regardless of whether it's visible, which used to
strand NVDA focus on an invisible control — the same class of bug on_alt_1()
already fixed for its own hotkey. This test pins the panel-prep dance that
now runs before the actual navigation.

MainWindow is a wx.Frame and cannot be instantiated without a running app, so
the method under test is bound onto a plain stub carrying only the attributes
it touches.
"""

from unittest.mock import Mock

from main import MainWindow


class _Stub:
    """Minimal stand-in for MainWindow for navigate_to_conversation_jid()."""

    def __init__(self, **kwargs):
        self._window_hidden = False
        self.restore_window = Mock()
        self.conversations_panel = Mock()
        self.conversations_panel.chats_list = []
        self.archived_conversations_panel = Mock()
        self.archived_conversations_panel.chats_list = []
        self.status_panel = Mock()
        self.content_panel = Mock()
        self.is_chat_archived = Mock(return_value=False)
        for key, value in kwargs.items():
            setattr(self, key, value)

    navigate_to_conversation_jid = MainWindow.navigate_to_conversation_jid


def test_non_archived_jid_shows_conversations_panel():
    stub = _Stub()
    stub.is_chat_archived = Mock(return_value=False)

    stub.navigate_to_conversation_jid("5511999999999@s.whatsapp.net")

    stub.archived_conversations_panel.Hide.assert_called_once()
    stub.status_panel.Hide.assert_called_once()
    stub.conversations_panel.conversations_label.Show.assert_called_once()
    stub.conversations_panel.conversations_list.Show.assert_called_once()
    stub.conversations_panel.Show.assert_called_once()
    stub.content_panel.Layout.assert_called_once()
    stub.conversations_panel.navigate_to_jid.assert_called_once_with(
        "5511999999999@s.whatsapp.net"
    )
    stub.conversations_panel.navigate_to_conversation.assert_not_called()


def test_archived_jid_found_opens_via_archived_panel_dance():
    jid = "5511888888888@s.whatsapp.net"
    chat = {"remoteJid": jid, "name": "Someone"}
    stub = _Stub()
    stub.is_chat_archived = Mock(return_value=True)
    stub.archived_conversations_panel.chats_list = [chat]

    stub.navigate_to_conversation_jid(jid)

    stub.conversations_panel.navigate_to_conversation.assert_called_once_with(chat)
    stub.conversations_panel.conversations_label.Hide.assert_called_once()
    stub.conversations_panel.conversations_list.Hide.assert_called_once()
    stub.archived_conversations_panel.Hide.assert_called_once()
    stub.status_panel.Hide.assert_called_once()
    stub.conversations_panel.navigate_to_jid.assert_not_called()


def test_archived_jid_not_found_falls_back_to_non_archived_path():
    jid = "5511777777777@s.whatsapp.net"
    stub = _Stub()
    stub.is_chat_archived = Mock(return_value=True)
    stub.archived_conversations_panel.chats_list = []  # stale/empty

    stub.navigate_to_conversation_jid(jid)

    stub.conversations_panel.navigate_to_jid.assert_called_once_with(jid)
    stub.conversations_panel.navigate_to_conversation.assert_not_called()


def test_window_hidden_restores_before_anything_else():
    stub = _Stub()
    stub._window_hidden = True
    stub.is_chat_archived = Mock(return_value=False)

    stub.navigate_to_conversation_jid("5511666666666@s.whatsapp.net")

    stub.restore_window.assert_called_once()
    stub.conversations_panel.navigate_to_jid.assert_called_once()


# ── A person with no conversation yet (Enter on a group participant) ─────────
#
# Reported 2026-09-23: Enter on a group participant who was not a saved
# contact (or was only a phone number) closed the dialog and left the user in
# the group. navigate_to_jid() only opens a row that already exists under the
# exact JID, and returned without a word when there was none.


class _ChatStub(_Stub):
    _chat_for_private_conversation = MainWindow._chat_for_private_conversation
    _is_bad_contact_name = staticmethod(MainWindow._is_bad_contact_name)
    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    get_chat = MainWindow.get_chat

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.chats = {}
        self._lid_to_phone = {}
        self._phone_to_lid = {}
        self._schedule_set_chats = Mock()
        self.conversations_panel.navigate_to_jid = Mock(return_value=False)


def test_a_participant_never_talked_to_gets_a_new_conversation():
    jid = "5511912345678@s.whatsapp.net"
    stub = _ChatStub()

    stub.navigate_to_conversation_jid(jid, "Maria")

    chat = stub.chats[jid]
    assert chat == {"remoteJid": jid, "pushName": "Maria"}
    stub._schedule_set_chats.assert_called_once()
    stub.conversations_panel.navigate_to_conversation.assert_called_once_with(chat)


def test_a_number_as_the_name_is_not_stored_as_one():
    jid = "5511912345678@s.whatsapp.net"
    stub = _ChatStub()

    stub.navigate_to_conversation_jid(jid, "+55 11 91234-5678")

    assert stub.chats[jid] == {"remoteJid": jid}


def test_a_chat_under_an_equivalent_jid_is_reused_not_duplicated():
    """The chat exists under the 8-digit form; the participant carries 9."""
    existing_jid = "551112345678@s.whatsapp.net"
    existing = {"remoteJid": existing_jid, "name": "Maria"}
    stub = _ChatStub()
    stub.chats = {existing_jid: existing}
    stub.conversations_panel.navigate_to_jid = Mock(side_effect=[False, True])

    stub.navigate_to_conversation_jid("5511912345678@s.whatsapp.net", "Maria")

    assert list(stub.chats) == [existing_jid]
    assert stub.conversations_panel.navigate_to_jid.call_args_list[-1].args == (existing_jid,)
    stub._schedule_set_chats.assert_not_called()


def test_an_unbridged_lid_creates_nothing():
    stub = _ChatStub()

    stub.navigate_to_conversation_jid("123456789012345@lid", "Maria")

    assert stub.chats == {}
    stub.conversations_panel.navigate_to_conversation.assert_not_called()


def test_a_group_jid_creates_nothing():
    stub = _ChatStub()

    stub.navigate_to_conversation_jid("120363000000000000@g.us")

    assert stub.chats == {}
    stub.conversations_panel.navigate_to_conversation.assert_not_called()
