"""channels_panel.py: the channels window's reads and follow/unfollow, against a
fake neonize client that records the MEX queries it is asked."""
import asyncio
from types import SimpleNamespace as N

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import channels_panel as cp
import raw_queries as rq

NODE = {"id": "120363000000000001@newsletter", "thread_metadata": {"name": {"text": "Notícias"}}}


@pytest.mark.parametrize("body, key", [
    ({"id": "123@newsletter"}, "123@newsletter"),
    ({"id": "123"}, "123@newsletter"),
    ({"invite": "https://whatsapp.com/channel/0029VaAbCdEfGhIjKlMnOp"}, "0029VaAbCdEfGhIjKlMnOp"),
    ({"invite": "0029VaAbCdEfGhIjKlMnOp/"}, "0029VaAbCdEfGhIjKlMnOp"),
    ({"id": "5511@c.us"}, ""),
    ({"invite": "short"}, ""),
    (None, ""),
])
def test_a_channel_key_is_an_id_or_an_invite_code(body, key):
    assert cp.channel_key(body) == key


def test_the_directory_searches_text_and_otherwise_recommends():
    query_id, variables, field = cp.directory_request({"query": " futebol ", "limit": 500})
    assert (query_id, field) == (cp.SEARCH_QUERY, cp.SEARCH_FIELD)
    assert variables["input"] == {"search_text": "futebol", "categories": [], "limit": 50,
                                  "start_cursor": None}
    query_id, variables, field = cp.directory_request({"limit": "x"})
    assert (query_id, field) == (cp.RECOMMENDED_QUERY, cp.RECOMMENDED_FIELD)
    assert variables["input"] == {"limit": 20, "country_codes": []}


def test_a_category_narrows_the_search_and_the_list():
    _, variables, _ = cp.directory_request({"query": "gol", "category": "sports"})
    assert variables["input"]["categories"] == ["SPORTS"]
    query_id, variables, field = cp.directory_request({"category": "NEWS", "view": "trending"})
    assert (query_id, field) == (cp.LIST_QUERY, cp.LIST_FIELD)
    assert variables["input"] == {"view": "TRENDING", "limit": 20, "start_cursor": None,
                                  "filters": {"country_codes": [], "categories": ["NEWS"]}}
    query_id, _, _ = cp.directory_request({"view": "NEW"})
    assert query_id == cp.LIST_QUERY
    _, variables, _ = cp.directory_request({"category": "SPECIAL_EVENTS", "view": "bogus"})
    assert variables["input"] == {"limit": 20, "country_codes": []}       # neither is sent


def test_the_lists_are_of_the_accounts_own_country():
    """A view or a category with no country is refused "Bad Request"
    (measured over CDP: 20 channels with ["BR"], an error with [])."""
    _, variables, _ = cp.directory_request({"view": "TRENDING"}, "BR")
    assert variables["input"]["filters"] == {"country_codes": ["BR"], "categories": []}
    _, variables, _ = cp.directory_request({}, "BR")
    assert variables["input"] == {"limit": 20, "country_codes": ["BR"]}
    _, variables, _ = cp.directory_request({"query": "gol"}, "BR")
    assert "country_codes" not in variables["input"]


@pytest.mark.parametrize("me, country", [
    ("5535999990000:12@s.whatsapp.net", "BR"),
    ("5535999990000@s.whatsapp.net", "BR"),
    ("14155550123@s.whatsapp.net", "US"),
    ("351912345678", "PT"),
    ("", ""),
    (None, ""),
    ("abc@lid", ""),
    ("999@s.whatsapp.net", ""),
])
def test_the_country_comes_from_the_accounts_own_number(me, country):
    assert cp.account_country(me) == country


def test_one_channel_is_read_by_jid_or_by_invite():
    assert cp.channel_request("1@newsletter")["fetch_viewer_metadata"] is True
    assert cp.channel_request("1@newsletter", viewer=False)["fetch_viewer_metadata"] is False
    assert cp.channel_request("1@newsletter")["input"] == {
        "key": "1@newsletter", "type": "JID", "view_role": "GUEST"}
    assert cp.channel_request("0029VaAbCdEfGhIjKlMnOp")["input"]["type"] == "INVITE"


def test_the_one_channel_read_brings_its_pinned_updates():
    assert cp.channel_request("1@newsletter")["fetch_pinned_messages"] is True


@pytest.mark.parametrize("pin, query_id, field", [
    (True, cp.PIN_QUERY, cp.PIN_FIELD), (False, cp.UNPIN_QUERY, cp.UNPIN_FIELD)])
def test_an_update_is_pinned_by_its_server_id(pin, query_id, field):
    assert cp.pin_request({"id": "1@newsletter", "serverId": 4977, "pin": pin}) == (
        query_id, {"newsletter_id": "1@newsletter", "input": {"message_ids": ["4977"]}}, field,
        {"id": "1@newsletter", "serverId": 4977, "pinned": pin})


@pytest.mark.parametrize("body", [
    {"id": "1@newsletter", "serverId": "4977", "pin": True},
    {"id": "1@newsletter", "serverId": True, "pin": True},
    {"id": "1@newsletter", "serverId": 0, "pin": True},
    {"id": "1@newsletter", "serverId": 4977, "pin": "yes"},
    {"id": "1@g.us", "serverId": 4977, "pin": True},
    None,
])
def test_a_pin_that_names_no_update_is_refused(body):
    assert cp.pin_request(body) is None


@pytest.mark.parametrize("answer, count", [({"admin_count": 3}, 3), ({"admin_count": "2"}, 2),
                                           ({"admin_count": -1}, None), ({}, None), (None, None)])
def test_the_admin_count_is_read_or_unknown(answer, count):
    assert cp.admin_count(answer) == count


def test_an_admin_invite_names_a_person_and_keeps_its_texts_short():
    assert cp.admin_invite_request({"id": "1@newsletter", "user": "5511999998888@c.us",
                                    "name": " Canal ", "message": "x" * 900}) == (
        "1@newsletter", "5511999998888@s.whatsapp.net", "Canal", "x" * cp.CAPTION_LIMIT)
    assert cp.admin_invite_request({"id": "1@newsletter", "user": "123456@lid"})[1] == "123456@lid"
    assert cp.admin_invite_request({"id": "1@newsletter", "user": "1203@g.us"}) is None
    assert cp.admin_invite_request({"id": "1@g.us", "user": "123456@lid"}) is None
    assert cp.invite_expiration({"invite_expiration_time": "1790000000"}) == 1790000000
    assert cp.invite_expiration({}) == 0
    assert cp.invite_fields("1@newsletter", "Canal", "oi", 5) == {
        "newsletterJID": "1@newsletter", "newsletterName": "Canal", "caption": "oi",
        "inviteExpiration": 5}


def test_followers_come_with_their_role_owner_and_admins_first():
    answer = {"followers": {"edges": [
        {"node": {"id": "11111@lid", "pn": "5511999998888@s.whatsapp.net", "display_name": " Ana "},
         "role": "SUBSCRIBER"},
        {"node": {"id": "22222@lid", "display_name": "Bia"}, "role": "ADMIN"},
        {"node": {"id": "33333@lid"}, "role": "OWNER"},
        {"node": {"id": "1203@g.us"}, "role": "ADMIN"}, {"role": "ADMIN"}, "x",
        {"node": {"id": "44444@lid"}, "role": "WEIRD"}]}}
    assert cp.followers_of(answer) == [
        {"id": "33333@lid", "phone": "", "name": "", "role": "owner"},
        {"id": "22222@lid", "phone": "", "name": "Bia", "role": "admin"},
        {"id": "11111@lid", "phone": "5511999998888@s.whatsapp.net", "name": "Ana",
         "role": "subscriber"},
        {"id": "44444@lid", "phone": "", "name": "", "role": "subscriber"}]
    assert cp.followers_of(None) == [] and cp.followers_of({"followers": None}) == []


def test_pending_invites_are_people():
    answer = {"pending_admin_invites": [{"user": {"id": "55555@lid"}}, {"user": {"id": "55555@lid"}},
                                        {"user": None}, None, {"user": {"id": "x"}}]}
    assert cp.pending_of(answer) == ["55555@lid"]
    assert cp.pending_of({}) == []


def test_nodes_come_from_a_list_or_a_result():
    assert cp.nodes_of([NODE, "x"]) == [NODE]
    assert cp.nodes_of({"result": [NODE]}) == [NODE]
    assert cp.nodes_of(None) == []


class Client:
    def __init__(self):
        self.asked, self.followed = [], []

    def send_iq(self):
        pass

    def send_mex(self, query_id, variables):
        self.asked.append(query_id)
        data = {cp.SUBSCRIBED_QUERY: {cp.SUBSCRIBED_FIELD: [NODE]},
                cp.SEARCH_QUERY: {cp.SEARCH_FIELD: {"result": [NODE]}},
                cp.CHANNEL_QUERY: {cp.CHANNEL_FIELD: NODE}}.get(query_id)
        return {"data": data} if data else {"error": "refused"}

    def follow_newsletter(self, jid):
        self.followed.append(jid)

    def newsletter_toggle_mute(self, jid, mute):
        self.followed.append((jid, mute))


def post(s, *requests, monkeypatch=None):
    async def main():
        app = web.Application()
        cp.register_routes(app.router, session=lambda req: s,
                           ok=lambda **kw: web.json_response(kw), jid_from_text=lambda t: t)
        out = []
        async with TestClient(TestServer(app)) as http:
            for route, body in requests:
                resp = await http.post("/api/tok/channels/" + route, json=body)
                out.append((resp.status, await resp.json()))
        return out
    return asyncio.run(main())


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setattr(rq, "available", lambda client: client is not None)
    return N(client=Client(), status="CONNECTED")


def test_reads_answer_whatsapps_own_nodes(session):
    (s1, listed), (s2, found), (s3, one), (s4, refused) = post(
        session, ("list", {}), ("directory", {"query": "futebol"}),
        ("info", {"invite": "0029VaAbCdEfGhIjKlMnOp"}), ("directory", {}))
    assert (s1, listed["response"]) == (200, [NODE])
    assert (s2, found["response"]) == (200, [NODE])
    assert (s3, one["response"]) == (200, NODE)
    assert s4 == 503 and refused == {"status": "error", "code": "channels_unavailable"}


def test_a_channel_not_followed_is_read_again_without_the_viewer(session):
    # WhatsApp refuses the viewer's metadata of a channel this account does
    # not follow ("Not Allowed", WhatsApp Web 2026-11-11)
    asked = []

    def send_mex(query_id, variables):
        asked.append(variables["fetch_viewer_metadata"])
        if variables["fetch_viewer_metadata"]:
            return {"error": "Not Allowed"}
        return {"data": {cp.CHANNEL_FIELD: NODE}}
    session.client.send_mex = send_mex
    (status, body), = post(session, ("info", {"id": "120363159356548761@newsletter"}))
    assert (status, body["response"]) == (200, NODE)
    assert asked == [True, False]


def test_a_channel_refused_both_ways_is_not_found(session):
    session.client.send_mex = lambda query_id, variables: {"error": "Bad Request"}
    (status, body), = post(session, ("info", {"id": "120363159356548761@newsletter"}))
    assert (status, body["code"]) == (503, "channel_not_found")


def test_follow_takes_only_a_channel(session):
    (ok_status, body), (bad, _) = post(session, ("follow", {"id": "123@newsletter"}),
                                       ("follow", {"id": "5511@c.us"}))
    assert ok_status == 200 and body["response"] == {"id": "123@newsletter"}
    assert bad == 400
    assert session.client.followed == ["123@newsletter"]


def test_mute_goes_both_ways_and_needs_a_boolean(session):
    (s1, body), (s2, _) = post(session, ("mute", {"id": "123@newsletter", "mute": False}),
                               ("mute", {"id": "123@newsletter", "mute": "no"}))
    assert s1 == 200 and body["response"] == {"id": "123@newsletter", "muted": False}
    assert s2 == 400
    assert session.client.followed == [("123@newsletter", False)]


def test_a_missing_action_is_a_static_error(session):
    (status, body), = post(session, ("unfollow", {"id": "123@newsletter"}))
    assert status == 503 and body["code"] == "channel_unfollow_failed"


def test_pin_answers_the_update_and_a_refusal_is_static(session):
    session.client.send_mex = lambda query_id, variables: (
        session.client.asked.append((query_id, variables))
        or ({"data": {cp.PIN_FIELD: {"id": "1"}}} if query_id == cp.PIN_QUERY else {"error": "x"}))
    (s1, done), (s2, refused), (s3, _) = post(
        session, ("pin", {"id": "123@newsletter", "serverId": 7, "pin": True}),
        ("pin", {"id": "123@newsletter", "serverId": 7, "pin": False}),
        ("pin", {"id": "123@newsletter", "serverId": "7", "pin": True}))
    assert (s1, done["response"]) == (200, {"id": "123@newsletter", "serverId": 7, "pinned": True})
    assert (s2, refused["code"]) == (503, "channel_pin_failed")
    assert s3 == 400
    assert [q for q, _ in session.client.asked] == [cp.PIN_QUERY, cp.UNPIN_QUERY]


def test_an_admin_is_invited_by_lid_and_the_invite_reaches_their_chat(session):
    sent = []
    session.client.get_lid_from_pn = lambda jid: N(User="777")
    session.client.send_message = lambda to, message: sent.append((to, message))
    session.client.send_mex = lambda query_id, variables: (
        session.client.asked.append((query_id, variables))
        or {"data": {cp.INVITE_FIELD: {"invite_expiration_time": "1790000000"}}})
    (status, body), = post(session, ("admin-invite", {
        "id": "123@newsletter", "user": "5511999998888@c.us", "name": "Canal", "message": "oi"}))
    assert status == 200 and body["response"] == {
        "id": "123@newsletter", "user": "5511999998888@s.whatsapp.net", "expiration": 1790000000}
    assert session.client.asked == [(cp.INVITE_QUERY, {"newsletter_id": "123@newsletter",
                                                       "user_id": "777@lid"})]
    (to, message), = sent
    assert to == "777@lid"
    invite = message.newsletterAdminInviteMessage
    assert (invite.newsletterJID, invite.caption, invite.inviteExpiration) == (
        "123@newsletter", "oi", 1790000000)


def test_without_a_lid_no_invite_is_created(session):
    session.client.get_lid_from_pn = lambda jid: N(User="")
    (status, body), = post(session, ("admin-invite", {"id": "123@newsletter",
                                                      "user": "5511999998888@c.us"}))
    assert (status, body["code"]) == (503, "channel_admin_invite_failed")
    assert session.client.asked == []


def test_a_revoke_is_refused_only_by_an_error(session):
    answers = [{"data": {}}, {"error": "not owner"}]
    session.client.send_mex = lambda query_id, variables: answers.pop(0)
    (s1, done), (s2, refused), (s3, _) = post(
        session, ("admin-revoke", {"id": "123@newsletter", "user": "77712345@lid"}),
        ("admin-revoke", {"id": "123@newsletter", "user": "77712345@lid"}),
        ("admin-revoke", {"id": "123@newsletter", "user": "x"}))
    assert (s1, done["response"]) == (200, {"id": "123@newsletter", "user": "77712345@lid"})
    assert (s2, refused["code"]) == (503, "channel_admin_revoke_failed")
    assert s3 == 400


def test_the_admin_count_route(session):
    session.client.send_mex = lambda query_id, variables: {"data": {cp.ADMIN_INFO_FIELD: {"admin_count": 2}}}
    (status, body), = post(session, ("admins", {"id": "123@newsletter"}))
    assert (status, body["response"]) == (200, {"id": "123@newsletter", "adminCount": 2})


def test_demote_and_owner_change_name_the_person_by_lid(session):
    asked = []
    session.client.get_lid_from_pn = lambda jid: N(User="77712345")
    session.client.send_mex = lambda query_id, variables: asked.append((query_id, variables)) or {"data": {}}
    (s1, a), (s2, b) = post(session, ("admin-demote", {"id": "123@newsletter", "user": "5511999998888@c.us"}),
                            ("owner-change", {"id": "123@newsletter", "user": "22222@lid"}))
    assert (s1, s2) == (200, 200)
    assert asked == [(cp.DEMOTE_QUERY, {"newsletter_id": "123@newsletter", "user_id": "77712345@lid"}),
                     (cp.OWNER_QUERY, {"newsletter_id": "123@newsletter", "user_id": "22222@lid"})]


def test_followers_and_pending_routes(session):
    session.client.send_mex = lambda query_id, variables: {"data": {
        cp.FOLLOWERS_FIELD: {"followers": {"edges": [{"node": {"id": "22222@lid"}, "role": "ADMIN"}]}},
        cp.ADMIN_INFO_FIELD: {"pending_admin_invites": [{"user": {"id": "55555@lid"}}]}}}
    (s1, f), (s2, p) = post(session, ("followers", {"id": "123@newsletter"}),
                            ("admin-pending", {"id": "123@newsletter"}))
    assert f["response"]["followers"] == [{"id": "22222@lid", "phone": "", "name": "", "role": "admin"}]
    assert p["response"] == {"id": "123@newsletter", "pending": ["55555@lid"]}
