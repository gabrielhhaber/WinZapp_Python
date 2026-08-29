"""Tests for ConversationDataDialog's local-contact action buttons.

Reported live: the "Add contact" button kept showing even after a local
contact (NewContactDialog, isSaved=True) already existed for that number,
inviting the user to "add" it again from scratch instead of editing or
removing the existing one. Now the dialog shows either "Add local contact"
(none saved yet) or "Edit contact" + "Delete contact" (one already exists),
never both.

ConversationsDataDialog is a wx.Dialog and can't be instantiated without a
running wx.App; the methods under test are bound onto a plain stub —
same approach as tests/test_group_data_dialog_admin_ui.py.
"""

import wx

from ui.dialogs.conversation_data_dialog import ConversationDataDialog
from tests.conftest import hidden_frame


class _FakeI18n:
    def t(self, key):
        return key


class _Stub:
    _resolve_contact_phone_jid = ConversationDataDialog._resolve_contact_phone_jid
    _local_contact_entry = ConversationDataDialog._local_contact_entry
    _populate_contact_action_buttons = ConversationDataDialog._populate_contact_action_buttons
    _on_add_contact = ConversationDataDialog._on_add_contact
    _on_edit_contact = ConversationDataDialog._on_edit_contact
    _on_delete_contact = ConversationDataDialog._on_delete_contact

    def __init__(self, jid, panel):
        self._jid = jid
        self._name = "Alice"
        self._i18n = _FakeI18n()
        self._mw = type("MW", (), {
            "contacts": {},
            "_lid_to_phone": {},
            "i18n": _FakeI18n(),
        })()
        self._contact_panel = panel
        self._contact_action_sizer = wx.BoxSizer(wx.VERTICAL)


class TestResolveContactPhoneJid:
    def test_phone_jid_is_returned_unchanged(self):
        stub = _Stub.__new__(_Stub)
        stub._jid = "5511999999999@s.whatsapp.net"
        stub._mw = type("MW", (), {"_lid_to_phone": {}})()
        assert stub._resolve_contact_phone_jid() == "5511999999999@s.whatsapp.net"

    def test_lid_jid_resolves_through_the_bridge_map(self):
        stub = _Stub.__new__(_Stub)
        stub._jid = "12345@lid"
        stub._mw = type("MW", (), {
            "_lid_to_phone": {"12345@lid": "5511999999999@s.whatsapp.net"}
        })()
        assert stub._resolve_contact_phone_jid() == "5511999999999@s.whatsapp.net"

    def test_unresolvable_lid_jid_is_returned_as_is(self):
        stub = _Stub.__new__(_Stub)
        stub._jid = "12345@lid"
        stub._mw = type("MW", (), {"_lid_to_phone": {}})()
        assert stub._resolve_contact_phone_jid() == "12345@lid"


class TestLocalContactEntry:
    def test_none_when_no_contact_at_all(self):
        stub = _Stub.__new__(_Stub)
        stub._jid = "5511999999999@s.whatsapp.net"
        stub._mw = type("MW", (), {"contacts": {}, "_lid_to_phone": {}})()
        assert stub._local_contact_entry() is None

    def test_none_when_contact_exists_but_was_never_locally_saved(self):
        """A contact synced from WhatsApp itself (pushName, profile pic)
        without ever going through NewContactDialog must not count."""
        jid = "5511999999999@s.whatsapp.net"
        stub = _Stub.__new__(_Stub)
        stub._jid = jid
        stub._mw = type("MW", (), {
            "contacts": {jid: {"pushName": "Alice", "isSaved": False}},
            "_lid_to_phone": {},
        })()
        assert stub._local_contact_entry() is None

    def test_returns_the_entry_when_locally_saved(self):
        jid = "5511999999999@s.whatsapp.net"
        entry = {"name": "Alice Silva", "isSaved": True}
        stub = _Stub.__new__(_Stub)
        stub._jid = jid
        stub._mw = type("MW", (), {"contacts": {jid: entry}, "_lid_to_phone": {}})()
        assert stub._local_contact_entry() is entry


class TestPopulateContactActionButtons:
    def test_shows_only_add_when_no_local_contact_exists(self, wx_app):
        frame = hidden_frame()
        try:
            stub = _Stub("5511999999999@s.whatsapp.net", frame)
            stub._populate_contact_action_buttons()

            children = stub._contact_action_sizer.GetChildren()
            labels = [c.GetWindow().GetLabel() for c in children if c.GetWindow()]
            assert labels == ["add_contact"]
        finally:
            frame.Destroy()

    def test_shows_edit_and_delete_when_a_local_contact_exists(self, wx_app):
        frame = hidden_frame()
        try:
            jid = "5511999999999@s.whatsapp.net"
            stub = _Stub(jid, frame)
            stub._mw.contacts[jid] = {"name": "Alice Silva", "isSaved": True}
            stub._populate_contact_action_buttons()

            children = stub._contact_action_sizer.GetChildren()
            labels = [c.GetWindow().GetLabel() for c in children if c.GetWindow()]
            assert labels == ["edit_contact_local", "delete_contact_local"]
        finally:
            frame.Destroy()

    def test_switches_from_add_to_edit_delete_after_being_repopulated(self, wx_app):
        """Simulates what _on_add_contact() does after a successful add:
        re-running this must swap the button set without leaving stale
        widgets from the previous state around."""
        frame = hidden_frame()
        try:
            jid = "5511999999999@s.whatsapp.net"
            stub = _Stub(jid, frame)
            stub._populate_contact_action_buttons()
            assert stub._contact_action_sizer.GetItemCount() == 1

            stub._mw.contacts[jid] = {"name": "Alice Silva", "isSaved": True}
            stub._populate_contact_action_buttons()

            assert stub._contact_action_sizer.GetItemCount() == 2
            labels = [
                c.GetWindow().GetLabel()
                for c in stub._contact_action_sizer.GetChildren() if c.GetWindow()
            ]
            assert labels == ["edit_contact_local", "delete_contact_local"]
        finally:
            frame.Destroy()
