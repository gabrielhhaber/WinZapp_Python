"""The refusals in the format the WinZapp client recognizes (responses.py)."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace as N

import responses

ROOT = Path(__file__).resolve().parent.parent


def _body(response):
    return json.loads(response.text)


def test_disconnected_is_the_404_of_the_real_servers_middleware():
    response = responses.disconnected()
    body = _body(response)
    # client/api_patches/src/middleware/statusConnection.ts: disconnected()
    assert response.status == 404
    assert body["status"] == "Disconnected" and body["response"] is None
    assert body["message"]


def test_disconnected_goes_through_the_clients_reader():
    """sending.py `_check_wa_connection_closed` and message_edit.response_not_sent
    read the 404 status and the `status` field (case-insensitively)."""
    response = responses.disconnected()
    body = _body(response)
    assert response.status == 404 and str(body.get("status", "")).lower() == "disconnected"
    assert body.get("reason") != "probe_timeout"     # it is not the "weak" 404


def test_without_connection_only_answers_when_there_is_no_connection():
    connected = N(client=object(), status="CONNECTED")
    assert responses.without_connection(connected) is None
    assert responses.without_connection(N(client=None, status="CONNECTED")).status == 404
    assert responses.without_connection(N(client=object(), status="CLOSED")).status == 404


def _client_reader():
    """The client's core/reply_failure.py, without importing the rest of it."""
    path = ROOT / "client" / "core" / "reply_failure.py"
    spec = importlib.util.spec_from_file_location("client_reply_failure", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_missing_quote_is_recognized_by_the_client_as_a_refusal_before_sending():
    response = responses.missing_quote()
    body = _body(response)
    fake = N(status_code=response.status, json=lambda: body)
    assert _client_reader().quote_missing_before_send(fake) is True
    assert response.status == 500        # never 4xx: old clients would drop the quote and resend


def test_edit_refused_carries_the_text_the_client_looks_for():
    response = responses.edit_refused("outside_window")
    assert "Cannot edit this message" in response.text      # message_actions.py
    assert 400 <= response.status < 500


def test_the_clients_description_of_a_quote_keeps_only_what_a_quote_needs():
    import quote_fallback
    described = quote_fallback.described({
        "type": "chat", "body": "x" * 5000, "fromMe": False, "author": "153@lid",
        "raw": "ignored", "duration": True})
    assert described == {"type": "chat", "body": "x" * quote_fallback.MAX_TEXT,
                         "fromMe": False, "author": "153@lid"}
    assert quote_fallback.described({"type": "ptt", "duration": 9, "fromMe": True}) == {
        "type": "ptt", "duration": 9, "fromMe": True}
    for unusable in (None, "chat", {"type": "reaction", "author": "1@lid"},
                     {"type": "chat", "body": "who wrote it?"},
                     {"type": "chat", "author": "no-server"}):
        assert quote_fallback.described(unusable) is None


def test_a_described_quote_becomes_the_same_bubble_a_stored_one_does():
    import msgmap
    import quote_fallback
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message
    described = quote_fallback.described({"type": "chat", "body": "oi", "author": "153@lid"})
    stored = {"type": "chat", "body": "oi", "author": "153@lid", "fromMe": False,
              "id": {"id": "3EB0AAA"}, "t": 1}
    assert msgmap.quoted(described) == msgmap.quoted(stored) == Message(conversation="oi")
    assert msgmap.quoted_participant(described, "me") == msgmap.quoted_participant(stored, "me")
