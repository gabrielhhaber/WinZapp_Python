"""Contact routes: the pure functions that build what the client reads."""
from types import SimpleNamespace as N

import contacts


def query(user, exists=True, business=False, server="s.whatsapp.net"):
    return N(IsIn=exists, JID=N(User=user if exists else "", Server=server),
             HasField=lambda field: business and field == "VerifiedName")


def test_contact_target_normalizes_the_number_forms():
    assert contacts.contact_target("5511999990000") == "5511999990000@s.whatsapp.net"
    assert contacts.contact_target("5511999990000@c.us") == "5511999990000@s.whatsapp.net"
    assert contacts.contact_target("5511999990000:12@s.whatsapp.net") == \
        "5511999990000@s.whatsapp.net"


def test_contact_target_keeps_the_whole_lid():
    assert contacts.contact_target("123456789012@lid") == "123456789012@lid"


def test_contact_target_refuses_what_is_not_a_person():
    for text in ("", None, "abc", "5511@c.us", "120363@g.us", "status@broadcast",
                 "5511999990000@newsletter"):
        assert contacts.contact_target(text) == ""


def test_first_destination_accepts_text_or_list():
    assert contacts.first_destination(["a", "b"]) == "a"
    assert contacts.first_destination("a") == "a"
    assert contacts.first_destination([]) == []


def test_number_forms_tries_the_ninth_digit_of_brazilian_mobiles():
    assert contacts.number_forms("5511999990000") == ["5511999990000", "551199990000"]
    assert contacts.number_forms("551199990000") == ["551199990000", "5511999990000"]


def test_number_forms_does_not_touch_landlines_nor_other_countries():
    assert contacts.number_forms("551133334444") == ["551133334444"]
    assert contacts.number_forms("14155550123") == ["14155550123"]


def test_found_in_query_takes_the_first_number_whatsapp_knows():
    found = contacts.found_in_query([query("", exists=False), query("5511888")])
    assert found == {"jid": "5511888@s.whatsapp.net", "business": False}


def test_found_in_query_marks_a_business_account_and_accepts_empty():
    assert contacts.found_in_query([query("5511888", business=True)])["business"] is True
    assert contacts.found_in_query([query("", exists=False)]) is None
    assert contacts.found_in_query([]) is None
    assert contacts.found_in_query(None) is None


def test_response_for_a_number_that_exists_uses_the_canonical_id():
    found = {"jid": "551199990000@s.whatsapp.net", "business": True}
    output = contacts.number_response("5511999990000", found)
    assert output["numberExists"] is True and output["canReceiveMessage"] is True
    assert output["status"] == 200 and output["isBusiness"] is True
    assert output["id"] == {"server": "c.us", "user": "551199990000",
                            "_serialized": "551199990000@c.us"}


def test_response_for_a_number_that_does_not_exist():
    output = contacts.number_response("5511999990000", None)
    assert output["numberExists"] is False and output["canReceiveMessage"] is False
    assert output["status"] == 404
    assert output["id"]["_serialized"] == "5511999990000@c.us"


def test_to_wid_only_accepts_a_number_or_lid():
    assert contacts.to_wid("+55 (11) 99999-0000") == "5511999990000@c.us"
    assert contacts.to_wid(["5511999990000"]) == "5511999990000@c.us"
    assert contacts.to_wid("5511999990000@s.whatsapp.net") == "5511999990000@c.us"
    assert contacts.to_wid("123456789012@lid") == "123456789012@lid"
    for bad in ("", None, "123", "120363000000@g.us", "status@broadcast", "abc@lid"):
        assert contacts.to_wid(bad) == ""


def test_validate_save_returns_the_fields():
    fields, code = contacts.validate_save({
        "phone": "5511999990000", "name": " Ana ", "lastName": " Souza ",
        "syncAddressBook": True})
    assert code == ""
    assert fields == {"wid": "5511999990000@c.us", "first": "Ana",
                      "last": "Souza", "sync": True}


def test_validate_save_syncs_unless_told_not_to():
    assert contacts.validate_save({"phone": "5511999990000", "name": "Ana"})[0]["sync"]
    assert not contacts.validate_save(
        {"phone": "5511999990000", "name": "Ana", "syncAddressBook": False})[0]["sync"]


def test_validate_save_checks_the_number_before_the_name():
    assert contacts.validate_save({"phone": "12", "name": ""}) == (None, "contact_invalid")
    assert contacts.validate_save({"phone": "5511999990000", "name": "  "}) == \
        (None, "contact_name_required")
    assert contacts.validate_save(None) == (None, "contact_invalid")


def test_validate_save_cuts_long_names():
    fields, _ = contacts.validate_save({"phone": "5511999990000", "name": "a" * 300})
    assert len(fields["first"]) == 100


def test_address_book_fields_carry_the_full_name_and_the_sync():
    fields = {"first": "Ana", "last": "Souza", "sync": True}
    assert contacts.address_book_fields(fields, "5511@s.whatsapp.net") == {
        "fullName": "Ana Souza", "firstName": "Ana", "saveOnPrimaryAddressbook": True,
        "pnJID": "5511@s.whatsapp.net"}
    fields = {"first": "Ana", "last": "", "sync": False}
    output = contacts.address_book_fields(fields, "5511@s.whatsapp.net", "99@lid")
    assert output["fullName"] == "Ana" and output["lidJID"] == "99@lid"
    assert output["saveOnPrimaryAddressbook"] is False


def test_contact_error_uses_the_reference_servers_code_and_status():
    assert contacts.contact_error("contact_invalid") == (
        400, {"status": "error", "code": "contact_invalid"})
    assert contacts.contact_error("contact_lid_without_phone")[0] == 400
    assert contacts.contact_error("contact_not_available")[0] == 503
    assert contacts.contact_error("contact_operation_failed")[0] == 502


def test_contact_error_never_repeats_exception_text():
    assert contacts.contact_error("Failure with 5511999990000") == (
        502, {"status": "error", "code": "contact_operation_failed"})


def test_save_response_returns_the_id_it_was_saved_under():
    assert contacts.save_response("551199990000:5@s.whatsapp.net", True) == {
        "id": "551199990000@c.us", "isMyContact": True, "syncToAddressbook": True}
    assert contacts.save_response("5511@s.whatsapp.net", False)["syncToAddressbook"] is False


def test_vcard_carries_the_name_and_the_waid():
    card = contacts.vcard_of("Ana Souza", "5511999990000")
    assert card.startswith("BEGIN:VCARD\nVERSION:3.0\n")
    assert "FN:Ana Souza\n" in card
    assert "TEL;type=CELL;waid=5511999990000:+5511999990000\n" in card
    assert card.endswith("END:VCARD")


def test_vcard_escapes_what_would_break_the_card():
    card = contacts.vcard_of("Ana;\nSouza, Jr", "5511999990000")
    assert "FN:Ana; Souza, Jr" not in card
    assert "FN:Ana\\; Souza\\, Jr\n" in card


def test_card_name_prefers_request_then_address_book_then_nickname_then_number():
    assert contacts.card_name({"name": "Ana"}, "55", "Mom") == "Mom"
    assert contacts.card_name({"name": "Ana", "pushname": "A"}, "55") == "Ana"
    assert contacts.card_name({"pushname": "A"}, "55") == "A"
    assert contacts.card_name({}, "55", "  ") == "55"


def test_set_name_joins_the_names():
    assert contacts.set_name(["Ana", "Bia"]) == "Ana, Bia"


class FakeRouter:
    def __init__(self):
        self.routes = []

    def add_get(self, path, _):
        self.routes.append(("GET", path))

    def add_post(self, path, _):
        self.routes.append(("POST", path))


def test_register_wires_the_contact_routes():
    r = FakeRouter()
    contacts.register_routes(r, session=None, ok=None, jid_from_text=None, jid_text=None,
                             destination_of=None, store_sent=None, quoted_context=None)
    assert set(r.routes) == {
        ("POST", "/api/{tok}/block-contact"),
        ("POST", "/api/{tok}/unblock-contact"),
        ("GET", "/api/{tok}/check-number-status/{phone}"),
        ("POST", "/api/{tok}/save-contact"),
        ("POST", "/api/{tok}/contact-vcard"),
    }


def test_equivalent_jids_puts_the_requested_form_last():
    assert contacts.equivalent_jids("5511999990000@s.whatsapp.net") == [
        "551199990000@s.whatsapp.net", "5511999990000@s.whatsapp.net"]
    assert contacts.equivalent_jids("551199990000@s.whatsapp.net") == [
        "5511999990000@s.whatsapp.net", "551199990000@s.whatsapp.net"]
    assert contacts.equivalent_jids("") == []


def test_equivalent_jids_leaves_lid_and_other_countries_as_they_are():
    assert contacts.equivalent_jids("5511999990000@lid") == ["5511999990000@lid"]
    assert contacts.equivalent_jids("14155550123@s.whatsapp.net") == [
        "14155550123@s.whatsapp.net"]
    assert contacts.equivalent_jids("1203-44@g.us") == ["1203-44@g.us"]


def test_book_identity_reads_the_number_behind_an_lid():
    rows = {"99@lid": {"name": "", "pushname": "aninha"},
            "5511999990000@s.whatsapp.net": {"name": "Ana Silva", "pushname": ""}}
    assert contacts.book_identity(rows, "99@lid", "5511999990000@s.whatsapp.net") == (
        "Ana Silva", "aninha")


def test_book_identity_finds_the_name_under_the_other_form_of_a_brazilian_number():
    rows = {"99@lid": {"name": "", "pushname": "bia"},
            "551199990000@s.whatsapp.net": {"name": "Bia Lima", "pushname": ""}}
    assert contacts.book_identity(rows, "99@lid", "5511999990000@s.whatsapp.net")[0] == "Bia Lima"
    # the exact number wins over its twin
    both = {**rows, "5511999990000@s.whatsapp.net": {"name": "Bia Exata", "pushname": ""}}
    assert contacts.book_identity(both, "99@lid", "5511999990000@s.whatsapp.net")[0] == "Bia Exata"


def test_book_identity_never_turns_a_nickname_into_a_name():
    rows = {"99@lid": {"name": "", "pushname": "apelido"}}
    assert contacts.book_identity(rows, "99@lid", "") == ("", "apelido")
    assert contacts.book_identity({}, "99@lid", "5511999990000@s.whatsapp.net") == ("", "")


def test_blocklist_entry_uses_the_number_when_the_block_is_on_the_lid():
    assert contacts.blocklist_entry("123456789012345@lid", "5511999990000@s.whatsapp.net") == {
        "phone": "5511999990000", "id": {"_serialized": "123456789012345@lid"}}
    assert contacts.blocklist_entry("5511999990000:12@s.whatsapp.net") == {
        "phone": "5511999990000", "id": {"_serialized": "5511999990000@c.us"}}
    # without the map, or with a "number" that is another LID, the LID's own digits stay
    assert contacts.blocklist_entry("123456789012345@lid")["phone"] == "123456789012345"
    assert contacts.blocklist_entry("123456789012345@lid", "999@lid")["phone"] == "123456789012345"


def test_pn_lid_response_carries_the_fields_of_both_client_readers():
    r = contacts.pn_lid_response("5511999990000@s.whatsapp.net", "123@lid",
                                 {"name": "Ana", "pushname": ""})
    number = {"_serialized": "5511999990000@c.us"}
    assert r == {"lid": {"_serialized": "123@lid"}, "phone": number, "id": number,
                 "phoneNumber": number, "contact": {"name": "Ana", "pushname": None}}


def test_contact_body_marks_a_business_account_only_with_a_verified_name():
    business = contacts.contact_body("5511@s.whatsapp.net", "5511@s.whatsapp.net",
                                     {"pushname": "Shop"}, "Official Shop")
    assert business["isBusiness"] is True and business["verifiedName"] == "Official Shop"
    assert business["phoneNumber"] == {"_serialized": "5511@c.us"}
    ordinary = contacts.contact_body("9@lid", "", {"name": "Ana"})
    assert ordinary["isBusiness"] is False and "verifiedName" not in ordinary
    assert ordinary["id"] == {"_serialized": "9@lid"} and "phoneNumber" not in ordinary
    assert ordinary["isMyContact"] is True and ordinary["name"] == "Ana"


def test_address_book_record_only_syncs_whoever_has_a_name_and_finds_the_own_account():
    mine = {"5511@c.us", "9@lid"}
    with_name = contacts.address_book_record(
        {"name": "Ana", "pushname": ""}, {"_serialized": "5522@c.us"}, mine)
    assert with_name["syncToAddressbook"] is True and with_name["isMyContact"] is True
    assert with_name["isMe"] is False and with_name["pushname"] is None
    without_name = contacts.address_book_record(
        {"name": "", "pushname": "ap"}, {"_serialized": "9@lid"}, mine)
    assert without_name["syncToAddressbook"] is False and without_name["isMe"] is True


def test_address_book_records_give_an_lid_the_name_saved_under_its_number():
    rows = [{"jid": "5511999990000@s.whatsapp.net", "name": "Paulo", "pushname": "P"},
            {"jid": "77@lid", "name": "", "pushname": "P"},
            {"jid": "88@lid", "name": "", "pushname": "Stranger"}]
    phones = {"77@lid": "5511999990000@s.whatsapp.net"}
    wpp_id = lambda jid: {"_serialized": jid}
    records = contacts.address_book_records(rows, wpp_id, set(), lambda lid: phones.get(lid, ""),
                                            lambda phone: "77@lid")
    by_id = {r["id"]["_serialized"]: r for r in records}
    assert len(records) == 3
    assert by_id["77@lid"]["name"] == "Paulo" and by_id["77@lid"]["isMyContact"] is True
    assert by_id["77@lid"]["pushname"] == "P"
    assert by_id["88@lid"]["isMyContact"] is False and by_id["88@lid"]["name"] is None
    assert by_id["5511999990000@s.whatsapp.net"]["name"] == "Paulo"


def test_address_book_records_ask_the_number_only_for_an_unnamed_lid():
    asked = []
    rows = [{"jid": "1@lid", "name": "Ana", "pushname": ""},
            {"jid": "5511@s.whatsapp.net", "name": "", "pushname": "x"}]
    contacts.address_book_records(rows, lambda j: {"_serialized": j}, set(),
                                  lambda lid: asked.append(lid) or "",
                                  lambda phone: asked.append(phone) or "")
    assert asked == []


def test_address_book_records_add_the_lid_of_a_saved_number_that_has_no_row():
    rows = [{"jid": "5511999990000@s.whatsapp.net", "name": "Vinicius", "pushname": ""},
            {"jid": "5522@s.whatsapp.net", "name": "Bia", "pushname": ""}]
    lids = {"5511999990000@s.whatsapp.net": "94@lid"}
    records = contacts.address_book_records(rows, lambda j: {"_serialized": j}, set(),
                                            lambda lid: "", lambda phone: lids.get(phone, ""))
    added = [r for r in records if r["id"]["_serialized"] == "94@lid"]
    assert len(records) == 3 and len(added) == 1
    assert added[0]["name"] == "Vinicius" and added[0]["isMyContact"] is True


def test_subscription_target_accepts_a_person_and_a_group_and_nothing_else():
    assert contacts.subscription_target("5511999990000@c.us") == "5511999990000@s.whatsapp.net"
    assert contacts.subscription_target("123456789012@lid") == "123456789012@lid"
    assert contacts.subscription_target("1203-44@g.us") == "1203-44@g.us"
    for bad in ("", None, "status@broadcast", "abc", "5511999990000@newsletter"):
        assert contacts.subscription_target(bad) == ""


def _entry(user, full="", first="", push="", server="s.whatsapp.net"):
    return N(JID=N(User=user, Server=server),
             Info=N(FullName=full, FirstName=first, PushName=push))


def _jid_text(jid):
    return f"{jid.User}@{jid.Server}" if jid.User else ""


def test_address_book_rows_prefer_the_full_name_and_skip_empty_entries():
    rows = contacts.address_book_rows([
        _entry("5511911110001", full="Ana Souza", first="Ana", push="Aninha"),
        _entry("5511911110002", first=" Beto ", push=""),
        _entry("5511911110003", push="Caio"),
        _entry("5511911110004"),                      # nothing to say
        _entry("", full="Sem JID"),
    ], _jid_text)
    assert rows == [("5511911110001@s.whatsapp.net", "Ana Souza", "Aninha"),
                    ("5511911110002@s.whatsapp.net", "Beto", ""),
                    ("5511911110003@s.whatsapp.net", "", "Caio")]
    assert contacts.address_book_rows(None, _jid_text) == []


class _Lookup:
    def __init__(self, responses=None, fail=False):
        self.responses, self.fail, self.asked = responses or [], fail, None

    def is_on_whatsapp(self, *numbers):
        self.asked = numbers
        if self.fail:
            raise RuntimeError("offline")
        return self.responses


def test_known_phone_jid_gives_the_registered_form_of_a_brazilian_number():
    client = _Lookup([query("555193050236")])
    assert contacts.known_phone_jid(client, "5551993050236") == (
        "555193050236", "s.whatsapp.net")
    assert client.asked == ("+5551993050236", "+555193050236")


def test_known_phone_jid_is_none_when_unknown_short_or_failing():
    assert contacts.known_phone_jid(_Lookup([query("", exists=False)]), "5551993050236") is None
    assert contacts.known_phone_jid(_Lookup(), "123") is None
    assert contacts.known_phone_jid(_Lookup(fail=True), "5551993050236") is None
