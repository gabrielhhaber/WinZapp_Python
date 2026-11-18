"""Call links on the Neonize engine (call_links.py)."""
import asyncio
from types import SimpleNamespace as N

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import call_links as cl
import call_media
import calls
import ongoing_calls as oc

TOKEN = "AbCdEfGhIjKlMnOpQrStUv"            # 22 characters, like WhatsApp's
VOICE = "https://call.whatsapp.com/voice/" + TOKEN


def test_a_call_link_is_read_as_whatsapp_web_reads_it():
    assert cl.parse_link(VOICE) == (False, VOICE)
    assert cl.parse_link(" https://call.whatsapp.com/VIDEO/" + TOKEN + "?x=1 ") == (
        True, "https://call.whatsapp.com/video/" + TOKEN)
    assert cl.parse_link("https://web.whatsapp.com/call/voice/" + TOKEN) == (False, VOICE)
    for wrong in ("", None, "https://call.whatsapp.com/voice/short",
                  "https://evil.example/voice/" + TOKEN, "http://call.whatsapp.com/voice/" + TOKEN,
                  "https://chat.whatsapp.com/" + TOKEN):
        assert cl.parse_link(wrong) == (None, "")


def test_the_waiting_room_names_people_and_never_by_number():
    room = {"users": [
        {"jid": "1@lid", "pushName": "Pedro", "guest": True, "state": "waiting_room_joined"},
        {"jid": "2@lid", "pn": "5511@s.whatsapp.net", "pushName": "Ana P", "state": "waiting_room_joined"},
        {"jid": "3@lid", "state": "waiting_room_joined"},
        {"jid": "4@lid", "pushName": "Gone", "state": "denied"},
        {"jid": "1@lid", "pushName": "Pedro", "guest": True, "state": "waiting_room_joined"}]}
    rows = cl.waiting_rows(room, lambda jid, lid: "Ana" if jid == "5511@s.whatsapp.net" else "")
    assert rows == [{"jid": "1@lid", "name": "Pedro", "guest": True},
                    {"jid": "2@lid", "name": "Ana", "guest": False},
                    {"jid": "3@lid", "name": "", "guest": False}]
    assert cl.waiting_rows(None, lambda *a: "") == []


def test_a_guest_in_the_roster_is_named_by_the_name_it_typed():
    people = [{"jid": "7@lid", "pushName": "Pedro", "guest": True, "state": "connected"}]
    assert oc.all_rows(people, set(), lambda jid, lid: "") == [
        {"jid": "7@lid", "name": "Pedro", "isSelf": False, "state": "connected"}]


def test_a_link_call_is_a_group_call_with_no_chat_and_no_record():
    event = call_media.state_event({"id": "L1", "peer": "", "outgoing": False, "link": True},
                                   state="ACTIVE", now=5)
    assert event["isGroup"] is True and event["isCallLink"] is True and event["groupJid"] == ""
    info = call_media.finished_info({"id": "L1", "peer": "", "link": True, "active_at": 1},
                                    "ENDED", 9)
    assert info["link"] is True
    # a peer "" would have become the chat "@s.whatsapp.net"
    assert calls.record_finished(N(), info, 9) is False


class Media:
    def __init__(self, live=""):
        self.live, self.created, self.joined, self.decided = live, [], [], []
        self.room = {"enabled": True, "admin": True, "waiting": False, "users": [
            {"jid": "1@lid", "pushName": "Pedro", "guest": True, "state": "waiting_room_joined"}]}

    def call_links_available(self):
        return True

    def live_call(self):
        return self.live

    def create_link(self, video=False):
        self.created.append(video)
        return {"link": ("https://call.whatsapp.com/video/" if video else "https://call.whatsapp.com/voice/")
                + TOKEN, "video": video}

    def join_link(self, link, video=False):
        self.joined.append((link, video))
        return "L1", {"call": "L1", "waiting": False, "admin": True}

    def waiting_room(self, call_id):
        return self.room if call_id == "L1" else None

    def decide_waiting(self, call_id, user, admit):
        self.decided.append((call_id, user, admit))
        return {"ok": True}


def session(media):
    db = N(contact_of=lambda *jids: {})
    return N(call_media=media, client=object(), status="CONNECTED", db=db)


def post(s, *requests):
    async def main():
        app = web.Application()
        cl.register_routes(app.router, session=lambda req: s,
                           ok=lambda **kw: web.json_response(kw))
        out = []
        async with TestClient(TestServer(app)) as http:
            for route, body in requests:
                resp = await http.post("/api/tok/" + route, json=body)
                out.append((resp.status, await resp.json()))
        return out
    return asyncio.run(main())


def test_creating_a_link_answers_the_link_and_its_kind():
    media = Media()
    (status, body), = post(session(media), ("call/link/create", {"isVideo": True}))
    assert status == 200
    assert body["response"] == {"link": "https://call.whatsapp.com/video/" + TOKEN, "isVideo": True}
    assert media.created == [True]


def test_joining_takes_the_kind_from_the_link_and_refuses_a_second_call():
    media = Media()
    s = session(media)
    (bad, _), (status, body) = post(s, ("call/link/join", {"link": "https://x.y/voice/" + TOKEN}),
                                    ("call/link/join", {"link": VOICE, "isVideo": True}))
    assert bad == 400
    assert status == 200 and media.joined == [(VOICE, False)]
    assert body["response"] == {"id": "L1", "isVideo": False, "isCallLink": True, "peerJid": "",
                                "waiting": False, "admin": True, "state": "CONNECTING",
                                "outgoing": False}
    media.live = "OTHER"
    (busy, _), = post(s, ("call/link/join", {"link": VOICE}))
    assert busy == 409 and len(media.joined) == 1


def test_the_admin_sees_who_waits_and_lets_them_in():
    media = Media()
    s = session(media)
    (status, room), (over, _), (admitted, _), (denied, _), (bad, _) = post(
        s, ("call/link/waiting-room", {"callId": "L1"}), ("call/link/waiting-room", {"callId": "L9"}),
        ("call/link/admit", {"callId": "L1", "jid": "1@lid"}),
        ("call/link/admit", {"callId": "L1", "jid": "1@lid", "admit": False}),
        ("call/link/admit", {"callId": "L1", "jid": "1203@g.us"}))
    assert status == 200 and room["response"] == {
        "id": "L1", "admin": True, "enabled": True, "waiting": False,
        "people": [{"jid": "1@lid", "name": "Pedro", "guest": True}]}
    assert over == 404
    assert (admitted, denied, bad) == (200, 200, 400)
    assert media.decided == [("L1", "1@lid", True), ("L1", "1@lid", False)]


def test_without_the_build_the_routes_say_501():
    media = Media()
    media.call_links_available = lambda: False
    (status, _), = post(session(media), ("call/link/create", {}))
    assert status == 501
    (status, _), = post(session(None), ("call/link/join", {"link": VOICE}))
    assert status == 501
