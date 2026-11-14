"""Group calls already going on through the Neonize engine (ongoing_calls.py):
the routes WinZapp's WPPConnect side answers from WhatsApp Web's call store."""
import asyncio
from types import SimpleNamespace as N

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import ongoing_calls as oc


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_the_creator_keeps_its_device_and_a_person_loses_it():
    assert oc.device_jid(N(User="100", Server="lid", Device=27)) == "100:27@lid"
    assert oc.device_jid(N(User="100", Server="lid", Device=0)) == "100@lid"
    assert oc.device_jid(None) == ""
    assert oc.person_jid({"jid": "100:3@lid", "pn": "5511:3@s.whatsapp.net"}) == "5511@s.whatsapp.net"
    assert oc.person_jid({"jid": "100:3@lid"}) == "100@lid"


def test_only_connected_people_are_listed_once_with_their_saved_name():
    people = [{"jid": "1@lid", "pn": "5511@s.whatsapp.net", "state": "connected"},
              {"jid": "2@lid", "state": "invited"}, {"jid": "3@lid", "state": "receipt"},
              {"jid": "9:27@lid", "state": "connected"},
              {"jid": "1@lid", "pn": "5511@s.whatsapp.net", "state": "connected"}]
    rows = oc.connected_rows(people, {"9"},
                             lambda jid, lid: "Ana" if jid == "5511@s.whatsapp.net" else "")
    assert rows == [{"jid": "5511@s.whatsapp.net", "name": "Ana", "isSelf": False},
                    {"jid": "9@lid", "name": "", "isSelf": True}]
    assert oc.is_going_on({"participants": people}) is True
    assert oc.is_going_on({"participants": people[1:3]}) is False
    assert oc.is_going_on(None) is False


def test_the_registry_forgets_old_calls_and_reuses_a_fresh_lobby():
    clock = Clock()
    known = oc.Known(clock)
    known.remember("G1", "100:27@lid", "1203@g.us", False)
    clock.now += 5
    known.remember("G2", "100:27@lid", "1204@g.us", True)
    assert [c["id"] for c in known.recent()] == ["G2", "G1"]
    assert known.for_chat("1203@g.us")["id"] == "G1"
    known.store_lobby("G1", {"participants": []})
    assert known.fresh_lobby("G1") == (True, {"participants": []})
    clock.now += oc.LOBBY_FRESH
    assert known.fresh_lobby("G1") == (False, None)
    clock.now += oc.KEEP
    assert known.recent() == []
    known.remember("", "x", "y", False)
    assert known.recent() == []


class Media:
    def __init__(self, lobbies, live=""):
        self.lobbies, self.live, self.asked, self.joined = lobbies, live, [], []

    def ongoing_available(self):
        return True

    def lobby(self, call_id, creator):
        self.asked.append((call_id, creator))
        return self.lobbies.get(call_id)

    def roster(self, call_id):
        return {"participants": [{"jid": "1@lid", "state": "connected"},
                                 {"jid": "9@lid", "state": "connected"}]}

    def in_call(self, call_id):
        return call_id == self.live

    def live_call(self):
        return self.live

    def join_ongoing(self, call_id, creator, chat, video=False):
        self.joined.append((call_id, creator, chat, video))
        return call_id


GOING = {"call": "G1", "video": False,
         "participants": [{"jid": "1@lid", "pn": "5511@s.whatsapp.net", "state": "connected"}]}


def session(media):
    db = N(contact_of=lambda *jids: {"name": "Ana"} if "5511@s.whatsapp.net" in jids else {})
    return N(call_media=media, client=object(), status="CONNECTED", me="9@s.whatsapp.net",
             lid="9@lid", db=db)


def post(s, *requests):
    async def main():
        app = web.Application()
        oc.register_routes(app.router, session=lambda req: s,
                           ok=lambda **kw: web.json_response(kw))
        out = []
        async with TestClient(TestServer(app)) as http:
            for route, body in requests:
                resp = await http.post("/api/tok/" + route, json=body)
                out.append((resp.status, await resp.json()))
        return out
    return asyncio.run(main())


def test_a_group_shows_its_call_while_someone_is_in_it():
    media = Media({"G1": GOING})
    oc.known(media).remember("G1", "100:27@lid", "1203@g.us", False)
    (status, body), (_, listed) = post(
        session(media), ("call/group/ongoing", {"groupJid": "1203@g.us"}), ("call/ongoing", {}))
    assert status == 200
    assert body["response"] == {"id": "G1", "groupJid": "1203@g.us", "isVideo": False}
    assert listed["response"] == [{"id": "G1", "chatJid": "1203@g.us", "isVideo": False}]
    assert media.asked == [("G1", "100:27@lid")]          # one lobby answer per round


def test_a_call_nobody_is_in_any_more_is_gone():
    media = Media({"G1": None})
    oc.known(media).remember("G1", "100:27@lid", "1203@g.us", False)
    (_, body), = post(session(media), ("call/group/ongoing", {"groupJid": "1203@g.us"}))
    assert body["response"] is None and oc.known(media).get("G1") is None
    (_, other), = post(session(Media({})), ("call/group/ongoing", {"groupJid": "1299@g.us"}))
    assert other["response"] is None


def test_participants_from_the_lobby_and_from_the_call_this_client_is_in():
    media = Media({"G1": GOING})
    oc.known(media).remember("G1", "100:27@lid", "1203@g.us", False)
    (_, out), (_, unknown) = post(session(media), ("call/participants", {"callId": "G1"}),
                                  ("call/participants", {"callId": "NOPE"}))
    assert out["response"] == {"id": "G1", "available": True, "participants": [
        {"jid": "5511@s.whatsapp.net", "name": "Ana", "isSelf": False, "state": "connected"}]}
    assert unknown["response"] == {"id": "NOPE", "available": False, "participants": []}
    media.live = "G1"
    (_, live), = post(session(media), ("call/participants", {"callId": "G1"}))
    assert [p["isSelf"] for p in live["response"]["participants"]] == [False, True]


def test_joining_checks_the_call_and_the_other_call():
    media = Media({"G1": GOING})
    oc.known(media).remember("G1", "100:27@lid", "1203@g.us", False)
    s = session(media)
    (missing, _), (wrong_chat, _), (unknown, _) = post(
        s, ("call/group/join", {"callId": "G1"}),
        ("call/group/join", {"callId": "G1", "groupJid": "1299@g.us"}),
        ("call/group/join", {"callId": "G9", "groupJid": "1203@g.us"}))
    assert (missing, wrong_chat, unknown) == (400, 409, 409)
    media.live = "OTHER"
    (busy, _), = post(s, ("call/group/join", {"callId": "G1", "groupJid": "1203@g.us"}))
    assert busy == 409 and media.joined == []
    media.live = ""
    (status, body), = post(s, ("call/group/join", {"callId": "G1", "groupJid": "1203@g.us",
                                                   "isVideo": False}))
    assert status == 200
    assert body["response"]["id"] == "G1" and body["response"]["outgoing"] is False
    assert media.joined == [("G1", "100:27@lid", "1203@g.us", False)]


def test_without_the_build_the_routes_say_501():
    media = Media({})
    media.ongoing_available = lambda: False
    (status, _), = post(session(media), ("call/ongoing", {}))
    assert status == 501
    (status, _), = post(session(None), ("call/ongoing", {}))
    assert status == 501


def test_the_participant_list_keeps_who_is_not_connected():
    # the phone lists a group call's ringing and departed people too
    roster = [{"jid": "1@lid", "pn": "5511@s.whatsapp.net", "state": "connected"},
              {"jid": "2@lid", "state": "outgoing"},
              {"jid": "3@lid", "state": "terminated"},
              {"jid": "4@lid", "state": "something new"},
              {"jid": "5@lid", "state": "invited"},
              {"jid": "1@lid", "pn": "5511@s.whatsapp.net", "state": "receipt"},
              {"jid": "9@lid", "state": "connected"}]
    rows = oc.all_rows(roster, {"9"}, lambda jid, lid: "")
    assert [(r["isSelf"], r["state"]) for r in rows] == [
        (False, "connected"), (False, "ringing"), (False, "left"), (False, "invited"),
        (True, "connected")]
