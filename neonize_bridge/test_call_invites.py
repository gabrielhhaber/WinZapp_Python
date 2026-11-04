"""call/add-participants (call_invites.py): someone else into the group call
this account is in, against a fake neonize call client."""
import asyncio
from types import SimpleNamespace as N

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import call_invites as ci


@pytest.mark.parametrize("jid, target", [
    ("5511999990000@c.us", "5511999990000@s.whatsapp.net"),
    ("5511999990000@s.whatsapp.net", "5511999990000@s.whatsapp.net"),
    ("123456789@lid", "123456789@lid"),
    ("1203630000@g.us", ""),
    ("status@broadcast", ""),
    ("", ""),
    (None, ""),
])
def test_only_a_persons_address_is_a_target(jid, target):
    assert ci.target_of(jid) == target


def test_the_outcome_says_invited_rang_or_failed():
    assert ci.outcome("a", {"ok": True}) == {"jid": "a", "status": "invited"}
    assert ci.outcome("a", {"rang": True}) == {"jid": "a", "status": "rang"}
    assert ci.outcome("a", error=RuntimeError("x" * 300))["error"] == "x" * 200


class Calls:
    def __init__(self):
        self.added = []

    def add_participant(self, call_id, target):
        if target.startswith("666"):
            raise RuntimeError("usync returned no LID")
        self.added.append((call_id, target))
        return {"rang": True} if target.startswith("777") else {"ok": True}


class Media:
    def __init__(self, live="G1"):
        self.calls, self.live = Calls(), live

    def in_call(self, call_id):
        return call_id == self.live


def post(s, body):
    async def main():
        app = web.Application()
        ci.register_routes(app.router, session=lambda req: s,
                           ok=lambda **kw: web.json_response(kw))
        async with TestClient(TestServer(app)) as http:
            resp = await http.post("/api/tok/call/add-participants", json=body)
            return resp.status, await resp.json()
    return asyncio.run(main())


def session(media):
    return N(call_media=media, client=object(), status="CONNECTED")


def test_each_person_is_asked_once_in_the_order_given():
    media = Media()
    status, body = post(session(media), {"callId": "G1", "participants": [
        "5511999990000@c.us", "77712345@lid", "66612345@lid", "1203@g.us"]})
    assert status == 200
    assert [r["status"] for r in body["response"]["results"]] == [
        "invited", "rang", "failed", "failed"]
    assert media.calls.added == [("G1", "5511999990000@s.whatsapp.net"), ("G1", "77712345@lid")]


def test_only_into_the_call_this_device_is_in():
    media = Media(live="OTHER")
    status, _ = post(session(media), {"callId": "G1", "participants": ["123456789@lid"]})
    assert status == 409 and media.calls.added == []


@pytest.mark.parametrize("body", [
    {"callId": "G1", "participants": []},
    {"callId": "", "participants": ["123456789@lid"]},
    {"callId": "G1", "participants": ["123456789@lid"] * 32},
    {"callId": "G1"},
])
def test_a_bad_request_invites_nobody(body):
    media = Media()
    status, _ = post(session(media), body)
    assert status == 400 and media.calls.added == []


def test_an_older_build_answers_501():
    media = Media()
    media.calls = object()
    status, _ = post(session(media), {"callId": "G1", "participants": ["123456789@lid"]})
    assert status == 501
