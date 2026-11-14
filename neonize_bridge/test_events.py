"""WhatsApp events on the Neonize engine (events.py).

The two vectors below were made by WhatsApp Web itself on 2026-11-12, over
CDP, with WAWebAddonEncryption.encryptAddOn: secret 1..32, iv 100..111, event
id 3EB0TESTID, creator 111@lid, answering (or editing) 222@lid. They pin the
key derivation, the additional data and the plaintext to WhatsApp's own."""
import asyncio
import base64
from types import SimpleNamespace as N

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import (
    EncEventResponseMessage, EventMessage, EventResponseMessage, LocationMessage, Message,
    MessageContextInfo, SecretEncryptedMessage)

import events
from store import Store

SECRET = bytes(range(1, 33))
IV = bytes(range(100, 112))
WA_RESPONSE = base64.b64decode("LI5BpgMY49AZFOTEw7zZeiXklojjK4u9kC50")   # maybe, +2 guests
WA_EDIT = base64.b64decode("v33+1GZ4lYDu9xwD+GvgQEVvoCF7Ui8EQ8GIA+iXwy1UVoAmVTURinHYQ/i/qw==")


def test_an_answer_whatsapp_web_encrypted_is_read():
    plain = events.decrypt(SECRET, events.USE_RESPONSE, "3EB0TESTID", ["111@lid"], ["222@lid"],
                           IV, WA_RESPONSE)
    answer = EventResponseMessage.FromString(plain)
    assert (answer.response, answer.extraGuestCount) == (3, 2)


def test_an_answer_encrypted_here_is_byte_for_byte_whatsapp_webs():
    plain = EventResponseMessage(response=3, timestampMS=1700000000000, extraGuestCount=2)
    ct, _ = events.encrypt(SECRET, events.USE_RESPONSE, "3EB0TESTID", "111@lid", "222@lid",
                           plain.SerializeToString(), iv=IV)
    assert ct == WA_RESPONSE


def test_an_edit_whatsapp_web_encrypted_is_read():
    plain = events.decrypt(SECRET, events.USE_EDIT, "3EB0TESTID", ["111@lid"], ["222@lid"], IV, WA_EDIT)
    event = events.edited_event(plain)
    assert (event.name, event.startTime, event.isCanceled) == ("Novo", 1800000000, True)


def test_the_other_address_is_tried_and_a_wrong_one_refused():
    found = events.decrypt(SECRET, events.USE_RESPONSE, "3EB0TESTID",
                           ["5511@s.whatsapp.net", "111@lid"], ["999@s.whatsapp.net", "222:7@lid"],
                           IV, WA_RESPONSE)
    assert found is not None
    assert events.decrypt(SECRET, events.USE_RESPONSE, "3EB0TESTID", ["111@lid"], ["333@lid"],
                          IV, WA_RESPONSE) is None


def test_an_event_message_becomes_whatsapp_webs_event_creation():
    event = EventMessage(name="Churrasco", description="No sítio", startTime=10, endTime=20,
                         location=LocationMessage(name="Sítio", address="Rua 1"),
                         joinLink="https://call.whatsapp.com/video/x", extraGuestsAllowed=True)
    w = events.event_fields(event)
    assert w["type"] == "event_creation" and w["eventLocation"] == "Sítio, Rua 1"
    assert (w["eventStartTime"], w["eventEndTime"], w["eventExtraGuestsAllowed"]) == (10, 20, True)


def test_the_own_address_follows_the_chat():
    s = N(lid="99:3@lid", me="5511:3@s.whatsapp.net")
    assert events.own_address_for(s, "1@g.us") == "99@lid"
    assert events.own_address_for(s, "7@lid") == "99@lid"
    assert events.own_address_for(s, "5522@s.whatsapp.net") == "5511@s.whatsapp.net"


# ------------------------------------------------------------ receiving
class _Session:
    name = "acc"
    me = "222@s.whatsapp.net"
    lid = "222@lid"

    def __init__(self):
        self.db = Store(":memory:")
        self.sent = []

    def emit(self, event, data):
        self.sent.append((event, data))


def _jid(text):
    user, _, server = text.partition("@")
    return N(User=user, Server=server)


def _jid_text(j):
    return f"{j.User}@{j.Server}" if j is not None and getattr(j, "User", "") else ""


def _source(sender, from_me=False, alt=""):
    return N(Sender=_jid(sender), SenderAlt=_jid(alt) if alt else None, IsFromMe=from_me)


def test_an_event_keeps_its_secret_and_answers_and_edits_arrive():
    s = _Session()
    created = Message(eventMessage=EventMessage(name="Antes", startTime=1800000000),
                      messageContextInfo=MessageContextInfo(messageSecret=SECRET))
    s.db.message("1@g.us", {"id": {"id": "3EB0TESTID"}, "t": 1, "type": "event_creation",
                            "eventName": "Antes", "eventStartTime": 1800000000})
    events.remember(s, "1@g.us", "3EB0TESTID", False, _source("111@lid"), created, _jid_text)
    assert s.db.event_secret("3EB0TESTID")[1] == ["111@lid"]

    answer = Message(encEventResponseMessage=EncEventResponseMessage(
        eventCreationMessageKey={"ID": "3EB0TESTID"}, encPayload=WA_RESPONSE, encIV=IV))
    e = N(Message=answer, Info=N(Timestamp=1700000000))
    # the answer came from 222, known here by its number first and its LID as the other address
    assert events.received(s, "1@g.us", _source("5522@s.whatsapp.net", alt="222@lid"), e, _jid_text)
    assert events.responses_wpp(s, "3EB0TESTID") == {"mine": 0, "responses": [
        {"sender": "5522@c.us", "response": 3, "extraGuestCount": 2, "isMe": False}]}

    edit = Message(secretEncryptedMessage=SecretEncryptedMessage(
        targetMessageKey={"ID": "3EB0TESTID"}, encPayload=WA_EDIT, encIV=IV, secretEncType=1))
    assert events.received(s, "1@g.us", _source("222@lid"), N(Message=edit, Info=N(Timestamp=1)), _jid_text)
    stored = s.db.wpp_message("3EB0TESTID")
    assert stored["eventName"] == "Novo" and stored["isEventCanceled"] is True
    assert [k for k, _ in s.sent] == ["event-changed", "event-changed"]


def test_an_ordinary_message_is_not_taken():
    s = _Session()
    plain = N(Message=Message(conversation="oi"), Info=N(Timestamp=1))
    assert events.received(s, "1@g.us", _source("1@lid"), plain, _jid_text) is False


# ------------------------------------------------------------ routes
class _Client:
    def __init__(self):
        self.sent = []

    def send_message(self, to, message):
        self.sent.append((to, message))
        return N(ID="3EB0NEWID", Timestamp=5, Message=message)


def _routes(s):
    if getattr(s, "client", None) is None:
        s.client, s.status = _Client(), "CONNECTED"

    def id_parts(serial):
        p = serial.split("_")
        return p[0] == "true", p[1].replace("@c.us", "@s.whatsapp.net"), p[2], p[3] if len(p) > 3 else ""
    s.id_parts = id_parts
    app = web.Application()

    async def in_thread(func, *a):
        return func(*a)
    events.register_routes(app.router, session=lambda req: s, ok=lambda **kw: web.json_response(kw),
                           destination_of=lambda phone: phone,
                           store_sent=lambda s_, dest, resp: web.json_response(
                               {"status": "success", "response": [{"id": "true_x_" + resp.ID}]}),
                           in_thread=in_thread)
    return app


def _post(s, *requests):
    async def main():
        out = []
        async with TestClient(TestServer(_routes(s))) as http:
            for route, body in requests:
                resp = await http.post("/api/tok/" + route, json=body)
                out.append((resp.status, await resp.json()))
        return out
    return asyncio.run(main())


def test_create_answer_edit_and_cancel_through_the_routes():
    s = _Session()
    (status, body), = _post(s, ("send-event", {"phone": "1@g.us", "name": "Churrasco",
                                               "startTime": 1800000000, "location": "Sítio"}))
    assert status == 200 and body["response"] == [{"id": "true_x_3EB0NEWID"}]
    _to, created = s.client.sent[0]
    assert created.eventMessage.name == "Churrasco" and created.eventMessage.location.name == "Sítio"
    assert len(created.messageContextInfo.messageSecret) == 32
    assert s.db.event_secret("3EB0NEWID")[1] == ["222@lid", "222@s.whatsapp.net"]

    s.db.message("1@g.us", {"id": {"id": "3EB0NEWID"}, "t": 1, "type": "event_creation",
                            "eventName": "Churrasco", "eventStartTime": 1800000000})
    serial = "true_1@g.us_3EB0NEWID"
    (s1, _), (s2, _), (s3, _) = _post(
        s, ("event-respond", {"messageId": serial, "response": 1}),
        ("edit-event", {"messageId": serial, "description": "Na praia"}),
        ("edit-event", {"messageId": serial, "cancel": True}))
    assert (s1, s2, s3) == (200, 200, 200)
    answer, edit, cancel = [m for _to, m in s.client.sent[1:]]
    secret = s.db.event_secret("3EB0NEWID")[2]
    plain = events.decrypt(secret, events.USE_RESPONSE, "3EB0NEWID", ["222@lid"], ["222@lid"],
                           answer.encEventResponseMessage.encIV, answer.encEventResponseMessage.encPayload)
    assert EventResponseMessage.FromString(plain).response == 1
    assert events.responses_wpp(s, "3EB0NEWID")["mine"] == 1
    assert edit.secretEncryptedMessage.secretEncType == 1
    assert s.db.wpp_message("3EB0NEWID")["isEventCanceled"] is True
    # canceled: no more answers or edits
    (s4, b4), = _post(s, ("event-respond", {"messageId": serial, "response": 2}))
    assert (s4, b4["code"]) == (400, "event_canceled")


def test_the_routes_refuse_what_they_cannot_do():
    s = _Session()
    (a, ba), (b, bb), (c, bc) = _post(
        s, ("send-event", {"phone": "1@g.us", "name": "X", "startTime": 1, "callType": "video"}),
        ("send-event", {"phone": "1@g.us", "name": " ", "startTime": 1}),
        ("event-respond", {"messageId": "true_1@g.us_NOPE", "response": 1}))
    assert (a, ba["code"]) == (400, "event_call_unsupported")
    assert (b, bb["code"]) == (400, "event_invalid")
    assert (c, bc["code"]) == (400, "event_not_found")
