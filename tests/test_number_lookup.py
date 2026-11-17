"""core.number_lookup: the canonical phone JID of a typed number."""
from types import SimpleNamespace as N

from core import number_lookup


def body(serialized, exists=True):
    return {"response": {"numberExists": exists, "id": {"_serialized": serialized}}}


def test_canonical_jid_gives_the_registered_form():
    assert number_lookup.canonical_jid(body("555193050236@c.us")) == "555193050236@s.whatsapp.net"


def test_canonical_jid_is_empty_for_unknown_lid_or_garbage():
    assert number_lookup.canonical_jid(body("5551@c.us", exists=False)) == ""
    assert number_lookup.canonical_jid(body("123456@lid")) == ""
    assert number_lookup.canonical_jid(None) == ""
    assert number_lookup.canonical_jid({"response": []}) == ""


def test_resolve_typed_number_falls_back_to_empty_on_failure(monkeypatch):
    mw = N(wpp_server="http://127.0.0.1", wpp_port=6300, token="t")

    def boom(*a, **k):
        raise OSError("down")
    monkeypatch.setattr(number_lookup, "api_get", boom)
    assert number_lookup.resolve_typed_number(mw, "5551993050236") == ""


def test_resolve_typed_number_uses_the_engine_answer(monkeypatch):
    mw = N(wpp_server="http://127.0.0.1", wpp_port=6300, token="t")
    seen = []

    def fake(url, **k):
        seen.append(url)
        return N(status_code=200, json=lambda: body("555193050236@c.us"))
    monkeypatch.setattr(number_lookup, "api_get", fake)
    assert number_lookup.resolve_typed_number(mw, "5551993050236") == "555193050236@s.whatsapp.net"
    assert seen[0].endswith("/api/t/check-number-status/5551993050236")


def test_a_lid_answer_gives_the_number_it_belongs_to():
    """WhatsApp Web answers a typed number with the @lid (2026-11-17); the
    number WinZapp's patch adds is WhatsApp's own form, without the 9th digit
    the user typed, so no second conversation is opened next to the real one."""
    answer = {"response": {"numberExists": True, "id": {"_serialized": "123456789012345@lid"},
                           "phoneNumber": "555193050236@c.us"}}
    assert number_lookup.typed_number_identity(answer) == (
        "555193050236@s.whatsapp.net", "123456789012345@lid")
    assert number_lookup.canonical_jid(answer) == "555193050236@s.whatsapp.net"


def test_resolve_typed_number_bridges_the_answered_lid(monkeypatch):
    registered = []
    mw = N(wpp_server="http://127.0.0.1", wpp_port=6300, token="t",
           register_jid_mapping=lambda lid, phone: registered.append((lid, phone)))
    answer = {"response": {"numberExists": True, "id": "777@lid",
                           "phoneNumber": {"_serialized": "555193050236@c.us"}}}
    monkeypatch.setattr(number_lookup, "api_get",
                        lambda url, **k: N(status_code=200, json=lambda: answer))
    assert number_lookup.resolve_typed_number(mw, "5551993050236") == "555193050236@s.whatsapp.net"
    assert registered == [("777@lid", "555193050236@s.whatsapp.net")]
