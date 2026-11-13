"""Remaining routes: the platform of a message, the Meta AI terms and the 501s."""
import asyncio
from types import SimpleNamespace as N

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import remaining


@pytest.mark.parametrize("serial,expected", [
    ("false_5511999@c.us_3EB0ABCDEF0123456789", "web"),
    ("true_5511999@c.us_3A1B2C3D4E5F60718293", "iphone"),
    ("false_5511999@c.us_" + "A" * 22, "android"),
    ("false_5511999@c.us_ABCDEF0123456789ABCD", "unknown"),
    # in a group the id is the third piece; the participant comes after
    ("false_120363-1@g.us_3EB0ABCDEF_5511888@c.us", "web"),
    ("3A1B2C3D4E5F", "iphone"),
    ("", "unknown"),
    (None, "unknown"),
])
def test_message_platform_follows_the_wa_js_rules(serial, expected):
    assert remaining.message_platform(serial) == expected


def test_platform_at_the_21_character_limit():
    assert remaining.message_platform("x_y_" + "B" * 21) == "unknown"
    assert remaining.message_platform("x_y_" + "B" * 22) == "android"


def run(s, requests):
    """Brings up an aiohttp server with only these routes and makes the requests:
    (method, path) or (method, path, JSON body)."""
    async def main():
        app = web.Application()
        remaining.register_routes(app.router, session=lambda req: s,
                                  ok=lambda **kw: web.json_response(kw),
                                  jid_from_text=lambda text: text, jid_text=str)
        async with TestClient(TestServer(app)) as http:
            outputs = []
            for method, path, *body in requests:
                resp = await http.request(method, "/api/tok" + path,
                                          json=body[0] if body else None)
                outputs.append((resp.status, await resp.json()))
            return outputs
    return asyncio.run(main())


def session(go=None, status="CONNECTED"):
    client = N(uuid=b"u1", **{"_NewClient__client": go}) if go is not None \
        else N(uuid=b"u1")
    return N(client=client, status=status)


def test_impossible_routes_answer_501_with_code_and_reason():
    s = session()
    requests = [(m, "/" + r.replace("{communityId}", "1@g.us").replace("{subgroupId}", "2@g.us")
                 .replace("{id}", "123")) for m, r, _, _ in remaining.IMPOSSIBLE]
    outputs = run(s, requests)
    assert len(outputs) == len(remaining.IMPOSSIBLE)
    for (status, body), (_, _, code, _) in zip(outputs, remaining.IMPOSSIBLE):
        assert status == 501 and body["status"] == "error"
        assert body["code"] == code and body["message"]


# The routes that were impossible before WinZapp's neonize build, and the code
# each answered: an older build still answers them so.
FORMERLY_IMPOSSIBLE = [
    ("POST", "/community-subgroups/1@g.us/2@g.us/join", None, "unavailable"),
    ("POST", "/community-subgroups/1@g.us/2@g.us/cancel-request", None, "unavailable"),
    ("POST", "/deactivate-community", {"id": "1@g.us"}, "unavailable"),
    ("POST", "/privacy/status", {"setting": "contact", "list": []},
     "status_privacy_unsupported"),
    ("POST", "/remove-contact", {"phone": "5511999998888"}, "contact_operation_unsupported"),
    ("PUT", "/newsletter/123", {"name": "New name"}, "newsletter_unsupported"),
    ("DELETE", "/newsletter/123", None, "newsletter_unsupported"),
    ("GET", "/custom-lists", None, "lists_not_available"),
    ("GET", "/keep-chats-archived", None, "keep_archived_unsupported"),
    ("POST", "/keep-chats-archived", {"keepArchived": True}, "keep_archived_unsupported"),
]


def test_an_older_neonize_build_still_answers_501_with_the_old_codes(monkeypatch):
    import app_state_mirror
    monkeypatch.setattr(app_state_mirror, "supported", lambda: False)
    outputs = run(session(), [(m, p, b) for m, p, b, _ in FORMERLY_IMPOSSIBLE])
    for (status, body), (_, path, _, code) in zip(outputs, FORMERLY_IMPOSSIBLE):
        assert (status, body["code"]) == (501, code), path


def test_no_impossible_route_is_the_last_seen_or_presence_one():
    routes = {r for _, r, _, _ in remaining.IMPOSSIBLE}
    assert not any("last-seen" in r or "chat-is-online" in r for r in routes)


def _tos_answer(state=None, notice="20250502"):
    attrs = {"id": notice}
    if state is not None:
        attrs["state"] = state
    return {"tag": "iq", "content": [
        {"tag": "tos", "content": [{"tag": "notice", "attrs": attrs}]}]}


class _Server:
    """WhatsApp's side of the tos queries: what it was sent, what it answers."""

    def __init__(self, state="false", refuse=None):
        self.state = state
        self.refuse = refuse
        self.sent = []

    def send_iq(self, client, query):
        self.sent.append(query)
        if self.refuse:
            raise remaining.rq.QueryError("refused", self.refuse)
        if query["type"] == "set":
            self.state = "true"
            return {"tag": "iq"}
        return _tos_answer(self.state)


@pytest.fixture
def server(monkeypatch):
    fake = _Server()
    monkeypatch.setattr(remaining.rq, "available", lambda client: True)
    monkeypatch.setattr(remaining.rq, "send_iq", fake.send_iq)
    monkeypatch.setattr(remaining, "TERMS_REREAD_DELAY_SECONDS", 0)
    return fake


def test_the_terms_are_read_with_whatsapp_webs_query():
    query = remaining.terms_query()
    assert (query["namespace"], query["type"]) == ("tos", "get")
    request, = query["content"]
    assert request["tag"] == "request" and "attrs" not in request
    assert request["content"] == [{"tag": "notice", "attrs": {"id": "20250502"}}]


def test_the_terms_are_accepted_first_with_the_state_the_read_gives_back():
    first = remaining.terms_accept_queries()[0]
    assert (first["namespace"], first["type"]) == ("tos", "set")
    assert first["content"] == [
        {"tag": "notice", "attrs": {"id": "20250502", "state": "true"}}]


def test_the_stanzas_whatsapp_refused_or_ignored_are_not_tried_again():
    """2026-11-13: session_update got 400; the stage-5 notice (bare or in a request)
    changed nothing or got no answer."""
    for query in remaining.terms_accept_queries():
        notice = query["content"][0]
        if notice["tag"] == "request":
            notice = notice["content"][0]
        assert notice["attrs"].get("state") == "true"
        assert (query["content"][0].get("attrs") or {}).get("type") != "session_update"


@pytest.mark.parametrize("answer,expected", [
    (_tos_answer("false"), "NOT_ACCEPTED"),
    (_tos_answer("true"), "ACCEPTED"),
    # WhatsApp Web: accepted unless the state says "false"
    (_tos_answer(), "ACCEPTED"),
    (_tos_answer("false", notice="1"), "UNKNOWN"),
    ({"tag": "iq"}, "UNKNOWN"),
    (None, "UNKNOWN"),
])
def test_the_state_of_the_notice_in_the_answer(answer, expected):
    assert remaining.terms_state(answer) == expected


def test_meta_ai_terms_says_unknown_without_a_connection(server):
    (status, body), = run(session(status="CLOSED"), [("GET", "/meta-ai-terms")])
    assert status == 200
    assert body["response"] == {"noticeId": "20250502", "state": "UNKNOWN"}
    assert server.sent == []


def test_meta_ai_terms_says_unknown_on_an_older_build(monkeypatch):
    monkeypatch.setattr(remaining.rq, "available", lambda client: False)
    (status, body), = run(session(), [("GET", "/meta-ai-terms")])
    assert (status, body["response"]["state"]) == (200, "UNKNOWN")


def test_meta_ai_terms_reads_the_state_from_whatsapp(server):
    (status, body), = run(session(), [("GET", "/meta-ai-terms")])
    assert status == 200
    assert body["response"] == {"noticeId": "20250502", "state": "NOT_ACCEPTED"}
    assert [q["type"] for q in server.sent] == ["get"]


def test_a_refused_read_says_unknown_and_does_not_block_the_send(server):
    server.refuse = 500
    (status, body), = run(session(), [("GET", "/meta-ai-terms")])
    assert (status, body["response"]["state"]) == (200, "UNKNOWN")


def test_accepting_sends_the_update_and_confirms_with_a_new_read(server):
    (status, body), = run(session(), [("POST", "/meta-ai-terms/accept")])
    assert status == 200
    assert body["response"] == {"noticeId": "20250502", "state": "ACCEPTED"}
    assert [q["type"] for q in server.sent] == ["get", "set", "get"]


def test_an_accept_whatsapp_did_not_record_is_not_a_success(server, monkeypatch):
    monkeypatch.setattr(server, "send_iq", lambda client, query: (
        server.sent.append(query) or _tos_answer("false")))
    monkeypatch.setattr(remaining.rq, "send_iq", server.send_iq)
    (status, body), = run(session(), [("POST", "/meta-ai-terms/accept")])
    assert status == 409 and body["response"]["state"] == "NOT_ACCEPTED"


def test_a_400_moves_on_to_the_next_stanza(server, monkeypatch):
    def send_iq(client, query):
        server.sent.append(query)
        content = query["content"][0]
        if query["type"] == "set" and content["tag"] == "notice":
            raise remaining.rq.QueryError("bad-request", 400)
        if query["type"] == "set":
            server.state = "true"
            return {"tag": "iq"}
        return _tos_answer(server.state)
    monkeypatch.setattr(remaining.rq, "send_iq", send_iq)
    (status, body), = run(session(), [("POST", "/meta-ai-terms/accept")])
    assert status == 200 and body["response"]["state"] == "ACCEPTED"
    assert [q["type"] for q in server.sent] == ["get", "set", "set", "get"]


def test_a_stanza_with_no_answer_in_time_moves_on_too(server, monkeypatch):
    def send_iq(client, query):
        server.sent.append(query)
        if query["type"] == "set" and query["content"][0]["tag"] == "notice":
            raise remaining.rq.QueryError("timeout", 0)
        if query["type"] == "set":
            server.state = "true"
            return {"tag": "iq"}
        return _tos_answer(server.state)
    monkeypatch.setattr(remaining.rq, "send_iq", send_iq)
    (status, body), = run(session(), [("POST", "/meta-ai-terms/accept")])
    assert status == 200 and body["response"]["state"] == "ACCEPTED"


def test_the_notice_attributes_are_logged_for_the_read():
    answer = _tos_answer("false")
    assert remaining.terms_notices(answer) == [{"id": "20250502", "state": "false"}] or \
        remaining.terms_notices(answer)[0]["state"] == "false"
    assert remaining.terms_notices({"tag": "iq", "content": [{"tag": "x"}]}) == [
        "no tos node; holds x"]


def test_every_tos_query_has_a_short_timeout():
    """The client waits 20 s for the accept: three stanzas and a read must fit."""
    queries = remaining.terms_accept_queries() + [remaining.terms_query()]
    assert all(q["timeout_ms"] == remaining.TERMS_TIMEOUT_MS for q in queries)
    assert 5 * remaining.TERMS_TIMEOUT_MS < 20000


def test_another_refusal_than_400_stops_at_the_first_stanza(server):
    server.refuse = 500
    (status, body), = run(session(), [("POST", "/meta-ai-terms/accept")])
    assert status == 502 and body["code"] == "meta_ai_terms_refused"
    assert [q["type"] for q in server.sent].count("set") == 1


def test_a_refused_accept_answers_502(server):
    server.refuse = 400
    (status, body), = run(session(), [("POST", "/meta-ai-terms/accept")])
    assert status == 502 and body["code"] == "meta_ai_terms_refused"
    assert [q["type"] for q in server.sent].count("set") == len(
        remaining.terms_accept_queries())


def test_accepting_needs_a_connection(server):
    (status, body), = run(session(status="CLOSED"), [("POST", "/meta-ai-terms/accept")])
    assert status == 503 and body["code"] == "disconnected"
    assert server.sent == []


def test_accepting_on_an_older_build_answers_501(monkeypatch):
    monkeypatch.setattr(remaining.rq, "available", lambda client: False)
    (status, body), = run(session(), [("POST", "/meta-ai-terms/accept")])
    assert status == 501 and body["code"] == "meta_ai_terms_unsupported"


def test_remaining_does_not_call_native_code():
    """The consent goes through the raw query, not ctypes: no AcceptTOSNotice."""
    code = open(remaining.__file__, encoding="utf-8").read()
    assert "ctypes" not in code


def test_get_platform_from_message_answers_the_word_in_json():
    outputs = run(session(), [
        ("GET", "/get-platform-from-message/false_5511@c.us_3EB0AA"),
        ("GET", "/get-platform-from-message/true_5511@c.us_3AFF00")])
    assert outputs == [(200, "web"), (200, "iphone")]
