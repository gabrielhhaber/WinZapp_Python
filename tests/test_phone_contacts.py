"""Contacts saved to WhatsApp and synced to the phone (core/phone_contacts.py).

The request shapes, the answer-to-message mapping and the contact records are
plain functions over an injected ``post``: no network, no wx.
"""

import json
from types import SimpleNamespace

import pytest

import main
from app_paths import resource_path
from core import phone_contacts as pc
from main import MainWindow
from main_window import contacts as contacts_module
from main_window.contacts import ContactsMixin
from tests.test_local_contact_sync import LID, PHONE, _Mw

PHONE_8 = "551199999999@s.whatsapp.net"     # PHONE without the 9th digit


def _resp(status=200, body=None):
    return SimpleNamespace(ok=status < 400, status_code=status, json=lambda: body or {})


class _Post:
    def __init__(self, response=None, raises=None):
        self.calls, self._response, self._raises = [], response, raises

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self._raises:
            raise self._raises
        return self._response


class TestWhatTravelsToTheServer:
    def test_a_phone_number_travels_as_digits(self):
        assert pc.wire_id("5511999999999@s.whatsapp.net") == "5511999999999"
        assert pc.wire_id("5511999999999@c.us") == "5511999999999"
        assert pc.wire_id("+55 (11) 99999-9999") == "5511999999999"
        assert pc.wire_id("5511999999999:7@s.whatsapp.net") == "5511999999999"

    def test_an_lid_travels_whole(self):
        """Its digits are not a phone number: sent as one, they would save or
        remove whoever owns that number."""
        assert pc.wire_id("123456789012345@lid") == "123456789012345@lid"
        assert pc.wire_id("123456789012345:2@lid") == "123456789012345@lid"

    @pytest.mark.parametrize("jid", ["120363067944453158@g.us", "status@broadcast",
                                     "123@newsletter", "12345", "", "abc@lid", None])
    def test_anything_else_is_refused(self, jid):
        assert pc.wire_id(jid) == ""

    def test_the_servers_id_becomes_the_jid_contacts_are_keyed_by(self):
        assert pc.jid_from_server("551199999999@c.us", PHONE) == PHONE_8
        assert pc.jid_from_server("123@lid", PHONE) == "123@lid"
        for odd in (None, "", "no-at-sign", "x@g.us", 5):
            assert pc.jid_from_server(odd, PHONE) == PHONE


class TestSaveContact:
    def test_the_request_asks_for_the_phone_sync(self):
        post = _Post(_resp(200, {"status": "success", "response": {
            "id": "5511999999999@c.us", "isMyContact": True, "syncToAddressbook": True}}))
        result = pc.save_contact("http://127.0.0.1:6300", "tok", PHONE, "Ana", "Silva", post=post)
        assert result == pc.SaveResult(True, "", PHONE, True)
        url, kwargs = post.calls[0]
        assert url == "http://127.0.0.1:6300/api/tok/save-contact"
        assert kwargs["json"] == {"phone": "5511999999999", "name": "Ana",
                                  "lastName": "Silva", "syncAddressBook": True}
        assert kwargs["headers"]["Authorization"] == "Bearer tok"

    def test_the_jid_whatsapp_filed_it_under_is_answered(self):
        """Typed with the 9th digit, filed by WhatsApp without it."""
        post = _Post(_resp(200, {"response": {
            "id": "551199999999@c.us", "isMyContact": True, "syncToAddressbook": True}}))
        assert pc.save_contact("b", "t", PHONE, "Ana", post=post).jid == PHONE_8

    def test_a_save_whose_sync_was_not_confirmed_says_so(self):
        post = _Post(_resp(200, {"response": {
            "id": "5511999999999@c.us", "isMyContact": True, "syncToAddressbook": False}}))
        result = pc.save_contact("b", "t", PHONE, "Ana", post=post)
        assert result.ok is True and result.synced is False

    def test_an_answer_with_no_details_is_a_save_without_a_confirmed_sync(self):
        result = pc.save_contact("b", "t", PHONE, "Ana", post=_Post(_resp(200, {})))
        assert result == pc.SaveResult(True, "", PHONE, False)

    def test_an_lid_is_sent_whole(self):
        post = _Post(_resp(200, {"response": {"id": "123456789012345@lid"}}))
        pc.save_contact("b", "t", "123456789012345@lid", "Ana", post=post)
        assert post.calls[0][1]["json"]["phone"] == "123456789012345@lid"

    def test_what_is_not_a_contact_is_refused_without_a_request(self):
        post = _Post(_resp())
        for jid in ("12345", "120363067944453158@g.us"):
            assert pc.save_contact("b", "t", jid, "Ana", post=post) == pc.SaveResult(
                False, pc.ERR_INVALID, jid, False)
        assert post.calls == []

    def test_a_number_that_is_not_on_whatsapp(self):
        """The server's contact validation answers 400 with no code."""
        post = _Post(_resp(400, {"status": "error", "message": "O número não existe."}))
        assert pc.save_contact("b", "t", PHONE, "Ana", post=post).error_key == pc.ERR_NOT_ON_WHATSAPP

    def test_the_servers_invalid_contact_code(self):
        post = _Post(_resp(400, {"status": "error", "code": "contact_invalid"}))
        assert pc.save_contact("b", "t", PHONE, "Ana", post=post).error_key == pc.ERR_INVALID

    def test_an_lid_whose_phone_whatsapp_does_not_know_yet(self):
        """Not "try again": nothing to retry now, the local tab is the way."""
        post = _Post(_resp(400, {"status": "error", "code": "contact_lid_without_phone"}))
        result = pc.save_contact("b", "t", "123456789012345@lid", "Ana", post=post)
        assert (result.ok, result.error_key) == (False, pc.ERR_LID_WITHOUT_PHONE)

    def test_any_other_failure_is_the_generic_message(self):
        for response in (_resp(502, {"code": "contact_operation_failed"}),
                         _resp(404, {"status": "Disconnected"}),
                         _resp(503, {"code": "contact_not_available"}),
                         _resp(500, ["not", "a", "dict"])):
            result = pc.save_contact("b", "t", PHONE, "Ana", post=_Post(response))
            assert (result.ok, result.error_key) == (False, pc.ERR_FAILED)

    def test_a_network_error_is_the_generic_message(self):
        result = pc.save_contact("b", "t", PHONE, "Ana", post=_Post(raises=ConnectionError("down")))
        assert (result.ok, result.error_key) == (False, pc.ERR_FAILED)

    def test_the_error_keys_are_the_ones_the_locales_define(self):
        with open(resource_path("languages", "pt-BR.json"), encoding="utf-8") as f:
            strings = json.load(f)
        assert {pc.ERR_INVALID, pc.ERR_NOT_ON_WHATSAPP, pc.ERR_LID_WITHOUT_PHONE,
                pc.ERR_FAILED} <= set(strings)


class TestRemoveContact:
    def test_it_asks_the_server_to_remove_it(self):
        post = _Post(_resp(200))
        assert pc.remove_contact("http://h:1", "tok", PHONE, post=post) is True
        assert post.calls[0][0] == "http://h:1/api/tok/remove-contact"
        assert post.calls[0][1]["json"] == {"phone": "5511999999999"}

    def test_an_lid_is_removed_as_an_lid(self):
        post = _Post(_resp(200))
        assert pc.remove_contact("b", "t", "123456789012345@lid", post=post) is True
        assert post.calls[0][1]["json"] == {"phone": "123456789012345@lid"}

    def test_a_contact_that_is_already_gone_counts_as_removed(self):
        for code in ("contact_not_found", "number_is_not_your_contact"):
            assert pc.remove_contact("b", "t", PHONE, post=_Post(_resp(400, {"code": code}))) is True

    def test_a_failure_is_not_removal(self):
        for post in (_Post(_resp(502, {"code": "contact_operation_failed"})),
                     _Post(_resp(400, {"message": "no code"})),
                     _Post(raises=OSError("x"))):
            assert pc.remove_contact("b", "t", PHONE, post=post) is False

    def test_what_is_not_a_contact_sends_nothing(self):
        post = _Post(_resp())
        assert pc.remove_contact("b", "t", "123", post=post) is False
        assert pc.remove_contact("b", "t", "x@g.us", post=post) is False
        assert post.calls == []


class TestRecords:
    def test_a_synced_entry_is_marked_and_saved(self):
        entry = pc.synced_entry(PHONE, "Ana Silva")
        assert entry["isSaved"] is True and entry[pc.SYNCED_KEY] is True
        assert entry["isMyContact"] is True and entry["syncToAddressbook"] is True
        assert entry["name"] == entry["pushName"] == "Ana Silva"

    def test_a_local_entry_is_not_marked_as_synced(self):
        entry = pc.local_entry(PHONE, "Ana")
        assert entry["isSaved"] is True and pc.SYNCED_KEY not in entry

    def test_a_save_whose_sync_was_not_confirmed_yet_is_marked_all_the_same(self):
        """It was asked for with the sync and accepted, and it is in WhatsApp
        either way: handled as a local contact, deleting it would remove it
        from WinZapp only. The next contact list corrects the mark if needed."""
        entry = pc.synced_entry(PHONE, "Ana", confirmed=False)
        assert entry["syncToAddressbook"] is False and entry["isMyContact"] is True
        assert pc.is_phone_synced(entry) is True

    def test_and_the_next_list_takes_the_mark_away_when_it_really_did_not_sync(self):
        contacts = {PHONE: pc.synced_entry(PHONE, "Ana", confirmed=False)}
        later = pc.now() + 60
        assert pc.clear_stale_marks(contacts, [_server(PHONE, True, False)], later) == [PHONE]
        assert pc.is_phone_synced(contacts[PHONE]) is False

    def test_unmark_takes_the_flags_with_the_mark(self):
        record = pc.synced_entry(PHONE, "Ana")
        pc.unmark(record, True, False)
        assert pc.is_phone_synced(record) is False and record["isMyContact"] is True

    def test_the_http_client_is_not_loaded_just_to_read_a_mark(self):
        """core/database.py imports this module for SYNCED_KEY alone. Checked
        in a fresh interpreter: in this one something else has loaded it."""
        import subprocess
        import sys
        code = ("import sys; import core.phone_contacts; "
                "print('core.api_client' in sys.modules, 'requests' in sys.modules)")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             cwd=resource_path(), timeout=60)
        assert out.stdout.split() == ["False", "False"], out.stderr

    def test_who_made_the_record(self):
        """The user's own records keep their name and follow the person."""
        assert pc.user_saved(pc.local_entry(PHONE, "Ana")) is True
        assert pc.user_saved(pc.synced_entry(PHONE, "Ana")) is True
        assert pc.user_saved({pc.SYNCED_KEY: True}) is True       # restored from the database
        assert pc.user_saved({"isMyContact": True, "syncToAddressbook": True}) is False
        assert pc.user_saved({"name": "aninha"}) is False
        assert pc.user_saved(None) is False

    def test_is_phone_synced(self):
        assert pc.is_phone_synced(pc.synced_entry("j", "n")) is True
        assert pc.is_phone_synced(pc.local_entry("j", "n")) is False
        assert pc.is_phone_synced(None) is False


class TestWhichContactsAreInThePhoneBook:
    def test_whatsapp_marking_a_contact_as_synced_counts(self):
        """Added on the phone, or in WhatsApp Web: WinZapp never wrote its own
        marker, but WhatsApp says it is saved and synced."""
        assert pc.is_phone_synced({"isMyContact": True, "syncToAddressbook": True}) is True

    def test_saved_in_whatsapp_only_does_not_count(self):
        assert pc.is_phone_synced({"isMyContact": True, "syncToAddressbook": False}) is False
        assert pc.is_phone_synced({"isMyContact": False, "syncToAddressbook": True}) is False
        assert pc.is_phone_synced({"name": "Ana"}) is False

    def test_the_lookup_is_tolerant_of_the_brazilian_ninth_digit(self):
        """Against the real MainWindow._get_contact_tolerant."""
        entry = {"isMyContact": True, "syncToAddressbook": True}
        mw = _Mw()
        mw.contacts = {PHONE_8: entry}
        assert pc.existing_contact(mw, PHONE) is entry
        assert pc.key_of(mw, entry, PHONE) == PHONE_8

    def test_an_lid_has_no_lid_of_its_own(self):
        """_lid_for_local_contact() is also called with the @lid a contact was
        saved under; its digits must not be compared with phone numbers."""
        mw = _Mw()
        mw._phone_to_lid = {"123456789012345@s.whatsapp.net": "999@lid"}
        assert mw._lid_for_local_contact("123456789012345@lid") == ""
        assert mw._lid_for_local_contact("123456789012345@s.whatsapp.net") == "999@lid"

    def test_the_key_of_a_record_that_is_not_stored_is_the_default(self):
        mw = _Mw()
        assert pc.key_of(mw, {"name": "stray"}, PHONE) == PHONE
        assert pc.key_of(mw, None, PHONE) == PHONE

    def test_a_window_without_the_tolerant_lookup_falls_back_to_a_plain_get(self):
        entry = {"isMyContact": True}
        assert pc.existing_contact(SimpleNamespace(contacts={"j": entry}), "j") is entry
        assert pc.existing_contact(SimpleNamespace(), "j") is None

    def test_a_synced_contact_leaves_only_the_synced_tab(self):
        both = ("local", "phone")
        synced = {"isMyContact": True, "syncToAddressbook": True}
        assert pc.available_modes(synced, both, "phone") == ("phone",)
        assert pc.available_modes(pc.synced_entry("j", "n"), both, "phone") == ("phone",)
        assert pc.available_modes(pc.local_entry("j", "n"), both, "phone") == both
        assert pc.available_modes(None, both, "phone") == both


def _server(jid, mine, synced):
    return {"remoteJid": jid, "isMyContact": mine, "syncToAddressbook": synced}


class TestWhatsAppsListIsTheTruth:
    """A contact removed on the phone keeps its mark here unless the list
    WhatsApp sends is applied to it."""

    def test_a_marked_contact_whatsapp_no_longer_has_loses_the_mark(self):
        contacts = {PHONE: {"isSaved": True, pc.SYNCED_KEY: True,
                            "isMyContact": True, "syncToAddressbook": True}}
        cleared = pc.clear_stale_marks(contacts, [_server(PHONE, False, False)], pc.now())
        assert cleared == [PHONE]
        assert pc.is_phone_synced(contacts[PHONE]) is False
        assert contacts[PHONE]["isSaved"] is True     # it is a local contact now

    def test_saved_in_whatsapp_but_no_longer_synced_also_loses_it(self):
        contacts = {PHONE: {pc.SYNCED_KEY: True}}
        assert pc.clear_stale_marks(contacts, [_server(PHONE, True, False)], pc.now()) == [PHONE]

    def test_a_contact_whatsapp_confirms_keeps_it(self):
        contacts = {PHONE: {pc.SYNCED_KEY: True}}
        assert pc.clear_stale_marks(contacts, [_server(PHONE, True, True)], pc.now()) == []
        assert contacts[PHONE][pc.SYNCED_KEY] is True

    def test_a_list_asked_for_before_the_save_does_not_undo_it(self):
        """WhatsApp did not know the contact yet when that list was requested."""
        requested_at = pc.now() - 5
        contacts = {PHONE: pc.synced_entry(PHONE, "Ana")}      # saved after the request
        assert pc.clear_stale_marks(contacts, [_server(PHONE, False, False)], requested_at) == []
        assert pc.is_phone_synced(contacts[PHONE]) is True

    def test_an_entry_with_no_answer_changes_nothing(self):
        contacts = {PHONE: {pc.SYNCED_KEY: True}}
        entries = [{"remoteJid": PHONE}, {"remoteJid": PHONE, "isMyContact": False},
                   "junk", {"isMyContact": False, "syncToAddressbook": False}]
        assert pc.clear_stale_marks(contacts, entries, pc.now()) == []

    def test_unmarked_records_and_unknown_numbers_are_left_alone(self):
        contacts = {PHONE: {"isSaved": True}}
        server = [_server(PHONE, False, False), _server("other@s.whatsapp.net", False, False)]
        assert pc.clear_stale_marks(contacts, server, pc.now()) == []
        assert contacts == {PHONE: {"isSaved": True}}

    def test_an_empty_list_clears_nothing(self):
        """A failed fetch must not read as "everyone was removed"."""
        contacts = {PHONE: {pc.SYNCED_KEY: True}}
        assert pc.clear_stale_marks(contacts, [], pc.now()) == []
        assert pc.clear_stale_marks(contacts, None, pc.now()) == []

    def test_a_number_absent_from_the_list_keeps_its_mark(self):
        """On purpose: while WhatsApp Web is still loading its store everyone
        is absent, and reading that as "removed" would unmark them all."""
        contacts = {PHONE: {pc.SYNCED_KEY: True}}
        other = _server("5511888888888@s.whatsapp.net", True, True)
        assert pc.clear_stale_marks(contacts, [other], pc.now()) == []
        assert contacts[PHONE][pc.SYNCED_KEY] is True

    def test_the_lid_copy_stops_reading_as_synced_with_the_phone_record(self, monkeypatch):
        """Through the real MainWindow methods, on the records save_local_contact()
        really makes: the copy carries the two WhatsApp flags as well as the
        mark, and either would bring "synced" back."""
        monkeypatch.setattr(main.wx, "CallAfter", lambda fn, *a, **k: fn(*a, **k))

        class _Window(_Mw):
            _clear_stale_phone_sync_marks = ContactsMixin._clear_stale_phone_sync_marks

        mw = _Window()
        entry = pc.synced_entry(PHONE, "Ana")
        entry[pc.SYNCED_AT_KEY] = 0             # saved long before this list
        mw.save_local_contact(PHONE, entry)
        assert pc.is_phone_synced(mw.contacts[LID])

        mw._clear_stale_phone_sync_marks([_server(PHONE, False, False)], pc.now())

        assert pc.is_phone_synced(mw.contacts[PHONE]) is False
        assert pc.is_phone_synced(mw.contacts[LID]) is False


class TestASyncedContactReplacesTheLocalOne:
    """One record per number: saving it as synced overwrites the local one, on
    the phone JID and on the @lid copy, so no local-only copy is left."""

    def test_the_local_record_becomes_the_synced_one(self, monkeypatch):
        monkeypatch.setattr(main.wx, "CallAfter", lambda fn, *a, **k: fn(*a, **k))
        mw = _Mw()
        mw.save_local_contact(PHONE, pc.local_entry(PHONE, "Ana (local)"))
        assert not pc.is_phone_synced(mw.contacts[PHONE])

        mw.save_local_contact(PHONE, pc.synced_entry(PHONE, "Ana Silva"))

        for jid in (PHONE, LID):
            assert mw.contacts[jid]["name"] == "Ana Silva"
            assert pc.is_phone_synced(mw.contacts[jid])


class TestMainWindowCalls:
    """ContactsMixin runs the request on a thread and answers on the wx thread."""

    class _MW:
        wpp_server, wpp_port, token = "http://127.0.0.1", 6300, "tok"
        save_phone_synced_contact = MainWindow.save_phone_synced_contact
        remove_phone_synced_contact = MainWindow.remove_phone_synced_contact

    @pytest.fixture(autouse=True)
    def _inline(self, monkeypatch):
        class _Thread:
            def __init__(self, target, **kwargs): self._target = target
            def start(self): self._target()

        monkeypatch.setattr(contacts_module.threading, "Thread", _Thread)
        monkeypatch.setattr(contacts_module.wx, "CallAfter", lambda fn, *a: fn(*a))

    def test_save_answers_with_the_result(self, monkeypatch):
        seen, answer = [], pc.SaveResult(True, "", PHONE_8, True)
        monkeypatch.setattr(contacts_module.phone_contacts, "save_contact",
                            lambda *args: seen.append(args) or answer)
        answers = []
        self._MW().save_phone_synced_contact(PHONE, "Ana", "Silva", answers.append)
        assert seen == [("http://127.0.0.1:6300", "tok", PHONE, "Ana", "Silva")]
        assert answers == [answer]

    def test_remove_answers_with_the_result(self, monkeypatch):
        monkeypatch.setattr(contacts_module.phone_contacts, "remove_contact",
                            lambda base, token, jid: False)
        answers = []
        self._MW().remove_phone_synced_contact(PHONE, answers.append)
        assert answers == [False]


class TestTheContactSyncAppliesIt:
    """get_remote_contacts() itself, with the HTTP call stubbed."""

    class _Window(_Mw):
        get_remote_contacts = MainWindow.get_remote_contacts
        _clear_stale_phone_sync_marks = MainWindow._clear_stale_phone_sync_marks
        wpp_server, wpp_port, token = "http://127.0.0.1", 6300, "tok"

        def __init__(self):
            super().__init__()
            # The server lists saved contacts and numbers with an open chat;
            # _Mw has a chat with PHONE, which is why a number that is no
            # longer a contact is still in the answer.
            assert PHONE in self.chats
            self.saved = []

        def _schedule_save(self, **kwargs):
            self.saved.append(kwargs)

    @staticmethod
    def _answering(monkeypatch, mine, synced, before_answering=None):
        def _get(url, **kwargs):
            if before_answering:
                before_answering()
            return SimpleNamespace(status_code=200, text="", json=lambda: {"response": [{
                "id": {"_serialized": "5511999999999@c.us"},
                "isMyContact": mine, "syncToAddressbook": synced}]})

        monkeypatch.setattr(contacts_module, "api_get", _get)

    def test_a_contact_removed_on_the_phone_stops_being_synced_here(self, monkeypatch):
        mw = self._Window()
        mw.contacts = {PHONE: {"remoteJid": PHONE, "name": "Ana", "isSaved": True,
                               pc.SYNCED_KEY: True}}
        self._answering(monkeypatch, mine=False, synced=False)

        mw.get_remote_contacts()

        assert pc.is_phone_synced(mw.contacts[PHONE]) is False
        assert mw.saved == [{"contacts_dirty": True}]

    def test_a_contact_still_on_the_phone_stays_synced(self, monkeypatch):
        mw = self._Window()
        mw.contacts = {PHONE: {"remoteJid": PHONE, "name": "Ana", pc.SYNCED_KEY: True}}
        self._answering(monkeypatch, mine=True, synced=True)
        mw.get_remote_contacts()
        assert pc.is_phone_synced(mw.contacts[PHONE]) is True

    def test_a_contact_saved_while_the_list_was_on_its_way_stays_synced(self, monkeypatch):
        mw = self._Window()

        def _saved_meanwhile():
            mw.contacts[PHONE] = pc.synced_entry(PHONE, "Ana")

        self._answering(monkeypatch, mine=False, synced=False, before_answering=_saved_meanwhile)
        mw.get_remote_contacts()
        assert mw.contacts[PHONE][pc.SYNCED_KEY] is True
