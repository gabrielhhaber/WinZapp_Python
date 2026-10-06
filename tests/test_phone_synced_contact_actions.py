"""Editing and deleting a contact saved to the phone (ConversationDataDialog).

A synced contact is edited as one, so no "local" copy is left next to the
WhatsApp one, and it is deleted from WhatsApp and the phone first: when that
fails the contact stays in WinZapp, which is what is still true.
"""

from types import SimpleNamespace

import pytest
import wx

import main
from core import phone_contacts
from tests.test_local_contact_sync import _Mw as _RealContactsWindow
from ui.dialogs import new_contact
from ui.dialogs.conversation_data_dialog import ConversationDataDialog

JID = "5511999999999@s.whatsapp.net"


class _MW:
    def __init__(self, entry):
        self.contacts = {JID: entry}
        self._lid_to_phone = {}
        self.removed_local, self.requests, self.spoken = [], [], []

    def remove_local_contact(self, jid): self.removed_local.append(jid)
    def output(self, text, **kwargs): self.spoken.append(text)

    def remove_phone_synced_contact(self, jid, on_done):
        self.requests.append(jid)
        self.on_done = on_done


class _Stub:
    _resolve_contact_phone_jid = ConversationDataDialog._resolve_contact_phone_jid
    _local_contact_entry = ConversationDataDialog._local_contact_entry
    _contact_entry = ConversationDataDialog._contact_entry
    _on_delete_contact = ConversationDataDialog._on_delete_contact
    _finish_delete_contact = ConversationDataDialog._finish_delete_contact
    _on_edit_contact = ConversationDataDialog._on_edit_contact
    _on_add_contact = ConversationDataDialog._on_add_contact

    def __init__(self, entry):
        self._jid, self._name = JID, "Ana Silva"
        self._i18n = SimpleNamespace(t=lambda key: key)
        self._mw = _MW(entry)
        self.repopulated = 0

    def _populate_contact_action_buttons(self): self.repopulated += 1
    def _fetch_data(self): pass
    def __bool__(self): return True


def _answer(monkeypatch, result):
    shown = []

    def _box(message, title="", style=0, parent=None):
        shown.append((message, title))
        return result

    monkeypatch.setattr(wx, "MessageBox", _box)
    return shown


class TestDelete:
    def test_a_local_contact_is_deleted_here_only(self, monkeypatch):
        stub = _Stub(phone_contacts.local_entry(JID, "Ana Silva"))
        shown = _answer(monkeypatch, wx.YES)
        stub._on_delete_contact(None)
        assert shown[0][0] == "delete_contact_local_confirm_msg"
        assert stub._mw.requests == [] and stub._mw.removed_local == [JID]
        assert stub.repopulated == 1

    def test_a_synced_contact_asks_whatsapp_and_warns_it_leaves_the_phone(self, monkeypatch):
        stub = _Stub(phone_contacts.synced_entry(JID, "Ana Silva"))
        shown = _answer(monkeypatch, wx.YES)
        stub._on_delete_contact(None)
        assert shown[0][0] == "delete_contact_phone_confirm_msg"
        assert stub._mw.requests == [JID] and stub._mw.removed_local == []

    def test_it_is_dropped_here_only_once_whatsapp_removed_it(self, monkeypatch):
        stub = _Stub(phone_contacts.synced_entry(JID, "Ana Silva"))
        _answer(monkeypatch, wx.YES)
        stub._on_delete_contact(None)
        stub._mw.on_done(True)
        assert stub._mw.removed_local == [JID] and stub.repopulated == 1

    def test_a_failure_keeps_the_contact_and_says_so(self, monkeypatch):
        stub = _Stub(phone_contacts.synced_entry(JID, "Ana Silva"))
        shown = _answer(monkeypatch, wx.YES)
        stub._on_delete_contact(None)
        stub._mw.on_done(False)
        assert stub._mw.removed_local == [] and stub.repopulated == 0
        assert stub._mw.spoken == ["delete_contact_phone_removing", "delete_contact_phone_failed"]
        assert shown[-1][0] == "delete_contact_phone_failed"

    def test_declining_the_confirmation_changes_nothing(self, monkeypatch):
        stub = _Stub(phone_contacts.synced_entry(JID, "Ana Silva"))
        _answer(monkeypatch, wx.NO)
        stub._on_delete_contact(None)
        assert stub._mw.requests == [] and stub._mw.removed_local == []


class TestEdit:
    def _edit(self, monkeypatch, entry):
        seen = {}

        class _Fake:
            def __init__(self, *args, **kwargs):
                seen.update(kwargs)

            def SetTitle(self, title): pass
            def ShowModal(self): return wx.ID_CANCEL
            def Destroy(self): pass

        monkeypatch.setattr(new_contact, "NewContactDialog", _Fake)
        _Stub(entry)._on_edit_contact(None)
        return seen

    def test_a_synced_contact_is_edited_only_as_a_synced_one(self, monkeypatch):
        seen = self._edit(monkeypatch, phone_contacts.synced_entry(JID, "Ana Silva"))
        assert seen["modes"] == (new_contact.MODE_PHONE,)
        assert seen["initial_mode"] == new_contact.MODE_PHONE

    def test_a_local_contact_opens_on_its_own_tab_and_may_become_synced(self, monkeypatch):
        seen = self._edit(monkeypatch, phone_contacts.local_entry(JID, "Ana Silva"))
        assert seen["modes"] == (new_contact.MODE_LOCAL, new_contact.MODE_PHONE)
        assert seen["initial_mode"] == new_contact.MODE_LOCAL


class TestAContactAddedOnThePhone:
    """Saved and synced by WhatsApp itself: WinZapp never wrote its marker, but
    the dialog must treat it as the real contact it is."""

    ENTRY = {"isMyContact": True, "syncToAddressbook": True, "name": "Ana Silva"}

    def test_it_is_the_contact_this_dialog_edits(self):
        stub = _Stub(dict(self.ENTRY))
        assert stub._local_contact_entry() is None
        assert stub._contact_entry() == self.ENTRY

    def test_a_number_that_is_not_in_the_phone_book_has_no_entry(self):
        stub = _Stub({"isMyContact": True, "syncToAddressbook": False, "name": "Ana"})
        assert stub._contact_entry() is None

    def test_it_is_edited_only_as_a_synced_one(self, monkeypatch):
        seen = {}

        class _Fake:
            def __init__(self, *args, **kwargs):
                seen.update(kwargs)

            def SetTitle(self, title): pass
            def ShowModal(self): return wx.ID_CANCEL
            def Destroy(self): pass

        monkeypatch.setattr(new_contact, "NewContactDialog", _Fake)
        _Stub(dict(self.ENTRY))._on_edit_contact(None)
        assert seen["modes"] == (new_contact.MODE_PHONE,)
        assert seen["prefill_name"] == "Ana"

    def test_deleting_it_removes_it_from_whatsapp_too(self, monkeypatch):
        stub = _Stub(dict(self.ENTRY))
        shown = _answer(monkeypatch, wx.YES)
        stub._on_delete_contact(None)
        assert shown[0][0] == "delete_contact_phone_confirm_msg"
        assert stub._mw.requests == [JID]

    def test_the_wait_is_announced(self, monkeypatch):
        stub = _Stub(dict(self.ENTRY))
        _answer(monkeypatch, wx.YES)
        stub._on_delete_contact(None)
        assert stub._mw.spoken == ["delete_contact_phone_removing"]

    def test_a_second_press_while_removing_asks_nothing_and_sends_nothing(self, monkeypatch):
        stub = _Stub(dict(self.ENTRY))
        shown = _answer(monkeypatch, wx.YES)
        stub._on_delete_contact(None)
        stub._on_delete_contact(None)
        assert len(shown) == 1 and stub._mw.requests == [JID]

    def test_after_a_failure_it_can_be_tried_again(self, monkeypatch):
        stub = _Stub(dict(self.ENTRY))
        _answer(monkeypatch, wx.YES)
        stub._on_delete_contact(None)
        stub._mw.on_done(False)
        stub._on_delete_contact(None)
        assert stub._mw.requests == [JID, JID]


JID_8 = "551199999999@s.whatsapp.net"      # JID without the 9th digit


class TestTheRecordFiledUnderTheOtherDigitForm:
    """The chat is 5511999999999 and the record sits under 551199999999 (or
    the reverse). The tolerant lookup finds it, so Edit/Delete are shown;
    deleting has to remove THAT key, or the contact stays as a ghost: gone
    from WhatsApp and the phone, still named and still deletable here."""

    class _Window(_RealContactsWindow):
        def __init__(self, entry):
            super().__init__(phone=JID)
            self.contacts = {JID_8: entry}
            self._phone_to_lid, self._lid_to_phone = {}, {}
            self.requests, self.spoken = [], []

        def output(self, text, **kwargs): self.spoken.append(text)

        def remove_phone_synced_contact(self, jid, on_done):
            self.requests.append(jid)
            self.on_done = on_done

    @pytest.fixture(autouse=True)
    def _inline_call_after(self, monkeypatch):
        monkeypatch.setattr(main.wx, "CallAfter", lambda fn, *a, **k: fn(*a, **k))

    def _stub(self, entry):
        stub = _Stub(entry)
        stub._mw = self._Window(entry)
        return stub

    def test_a_synced_one_is_removed_everywhere(self, monkeypatch):
        stub = self._stub({**phone_contacts.synced_entry(JID_8, "Ana Silva")})
        assert stub._contact_entry() is not None
        _answer(monkeypatch, wx.YES)

        stub._on_delete_contact(None)
        assert stub._mw.requests == [JID_8]        # the id WhatsApp knows it by
        stub._mw.on_done(True)

        assert stub._mw.contacts == {} and stub._contact_entry() is None
        assert JID_8 in stub._mw.db.deleted

    def test_one_added_on_the_phone_too(self, monkeypatch):
        stub = self._stub({"isMyContact": True, "syncToAddressbook": True, "name": "Ana"})
        _answer(monkeypatch, wx.YES)
        stub._on_delete_contact(None)
        stub._mw.on_done(True)
        assert stub._mw.contacts == {} and stub._contact_entry() is None


LID_CHAT = "123456789012345@lid"


class TestAChatKnownOnlyByItsLid:
    """Add and Edit hand the conversation's JID to the dialog, which then
    saves under the @lid instead of asking for a phone number."""

    @staticmethod
    def _open(monkeypatch, method, jid, entry=None):
        seen = {}

        class _Fake:
            def __init__(self, *args, **kwargs):
                seen.update(kwargs)

            def SetTitle(self, title): pass
            def ShowModal(self): return wx.ID_CANCEL
            def Destroy(self): pass

        monkeypatch.setattr(new_contact, "NewContactDialog", _Fake)
        stub = _Stub(entry or {})
        stub._jid = jid
        stub._mw.contacts = {jid: entry} if entry else {}
        getattr(stub, method)(None)
        return seen

    def test_add_passes_the_lid(self, monkeypatch):
        seen = self._open(monkeypatch, "_on_add_contact", LID_CHAT)
        assert seen["contact_jid"] == LID_CHAT

    def test_edit_passes_the_lid(self, monkeypatch):
        entry = phone_contacts.local_entry(LID_CHAT, "Ana Silva")
        seen = self._open(monkeypatch, "_on_edit_contact", LID_CHAT, entry)
        assert seen["contact_jid"] == LID_CHAT

    def test_a_chat_with_a_known_phone_passes_the_phone_jid(self, monkeypatch):
        """Which the dialog ignores: only an @lid fixes the JID."""
        seen = self._open(monkeypatch, "_on_add_contact", JID)
        assert seen["contact_jid"] == JID
        assert new_contact.fixed_jid_of(seen["contact_jid"]) == ""
