"""Tests for AddMemberDialog._populate_contacts() only listing the user's
own contacts.

Reported live: the group-member picker showed contacts main_window.contacts
never actually earned that status for — a JID that only ever appeared there
because on_presence_update()/sender-name learning wrote {name, pushName} for
someone who spoke in some OTHER group. Those entries carry no isMyContact/
isSaved flag and aren't backed by a 1:1 chat, so they now get filtered out;
only genuine WhatsApp contacts (isMyContact), locally-added ones (isSaved),
"me", and anyone with an existing 1:1 chat still show up.

AddMemberDialog is a wx.Dialog and can't be instantiated without a running
wx.App, so the method under test is bound onto a plain stub carrying a real
wx.ListCtrl — same approach as the rest of this test suite.
"""

import wx

from ui.dialogs.add_member_dialog import AddMemberDialog
from tests.conftest import hidden_frame


class _Stub:
    _populate_contacts = AddMemberDialog._populate_contacts

    def __init__(self, frame, contacts, chats=None):
        self._mw = type("MW", (), {"contacts": contacts, "chats": chats or {}})()
        self._list = wx.ListCtrl(frame, style=wx.LC_REPORT)
        self._list.InsertColumn(0, "Name")
        self._list.InsertColumn(1, "Phone")


class TestAddMemberContactFilter:
    def test_group_participant_only_entry_is_excluded(self, wx_app):
        """No isMyContact/isSaved, no 1:1 chat — just a name learned from
        some other group's presence updates."""
        frame = hidden_frame()
        try:
            jid = "5511999999999@s.whatsapp.net"
            stub = _Stub(frame, {jid: {"name": "Alice", "pushName": "Alice"}})
            stub._populate_contacts()
            assert stub._contact_jids == []
        finally:
            frame.Destroy()

    def test_genuine_whatsapp_contact_is_included(self, wx_app):
        jid = "5511999999999@s.whatsapp.net"
        frame = hidden_frame()
        try:
            stub = _Stub(frame, {jid: {"name": "Alice", "isMyContact": True}})
            stub._populate_contacts()
            assert stub._contact_jids == [jid]
        finally:
            frame.Destroy()

    def test_locally_added_contact_is_included(self, wx_app):
        jid = "5511999999999@s.whatsapp.net"
        frame = hidden_frame()
        try:
            stub = _Stub(frame, {jid: {"name": "Alice", "isSaved": True}})
            stub._populate_contacts()
            assert stub._contact_jids == [jid]
        finally:
            frame.Destroy()

    def test_contact_with_an_existing_1to1_chat_is_included(self, wx_app):
        """Someone who messaged first without being in the user's own
        address book — WhatsApp may never flag them isMyContact, but the
        user clearly already has a real conversation with them."""
        jid = "5511999999999@s.whatsapp.net"
        frame = hidden_frame()
        try:
            stub = _Stub(
                frame,
                {jid: {"name": "Alice"}},
                chats={jid: {"remoteJid": jid}},
            )
            stub._populate_contacts()
            assert stub._contact_jids == [jid]
        finally:
            frame.Destroy()

    def test_groups_are_always_excluded_regardless_of_flags(self, wx_app):
        jid = "123456789-987654321@g.us"
        frame = hidden_frame()
        try:
            stub = _Stub(frame, {jid: {"name": "Some Group", "isMyContact": True}})
            stub._populate_contacts()
            assert stub._contact_jids == []
        finally:
            frame.Destroy()

    def test_mixed_list_keeps_only_the_legitimate_ones(self, wx_app):
        real = "5511111111111@s.whatsapp.net"
        leaked = "5522222222222@s.whatsapp.net"
        frame = hidden_frame()
        try:
            stub = _Stub(frame, {
                real:   {"name": "Real Contact", "isMyContact": True},
                leaked: {"name": "Leaked From Another Group"},
            })
            stub._populate_contacts()
            assert stub._contact_jids == [real]
        finally:
            frame.Destroy()
