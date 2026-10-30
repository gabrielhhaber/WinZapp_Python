from core.vcard_payload import contact_message_payload

CARD = "BEGIN:VCARD\nVERSION:3.0\nFN:Ana\nTEL;waid=5511999990000:+55 11 99999-0000\nEND:VCARD"


def test_wppconnect_vcard_keeps_the_card_and_names_it_from_the_card():
    out = contact_message_payload({"type": "vcard", "body": CARD, "vcardFormattedName": "Ana"})
    assert out == {"displayName": "Ana", "vcard": CARD}


def test_wppconnect_card_without_name_still_carries_the_number():
    out = contact_message_payload({"type": "vcard", "body": CARD})
    assert out == {"displayName": "", "vcard": CARD}


def test_card_stuffed_in_the_name_field_is_kept_as_the_card():
    out = contact_message_payload({"type": "vcard", "displayName": CARD})
    assert out == {"displayName": "", "vcard": CARD}


def test_malformed_input_does_not_raise():
    out = contact_message_payload({"body": None, "vcardList": [None, 3, CARD]})
    assert out == {"displayName": "", "vcard": CARD}
    assert contact_message_payload({"vcardList": []}) == {"displayName": ""}


def test_neonize_card_keeps_name_and_card():
    out = contact_message_payload({"type": "vcard", "body": CARD, "displayName": "Ana"})
    assert out == {"displayName": "Ana", "vcard": CARD}


def test_multi_vcard_joins_every_card():
    other = CARD.replace("Ana", "Bia")
    out = contact_message_payload({"type": "multi_vcard", "vcardList": [
        {"displayName": "Ana", "vcard": CARD}, {"displayName": "Bia", "vcard": other}]})
    assert out["vcard"] == CARD + "\n" + other and out["displayName"] == "Ana"


def test_plain_body_is_only_a_name():
    assert contact_message_payload({"body": "Ana"}) == {"displayName": "Ana"}
