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


def test_one_channel_is_read_by_jid_or_by_invite():
    assert cp.channel_request("1@newsletter")["input"] == {
        "key": "1@newsletter", "type": "JID", "view_role": "GUEST"}
    assert cp.channel_request("0029VaAbCdEfGhIjKlMnOp")["input"]["type"] == "INVITE"


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
