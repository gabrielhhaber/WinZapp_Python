"""NewContactDialog: a local-contact tab and a phone-synced tab.

The synced tab is the default. Saving there goes to WhatsApp first and keeps
the local record only once WhatsApp has the contact, under the JID WhatsApp
filed it under. The dialog is a wx.Dialog, so its methods are called on a
plain stub (never a real window).
"""

import inspect
from types import SimpleNamespace

import pytest
import wx

import main
from core import phone_contacts
from core.phone_contacts import SaveResult
from tests.locales import registered_locale_codes
from tests.mnemonics import load_strings, mnemonic
from tests.test_local_contact_sync import _Mw as _RealContactsWindow
from ui.dialogs import new_contact
from ui.dialogs.new_contact import (
    MODE_LOCAL, MODE_PHONE, NewContactDialog, asked_fields, fixed_jid_of, known_jid,
    resolve_initial_mode)

JID = "5511999999999@s.whatsapp.net"
JID_8 = "551199999999@s.whatsapp.net"      # the same number without the 9th digit
SYNCED = {"isMyContact": True, "syncToAddressbook": True}


class _Field:
    def __init__(self, value=""):
        self.value, self.focused = value, False

    def GetValue(self): return self.value
    def ChangeValue(self, value): self.value = value
    def SetFocus(self): self.focused = True


class _Notebook:
    def __init__(self, selection=1):
        self.selection, self.enabled = selection, True

    def GetSelection(self): return self.selection
    def SetSelection(self, index): self.selection = index
    def Enable(self, on): self.enabled = on


class _Control:
    def __init__(self): self.label, self.enabled, self.focused = "", True, False
    def SetLabel(self, text): self.label = text
    def Enable(self, on): self.enabled = on
    def SetFocus(self): self.focused = True


class _MW:
    i18n = SimpleNamespace(t=lambda key: key)

    def __init__(self):
        self.local_saved, self.local_removed, self.spoken, self.requests = [], [], [], []
        self.contacts = {}

    def save_local_contact(self, jid, entry): self.local_saved.append((jid, entry))
    def remove_local_contact(self, jid): self.local_removed.append(jid)
    def output(self, text, **kwargs): self.spoken.append(text)

    def save_phone_synced_contact(self, jid, first, last, on_done):
        self.requests.append((jid, first, last))
        self.on_done = on_done


class _Dialog:
    _mode = NewContactDialog._mode
    _refresh_ok_label = NewContactDialog._refresh_ok_label
    _on_tab_changed = NewContactDialog._on_tab_changed
    _set_busy = NewContactDialog._set_busy
    _on_add = NewContactDialog._on_add
    _save_synced = NewContactDialog._save_synced

    def __init__(self, mode=MODE_PHONE, mw=None, fixed_jid=""):
        self._mw = mw or _MW()
        self._modes = (MODE_LOCAL, MODE_PHONE)
        self._notebook = _Notebook(self._modes.index(mode))
        self._fixed_jid = fixed_jid
        # The fields _build_page() creates: the same asked_fields() decides.
        self._fields = {m: {key: _Field() for key, _label in asked_fields(fixed_jid)}
                        for m in self._modes}
        self._ok_btn, self._cancel_btn, self._status = _Control(), _Control(), _Control()
        self._busy = False
        self.result_jid = self.result_name = ""
        self.ended, self.closed, self.modal = [], False, True

    def Layout(self): pass
    def EndModal(self, code): self.ended.append(code)
    def IsModal(self): return self.modal
    def __bool__(self): return not self.closed

    def fill(self, mode, name="Ana", surname="Silva", phone="+55 11 99999-9999"):
        for key, value in (("name", name), ("surname", surname), ("phone", phone)):
            if key in self._fields[mode]:
                self._fields[mode][key].value = value

    def saving(self):
        """A dialog whose synced tab was filled and submitted."""
        self.fill(MODE_PHONE)
        self._on_add(None)
        return self


def _ok(jid=JID, synced=True):
    return SaveResult(True, "", jid, synced)


@pytest.fixture
def boxes(monkeypatch):
    shown = []
    monkeypatch.setattr(new_contact.wx, "MessageBox", lambda *a, **k: shown.append(a[0]))
    return shown


class TestWhichTabOpens:
    def test_the_phone_synced_tab_is_the_default(self):
        assert inspect.signature(NewContactDialog).parameters["initial_mode"].default == MODE_PHONE
        assert resolve_initial_mode((MODE_LOCAL, MODE_PHONE), MODE_PHONE) == MODE_PHONE

    def test_a_requested_tab_that_exists_is_used(self):
        assert resolve_initial_mode((MODE_LOCAL, MODE_PHONE), MODE_LOCAL) == MODE_LOCAL

    def test_a_tab_that_does_not_exist_falls_back_to_the_synced_one(self):
        assert resolve_initial_mode((MODE_PHONE,), MODE_LOCAL) == MODE_PHONE

    def test_the_tabs_are_local_first_then_synced(self):
        assert inspect.signature(NewContactDialog).parameters["modes"].default == (
            MODE_LOCAL, MODE_PHONE)


class TestTabs:
    def test_the_button_says_what_the_tab_does(self):
        d = _Dialog(MODE_PHONE)
        d._refresh_ok_label()
        assert d._ok_btn.label == "create_contact_phone"
        d._notebook.selection = 0
        d._refresh_ok_label()
        assert d._ok_btn.label == "create_contact"

    def test_what_was_typed_follows_to_the_other_tab(self):
        d = _Dialog(MODE_LOCAL)
        d.fill(MODE_PHONE, "Ana", "Silva", "5511999999999")
        d._notebook.selection = 0
        event = SimpleNamespace(GetOldSelection=lambda: 1, GetSelection=lambda: 0,
                                Skip=lambda: None)
        d._on_tab_changed(event)
        assert [d._fields[MODE_LOCAL][k].value for k in ("name", "surname", "phone")] == [
            "Ana", "Silva", "5511999999999"]


class TestLocalTab:
    def test_a_local_contact_is_stored_without_asking_whatsapp(self, boxes):
        d = _Dialog(MODE_LOCAL)
        d.fill(MODE_LOCAL)
        d._on_add(None)
        assert d._mw.requests == []
        assert d._mw.local_saved == [(JID, phone_contacts.local_entry(JID, "Ana Silva"))]
        assert d.ended == [wx.ID_OK] and d.result_jid == JID and d.result_name == "Ana Silva"

    def test_a_missing_name_or_a_short_number_is_refused(self, boxes):
        d = _Dialog(MODE_LOCAL)
        d.fill(MODE_LOCAL, name="")
        d._on_add(None)
        d.fill(MODE_LOCAL, phone="123")
        d._on_add(None)
        assert d.ended == [] and d._mw.local_saved == [] and len(boxes) == 2


class TestSyncedTab:
    def test_it_asks_whatsapp_and_waits_before_storing(self, boxes):
        d = _Dialog(MODE_PHONE).saving()
        assert d._mw.requests == [(JID, "Ana", "Silva")]
        assert d._mw.local_saved == [] and d.ended == []
        assert d._busy and not d._ok_btn.enabled and d._status.label == "new_contact_phone_saving"

    def test_the_focus_leaves_the_controls_that_get_disabled(self, boxes):
        """A disabled control holding the focus leaves the keyboard nowhere."""
        d = _Dialog(MODE_PHONE).saving()
        assert d._cancel_btn.focused and d._cancel_btn.enabled

    def test_when_whatsapp_has_it_the_record_is_kept_and_the_dialog_closes(self, boxes):
        d = _Dialog(MODE_PHONE).saving()
        d._mw.on_done(_ok())
        (jid, entry), = d._mw.local_saved
        assert jid == JID and phone_contacts.is_phone_synced(entry) and entry["name"] == "Ana Silva"
        assert d.ended == [wx.ID_OK] and d.result_jid == JID
        assert d._mw.spoken[-1] == "new_contact_phone_saved"

    def test_it_is_filed_under_the_jid_whatsapp_answered(self, boxes):
        """Typed with the 9th digit, filed by WhatsApp without it: under the
        typed form the contact data dialog found the record and could never
        delete it."""
        d = _Dialog(MODE_PHONE).saving()
        d._mw.on_done(_ok(jid=JID_8))
        (jid, entry), = d._mw.local_saved
        assert jid == JID_8 and entry["remoteJid"] == JID_8
        assert d.result_jid == JID_8

    def test_a_sync_that_was_not_confirmed_is_said_so_but_the_contact_is_whatsapps(self, boxes):
        """The announcement is honest about it; the record is still handled
        as a contact that lives in WhatsApp, which it does."""
        d = _Dialog(MODE_PHONE).saving()
        d._mw.on_done(_ok(synced=False))
        (_jid, entry), = d._mw.local_saved
        assert phone_contacts.is_phone_synced(entry) is True
        assert entry["syncToAddressbook"] is False
        assert d._mw.spoken[-1] == "new_contact_phone_saved_unconfirmed"
        assert d.ended == [wx.ID_OK]

    def test_a_refusal_keeps_the_dialog_open_and_says_why(self, boxes):
        d = _Dialog(MODE_PHONE).saving()
        d._mw.on_done(SaveResult(False, "new_contact_phone_not_on_whatsapp", JID, False))
        assert d._mw.local_saved == [] and d.ended == []
        assert not d._busy and d._ok_btn.enabled
        assert boxes == ["new_contact_phone_not_on_whatsapp"]
        assert d._mw.spoken[-1] == "new_contact_phone_not_on_whatsapp"
        assert d._fields[MODE_PHONE]["phone"].focused

    def test_closed_meanwhile_the_record_is_still_kept(self, boxes):
        """WhatsApp has the contact by then; WinZapp must not forget it."""
        d = _Dialog(MODE_PHONE).saving()
        d.closed = True
        d._mw.on_done(_ok())
        assert len(d._mw.local_saved) == 1 and d.ended == []

    def test_no_longer_modal_it_is_not_ended_a_second_time(self, boxes):
        """Cancelled while saving: ShowModal() has returned and the caller is
        about to destroy the dialog, which still tests as alive."""
        d = _Dialog(MODE_PHONE).saving()
        d.modal = False
        d._mw.on_done(_ok())
        assert len(d._mw.local_saved) == 1 and d.ended == []
        d._mw.on_done(SaveResult(False, "new_contact_phone_failed", JID, False))
        assert boxes == []

    def test_a_second_press_while_saving_does_nothing(self, boxes):
        d = _Dialog(MODE_PHONE).saving()
        d._on_add(None)
        assert len(d._mw.requests) == 1


class TestTheRecordAlreadyHereUnderTheOtherDigitForm:
    """Through the real MainWindow contact methods."""

    class _Window(_RealContactsWindow):
        i18n = SimpleNamespace(t=lambda key: key)

        def output(self, text, **kwargs): pass

        def save_phone_synced_contact(self, jid, first, last, on_done):
            self.on_done = on_done

    @pytest.fixture(autouse=True)
    def _inline_call_after(self, monkeypatch):
        monkeypatch.setattr(main.wx, "CallAfter", lambda fn, *a, **k: fn(*a, **k))

    def test_it_is_replaced_not_left_next_to_the_synced_one(self, boxes):
        mw = self._Window(phone=JID)
        mw.contacts = {JID: phone_contacts.local_entry(JID, "Ana (local)")}
        d = _Dialog(MODE_PHONE, mw=mw).saving()

        mw.on_done(_ok(jid=JID_8))

        assert JID not in mw.contacts
        assert phone_contacts.is_phone_synced(mw.contacts[JID_8])
        assert mw.contacts[JID_8]["name"] == "Ana Silva"
        assert JID in mw.db.deleted

    def test_the_same_key_is_simply_overwritten(self, boxes):
        mw = self._Window(phone=JID)
        mw.contacts = {JID: phone_contacts.local_entry(JID, "Ana (local)")}
        _Dialog(MODE_PHONE, mw=mw).saving()

        mw.on_done(_ok(jid=JID))

        assert phone_contacts.is_phone_synced(mw.contacts[JID])
        assert JID not in mw.db.deleted


class TestANumberAlreadyInThePhoneBook:
    def test_the_local_tab_cannot_hide_it(self, boxes):
        d = _Dialog(MODE_LOCAL)
        d._mw.contacts[JID] = dict(SYNCED)
        d.fill(MODE_LOCAL)
        d._on_add(None)
        assert d._mw.local_saved == [] and d._mw.requests == [] and d.ended == []
        assert boxes == ["new_contact_local_blocked"]
        assert d._notebook.selection == d._modes.index(MODE_PHONE)
        assert d._fields[MODE_PHONE]["name"].focused

    def test_a_number_that_is_not_synced_still_saves_locally(self, boxes):
        d = _Dialog(MODE_LOCAL)
        d._mw.contacts[JID] = {"isMyContact": True, "syncToAddressbook": False}
        d.fill(MODE_LOCAL)
        d._on_add(None)
        assert len(d._mw.local_saved) == 1 and d.ended == [wx.ID_OK]

    def test_the_jid_a_prefilled_number_names(self):
        """What the dialog looks up when it opens, to offer only the synced
        tab for a number already in the phone's address book."""
        assert known_jid("", "+55 (11) 99999-9999") == JID
        assert known_jid("", "") == ""


class TestMnemonics:
    """Per locale: the letters of the buttons this feature added or renamed
    repeat no other control of their own dialog."""

    @pytest.fixture(params=registered_locale_codes())
    def strings(self, request):
        return load_strings(request.param)

    def test_save_to_phone_shares_no_letter_with_the_new_contact_dialog(self, strings):
        letter = mnemonic(strings["create_contact_phone"])
        assert letter is not None
        assert letter not in {mnemonic(strings[k]) for k in (
            "contact_name", "contact_surname", "create_contact", "cancel")}

    def test_new_contact_shares_no_letter_with_the_new_chat_dialog(self, strings):
        """The button no longer says "local" (the dialog it opens defaults to
        the synced tab), so its letter moved."""
        letter = mnemonic(strings["new_contact"])
        assert letter is not None
        assert letter not in {mnemonic(strings[k]) for k in (
            "close", "new_group", "search_name_or_number")}


LID = "123456789012345@lid"


class TestAConversationKnownOnlyByItsLid:
    """No phone number is known for it. Its digits are not one: typed into a
    phone field they named nobody ("this number is not on WhatsApp", about
    someone the user is talking to) or a stranger."""

    def test_only_an_lid_fixes_the_jid(self):
        assert fixed_jid_of(LID) == LID
        for other in (JID, "5511999999999@c.us", "", None):
            assert fixed_jid_of(other) == ""

    def test_the_dialog_takes_the_conversations_jid(self):
        assert "contact_jid" in inspect.signature(NewContactDialog).parameters

    def test_no_phone_is_asked_for(self):
        """The decision _build_page() builds its fields from."""
        assert [key for key, _label in asked_fields(LID)] == ["name", "surname"]
        assert [key for key, _label in asked_fields("")] == ["name", "surname", "phone"]
        assert dict(asked_fields(""))["phone"] == "phone_label"

    def test_the_lid_is_what_the_dialog_looks_up_whatever_was_prefilled(self):
        """The @lid's digits are not a phone number to look anything up by."""
        assert known_jid(LID, "123456789012345") == LID
        assert known_jid(LID, "") == LID

    def test_the_synced_tab_saves_under_the_lid_without_asking_for_a_number(self, boxes):
        d = _Dialog(MODE_PHONE, fixed_jid=LID).saving()
        assert boxes == []                                   # no "check the number"
        assert d._mw.requests == [(LID, "Ana", "Silva")]
        d._mw.on_done(_ok(jid=LID))
        (jid, entry), = d._mw.local_saved
        assert jid == LID and entry["remoteJid"] == LID
        assert phone_contacts.is_phone_synced(entry) and d.result_jid == LID

    def test_the_request_carries_the_whole_lid(self):
        """What the mixin then sends for that JID."""
        assert phone_contacts.wire_id(LID) == LID

    def test_the_local_tab_files_it_under_the_lid_too(self, boxes):
        d = _Dialog(MODE_LOCAL, fixed_jid=LID)
        d.fill(MODE_LOCAL)
        d._on_add(None)
        assert d._mw.local_saved == [(LID, phone_contacts.local_entry(LID, "Ana Silva"))]
        assert d.ended == [wx.ID_OK] and d.result_jid == LID

    def test_a_name_is_still_required(self, boxes):
        d = _Dialog(MODE_PHONE, fixed_jid=LID)
        d.fill(MODE_PHONE, name="")
        d._on_add(None)
        assert len(boxes) == 1 and d._mw.requests == []

    def test_a_refusal_puts_the_focus_on_a_field_that_exists(self, boxes):
        d = _Dialog(MODE_PHONE, fixed_jid=LID).saving()
        d._mw.on_done(SaveResult(False, "new_contact_phone_failed", LID, False))
        assert d._fields[MODE_PHONE]["name"].focused and d.ended == []
