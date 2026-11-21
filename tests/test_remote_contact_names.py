"""Public nicknames in all-contacts must work for group-only senders too."""
from types import SimpleNamespace

import pytest
from core.remote_contact_names import learn_remote_profiles, profile_name
from core.unsaved_contact_marker import contact_is_saved
from main_window import contacts as module
from main_window.contacts import ContactsMixin

PHONE = "5511999990000@s.whatsapp.net"
TWIN = "551199990000@s.whatsapp.net"
LID = "123456789012345@lid"
normalize = lambda jid: jid.replace("@c.us", "@s.whatsapp.net")
invalid = ContactsMixin._is_bad_contact_name


@pytest.mark.parametrize("field", ["pushName", "pushname"])
def test_public_nickname_is_cached_for_a_group_only_person(field):
    contacts = {}
    learned = learn_remote_profiles(contacts, [
        {"id": {"_serialized": PHONE.replace("@s.whatsapp.net", "@c.us")},
         field: "  aninha  ", "isMyContact": False}], normalize, invalid, {}, {PHONE: LID})
    assert set(learned) == {PHONE, LID}
    for contact in contacts.values():
        assert contact["pushName"] == "aninha"
        assert not contact.get("name") and not contact_is_saved(contact)


def test_lid_nickname_keeps_a_local_name_and_uses_the_existing_ninth_digit_form():
    contacts = {TWIN: {"name": "Nome da agenda", "isSaved": True}}
    learn_remote_profiles(contacts, [{"id": LID, "pushname": "apelido"}],
                          normalize, invalid, {LID: PHONE}, {})
    assert PHONE not in contacts
    assert contacts[TWIN]["name"] == "Nome da agenda"
    assert contacts[TWIN]["pushName"] == "apelido"
    assert contacts[LID]["pushName"] == "apelido"


@pytest.mark.parametrize("value", [None, "", "5511999990000", "+55••••76", "Unknown User"])
def test_bad_nickname_does_not_replace_a_known_one(value):
    contacts = {PHONE: {"pushName": "conhecido"}}
    assert learn_remote_profiles(contacts, [{"id": PHONE, "pushname": value}],
                                 normalize, invalid, {}, {}) == {}
    assert contacts[PHONE]["pushName"] == "conhecido"


def test_valid_lowercase_nickname_is_used_if_the_other_spelling_is_invalid():
    assert profile_name({"pushName": "Unknown User", "pushname": "Ana"}, invalid) == "Ana"


def test_repeated_contact_polls_do_not_request_another_message_repaint():
    contacts = {PHONE: {"pushName": "Ana"}}
    assert learn_remote_profiles(contacts, [{"id": PHONE, "pushname": "Ana"}],
                                 normalize, invalid, {}, {}) == {}


def test_non_people_and_malformed_rows_are_not_cached():
    contacts = {}
    rows = [None, {"id": "group@g.us", "pushname": "grupo"},
            {"id": "channel@newsletter", "pushname": "canal"},
            {"id": 42, "pushname": "invalid"}]
    assert learn_remote_profiles(contacts, rows, normalize, invalid, {}, {}) == {}


@pytest.mark.parametrize("active", [False, True])
def test_real_contact_sync_keeps_nickname_separate_from_the_phonebook(monkeypatch, active):
    refreshed = []
    monkeypatch.setattr(module.wx, "CallAfter", lambda f, *a: f(*a))
    rows = [{"id": {"_serialized": PHONE.replace("@s.whatsapp.net", "@c.us")},
             "pushname": "aninha", "isMyContact": False, "syncToAddressbook": False}]
    monkeypatch.setattr(module, "api_get", lambda *a, **kw: SimpleNamespace(
        status_code=200, json=lambda: {"response": rows}))
    window = SimpleNamespace(
        wpp_server="http://localhost", wpp_port=6300, token="test",
        contacts={PHONE: {"name": "Nome local", "isSaved": True}} if active else {},
        chats={PHONE: {}} if active else {},
        _normalize_jid=normalize, _lid_to_phone={}, _phone_to_lid={PHONE: LID},
        _clear_stale_phone_sync_marks=lambda *a: None,
        _schedule_refresh_active_messages=refreshed.append,
        _schedule_save=lambda **kw: None)
    window._learn_address_book_lids = lambda rows: ContactsMixin._learn_address_book_lids(window, rows)
    result = ContactsMixin.get_remote_contacts(window)
    assert result[PHONE]["pushName"] == "aninha"
    assert result[LID]["pushName"] == "aninha"
    assert not contact_is_saved(result[LID])
    assert refreshed == [{PHONE, LID}]
    if active:
        assert result[PHONE]["name"] == "Nome local"
    else:
        assert not contact_is_saved(result[PHONE])


def test_an_lid_row_with_its_number_is_one_person_with_that_number():
    """All-contacts rows carry `phoneNumber` on an @lid (both engines); without
    it 144 saved @lid contacts of a real account had no number and showed twice
    in "Add participant" (2026-11-17)."""
    from core.remote_contact_names import lid_phone_pairs
    rows = [{"id": {"_serialized": "77@lid"}, "phoneNumber": "5511999990000@c.us"},
            {"id": "88@lid", "phoneNumber": {"_serialized": "5522999990000@c.us"}},
            {"id": "99@lid"},                                            # no number
            {"id": "5533@c.us", "phoneNumber": "5533@c.us"},             # not an @lid
            {"id": "66@lid", "phoneNumber": "1203@g.us"},                # not a number
            None]
    assert lid_phone_pairs(rows, normalize) == [
        ("77@lid", "5511999990000@s.whatsapp.net"), ("88@lid", "5522999990000@s.whatsapp.net")]


def test_the_contact_sync_registers_only_the_pairs_it_did_not_know(monkeypatch):
    scheduled = []
    monkeypatch.setattr(module.wx, "CallAfter", lambda f, *a: scheduled.append(f))
    registered = []
    window = SimpleNamespace(
        _normalize_jid=normalize, _lid_to_phone={"77@lid": "5511999990000@s.whatsapp.net"},
        register_jid_mapping=lambda lid, phone, **kw: registered.append((lid, phone, kw)),
        _schedule_set_chats="refresh")
    rows = [{"id": "77@lid", "phoneNumber": "5511999990000@c.us"},
            {"id": "88@lid", "phoneNumber": "5522999990000@c.us"}]
    ContactsMixin._learn_address_book_lids(window, rows)
    assert registered == [("88@lid", "5522999990000@s.whatsapp.net", {"defer_ui": True})]
    assert scheduled == ["refresh"]
    registered.clear(), scheduled.clear()
    ContactsMixin._learn_address_book_lids(window, rows[:1])
    assert registered == [] and scheduled == []


def test_rows_of_one_person_that_disagree_do_not_flip_the_name_at_each_refresh():
    from core.remote_contact_names import learn_remote_profiles
    phone, lid = "5511@s.whatsapp.net", "9@lid"
    contacts = {phone: {"name": "alex", "pushName": "alex grupos"},
                lid: {"name": "alex", "pushName": "alex grupos"}}
    rows = [{"remoteJid": phone, "pushname": "alex"},
            {"remoteJid": lid, "pushname": "alex grupos"}]
    for _ in range(3):
        for ordered in (rows, rows[::-1]):
            learn_remote_profiles(contacts, ordered, lambda j: j, lambda n: False,
                                  {lid: phone}, {phone: lid})
            assert contacts[phone]["pushName"] == contacts[lid]["pushName"] == "alex grupos"
            assert contacts[phone]["name"] == contacts[lid]["name"] == "alex grupos"
