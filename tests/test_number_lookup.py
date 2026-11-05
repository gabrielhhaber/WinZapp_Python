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
