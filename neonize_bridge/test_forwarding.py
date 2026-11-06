"""Forwarding stored messages without media."""
from forwarding import card_name, plain_forward

CARD = "BEGIN:VCARD\nVERSION:3.0\nFN:Ana Souza\nTEL;waid=5511999998888:+55 11 99999-8888\nEND:VCARD"


def test_text():
    msg = plain_forward({"type": "chat", "body": "oi"})
    assert msg.extendedTextMessage.text == "oi"
    assert msg.extendedTextMessage.contextInfo.isForwarded
    assert plain_forward({"type": "chat", "body": ""}) is None


def test_location_keeps_point_name_and_address():
    loc = plain_forward({"type": "location", "lat": -23.5, "lng": -46.6,
                         "body": "Padaria", "loc": "Padaria"}).locationMessage
    assert (loc.degreesLatitude, loc.degreesLongitude, loc.name) == (-23.5, -46.6, "Padaria")
    assert not loc.HasField("address") and loc.contextInfo.isForwarded
    loc = plain_forward({"type": "location", "lat": 1.0, "lng": 2,
                         "body": "", "loc": "Rua A, 1"}).locationMessage
    assert (loc.name, loc.address) == ("", "Rua A, 1")
    live = plain_forward({"type": "liveLocation", "lat": 1.0, "lng": 2.0,
                          "loc": "a caminho"}).locationMessage
    assert (live.name, live.address) == ("", "")
    assert plain_forward({"type": "location", "lat": None, "lng": 2.0}) is None
    assert plain_forward({"type": "location", "lat": True, "lng": 2.0}) is None


def test_contact_cards():
    one = plain_forward({"type": "vcard", "body": CARD, "displayName": ""}).contactMessage
    assert (one.displayName, one.vcard) == ("Ana Souza", CARD) and one.contextInfo.isForwarded
    other = CARD.replace("Ana Souza", "Bia")
    many = plain_forward({"type": "vcard", "body": CARD + "\n" + other,
                          "displayName": "Ana e mais 1"}).contactsArrayMessage
    assert many.displayName == "Ana e mais 1"
    assert [c.displayName for c in many.contacts] == ["Ana Souza", "Bia"]
    assert plain_forward({"type": "vcard", "body": "nada"}) is None
    assert card_name("BEGIN:VCARD\nFN;CHARSET=UTF-8:José\nEND:VCARD") == "José"


def test_other_types_are_not_forwarded():
    assert plain_forward({"type": "poll_creation", "body": "x"}) is None


def test_text_keeps_its_link_preview_only_when_asked():
    stored = {"type": "chat", "body": "https://example.com/a", "title": "Example",
              "description": "A page", "canonicalUrl": "https://example.com/a"}
    kept = plain_forward(stored, keep_preview=True).extendedTextMessage
    assert (kept.title, kept.description, kept.matchedText) == (
        "Example", "A page", "https://example.com/a")
    assert kept.text == "https://example.com/a" and kept.contextInfo.isForwarded
    plain = plain_forward(stored).extendedTextMessage
    assert (plain.title, plain.description, plain.matchedText) == ("", "", "")


def test_text_without_a_preview_has_nothing_to_keep():
    msg = plain_forward({"type": "chat", "body": "oi"}, keep_preview=True).extendedTextMessage
    assert (msg.title, msg.description, msg.matchedText) == ("", "", "")
