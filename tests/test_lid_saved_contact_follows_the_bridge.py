"""A contact saved for a chat known only by its @lid survives learning its phone.

The new-contact dialog saves such a contact under the @lid (there is no phone
number to save it under). Two things then went wrong once the @lid resolution
ran, both measured on the real methods before the fix:

* the bridge @lid -> phone was learned, every lookup moved to the phone JID,
  and the record stayed under the @lid alone: the contact data dialog showed
  "Add contact" again, with the saved one out of reach of Edit and Delete;
* the resolution wrote the person's pushname over the name the user had given
  ("Ana Silva" became "aninha"), with the record still marked as saved.

The real MainWindow methods run on a stub: no window, and api_get is replaced.
"""

import threading
from types import SimpleNamespace

import pytest

from core import phone_contacts as pc
from main import MainWindow
from main_window import contacts as contacts_module
from main_window import identity as identity_module
from ui.dialogs.conversation_data_dialog import ConversationDataDialog

LID = "123456789012345@lid"
PHONE = "5511999999999@s.whatsapp.net"
PN = {"id": "5511999999999", "server": "c.us", "_serialized": "5511999999999@c.us"}


class _Db:
    def __init__(self):
        self.upserted = {}

    def upsert_contacts_batch(self, contacts):
        self.upserted.update(contacts)

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class _Window:
    save_local_contact = MainWindow.save_local_contact
    _lid_for_local_contact = MainWindow._lid_for_local_contact
    _refresh_views_after_contact_change = MainWindow._refresh_views_after_contact_change
    _get_contact_tolerant = MainWindow._get_contact_tolerant
    _phone_digits_equivalent = staticmethod(MainWindow._phone_digits_equivalent)
    _is_bad_contact_name = staticmethod(MainWindow._is_bad_contact_name)
    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    register_jid_mapping = MainWindow.register_jid_mapping
    resolve_lid_jids_via_api = MainWindow.resolve_lid_jids_via_api
    _store_resolved_name = MainWindow._store_resolved_name
    _resolve_contact_name = MainWindow._resolve_contact_name
    wpp_server, wpp_port, token = "http://127.0.0.1", 6300, "tok"
    my_lid = my_jid = ""

    def __init__(self):
        self.contacts = {LID: {"id": LID, "remoteJid": LID, "name": "", "pushName": ""}}
        self.chats = {LID: {"remoteJid": LID}}
        self._lid_to_phone, self._phone_to_lid = {}, {}
        self._lid_mapping_lock = threading.Lock()
        self._unresolvable_lids, self._unresolvable_names = set(), set()
        self._presence_pushname_map = {}
        self._wa_connected = True
        self.db = _Db()

    def _is_self_jid(self, jid): return False
    def _find_alt_jid_from_messages(self, chat): return None
    def _schedule_set_chats(self): pass
    def _schedule_refresh_active_messages(self, jids=None): pass
    def save_data(self, *args, **kwargs): pass


class _Dialog:
    _resolve_contact_phone_jid = ConversationDataDialog._resolve_contact_phone_jid
    _local_contact_entry = ConversationDataDialog._local_contact_entry
    _contact_entry = ConversationDataDialog._contact_entry

    def __init__(self, mw):
        self._mw, self._jid = mw, LID


@pytest.fixture(autouse=True)
def _no_wx_no_sleep(monkeypatch):
    monkeypatch.setattr(identity_module.wx, "CallAfter", lambda *a, **k: None)
    monkeypatch.setattr(contacts_module.wx, "CallAfter", lambda *a, **k: None)
    monkeypatch.setattr(identity_module.time, "sleep", lambda seconds: None)


def _saved(entry):
    mw = _Window()
    mw.save_local_contact(LID, entry)
    return mw


def _resolution_answers(monkeypatch, body):
    monkeypatch.setattr(identity_module, "api_get", lambda url, **kwargs: SimpleNamespace(
        status_code=200, text="", json=lambda: body))


class TestWhileOnlyTheLidIsKnown:
    def test_a_local_contact_is_filed_under_the_lid_and_found(self):
        mw = _saved(pc.local_entry(LID, "Ana Silva"))
        assert mw._lid_for_local_contact(LID) == ""        # an @lid has no @lid of its own
        assert mw.contacts[LID]["name"] == "Ana Silva"
        assert _Dialog(mw)._contact_entry() is mw.contacts[LID]
        assert mw._resolve_contact_name(mw.chats[LID]) == "Ana Silva"


class TestOnceTheBridgeIsLearned:
    def test_the_contact_follows_the_person_to_the_phone_jid(self):
        mw = _saved(pc.local_entry(LID, "Ana Silva"))

        mw.register_jid_mapping(LID, PHONE)

        dialog = _Dialog(mw)
        assert dialog._resolve_contact_phone_jid() == PHONE
        entry = dialog._contact_entry()
        assert entry is not None and entry["name"] == "Ana Silva" and entry["isSaved"] is True
        assert entry["remoteJid"] == PHONE
        assert mw.db.upserted[PHONE]["name"] == "Ana Silva"      # and it is persisted

    def test_it_is_persisted_even_by_the_batch_callers(self):
        """resolve_lid_jids_via_api() registers with save=False and persists
        only what it changed itself."""
        mw = _saved(pc.local_entry(LID, "Ana Silva"))
        mw.db.upserted.clear()
        mw.register_jid_mapping(LID, PHONE, save=False, defer_ui=True)
        assert mw.db.upserted[PHONE]["isSaved"] is True

    def test_a_contact_saved_to_the_phone_keeps_its_mark_there(self):
        mw = _saved(pc.synced_entry(LID, "Ana Silva"))
        mw.register_jid_mapping(LID, PHONE)
        assert pc.is_phone_synced(mw.contacts[PHONE])
        assert pc.is_phone_synced(_Dialog(mw)._contact_entry())

    def test_what_whatsapp_already_knew_of_the_phone_jid_is_kept(self):
        mw = _saved(pc.local_entry(LID, "Ana Silva"))
        mw.contacts[PHONE] = {"id": PHONE, "remoteJid": PHONE, "name": "aninha",
                              "profilePicUrl": "pic"}
        mw.register_jid_mapping(LID, PHONE)
        assert mw.contacts[PHONE]["name"] == "Ana Silva"
        assert mw.contacts[PHONE]["profilePicUrl"] == "pic"

    def test_a_contact_the_user_saved_under_the_phone_jid_is_not_overwritten(self):
        mw = _saved(pc.local_entry(LID, "Ana (lid)"))
        mw.contacts[PHONE] = pc.local_entry(PHONE, "Ana (phone)")
        mw.register_jid_mapping(LID, PHONE)
        assert mw.contacts[PHONE]["name"] == "Ana (phone)"

    def test_an_lid_record_the_user_did_not_save_goes_nowhere(self):
        """Only a contact the user made follows; a pushname cache entry or a
        contact WhatsApp reports does not create a record under the phone."""
        mw = _Window()
        mw.contacts[LID] = {"id": LID, "remoteJid": LID, "name": "aninha",
                            "isMyContact": True, "syncToAddressbook": True}
        mw.register_jid_mapping(LID, PHONE)
        assert PHONE not in mw.contacts


PHONE_8 = "551199999999@s.whatsapp.net"     # PHONE without the 9th digit


def _from_the_phone_book(name):
    """An @lid record as get_contacts() restores it for a contact the user
    added on the PHONE: never saved through WinZapp, marked as synced."""
    return {"id": LID, "remoteJid": LID, "name": name, "pushName": name,
            "profilePicUrl": "", "type": "contact", "isSaved": False, pc.SYNCED_KEY: True}


class TestWhatsAppsOwnContactsAreNotTheUsers:
    """After a restart every contact of the phone's address book carries the
    synced mark (the database restores it). They are WhatsApp's records: the
    mark must not make them follow the bridge or shield them from a name."""

    def test_a_restored_address_book_record_does_not_follow(self):
        mw = _Window()
        mw.contacts[LID] = _from_the_phone_book("Ana Agenda")
        mw.contacts[PHONE] = {"id": PHONE, "remoteJid": PHONE, "name": "Ana Telefone"}
        mw.register_jid_mapping(LID, PHONE)
        assert mw.contacts[PHONE]["name"] == "Ana Telefone"
        assert PHONE not in mw.db.upserted

    def test_a_nameless_one_does_not_blank_the_phone_records_name(self):
        mw = _Window()
        mw.contacts[LID] = _from_the_phone_book("")
        mw.contacts[PHONE] = {"id": PHONE, "remoteJid": PHONE, "name": "Ana Telefone"}
        mw.register_jid_mapping(LID, PHONE)
        assert mw.contacts[PHONE]["name"] == "Ana Telefone"

    def test_it_creates_no_record_under_the_phone_jid(self):
        mw = _Window()
        mw.contacts[LID] = _from_the_phone_book("Ana Agenda")
        mw.register_jid_mapping(LID, PHONE)
        assert PHONE not in mw.contacts

    def test_the_resolution_still_names_a_nameless_one(self, monkeypatch):
        mw = _Window()
        mw.contacts[LID] = _from_the_phone_book("")
        _resolution_answers(monkeypatch, {"lid": {"_serialized": LID},
                                          "contact": {"pushname": "aninha"}})
        mw.resolve_lid_jids_via_api([LID])
        assert mw.contacts[LID]["name"] == "aninha"


class TestTheOtherDigitFormOfThePhone:
    def test_a_contact_saved_under_it_is_not_doubled(self):
        """Saved under the number without the 9th digit; the bridge is learned
        with it. The user's record for that number stands, and no second saved
        record appears next to it."""
        mw = _saved(pc.local_entry(LID, "Ana (lid)"))
        mw.contacts[PHONE_8] = pc.local_entry(PHONE_8, "Ana (phone)")
        mw.register_jid_mapping(LID, PHONE)
        assert PHONE not in mw.contacts
        assert mw.contacts[PHONE_8]["name"] == "Ana (phone)"


class TestTheResolutionDoesNotRenameIt:
    def test_a_pushname_does_not_replace_the_name_the_user_gave(self, monkeypatch):
        mw = _saved(pc.local_entry(LID, "Ana Silva"))
        _resolution_answers(monkeypatch, {"lid": {"_serialized": LID},
                                          "contact": {"pushname": "aninha"}})

        mw.resolve_lid_jids_via_api([LID])

        assert mw.contacts[LID]["name"] == "Ana Silva"
        assert mw._resolve_contact_name(mw.chats[LID]) == "Ana Silva"

    def test_nor_when_the_phone_is_learned_in_the_same_answer(self, monkeypatch):
        mw = _saved(pc.local_entry(LID, "Ana Silva"))
        _resolution_answers(monkeypatch, {"lid": {"_serialized": LID}, "phoneNumber": PN,
                                          "contact": {"pushname": "aninha"}})

        mw.resolve_lid_jids_via_api([LID])

        assert mw._lid_to_phone == {LID: PHONE}
        assert mw.contacts[LID]["name"] == "Ana Silva"
        assert mw.contacts[PHONE]["name"] == "Ana Silva"
        assert _Dialog(mw)._contact_entry()["name"] == "Ana Silva"

    def test_a_record_the_user_did_not_save_still_gets_the_name(self, monkeypatch):
        """What the resolution is for."""
        mw = _Window()
        _resolution_answers(monkeypatch, {"lid": {"_serialized": LID}, "phoneNumber": PN,
                                          "contact": {"pushname": "aninha"}})
        mw.resolve_lid_jids_via_api([LID])
        assert mw.contacts[LID]["name"] == "aninha"
        assert mw.contacts[PHONE]["name"] == "aninha"


class TestStoreResolvedName:
    def test_it_creates_the_record_when_there_is_none(self):
        mw, updated = _Window(), {}
        mw._store_resolved_name(PHONE, "aninha", updated)
        assert mw.contacts[PHONE] == {"name": "aninha", "pushName": "aninha"}
        assert updated == {PHONE: mw.contacts[PHONE]}

    def test_it_leaves_a_saved_record_and_reports_no_change(self):
        mw, updated = _Window(), {}
        mw.contacts[PHONE] = pc.synced_entry(PHONE, "Ana Silva")
        mw._store_resolved_name(PHONE, "aninha", updated)
        assert mw.contacts[PHONE]["name"] == "Ana Silva" and updated == {}
