"""Business message buttons: from the proto to the fields WinZapp reads, the
reply plan, the reply proto and the reply-button route."""
import asyncio
import json
from types import SimpleNamespace as N

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import ContextInfo, Message

import buttons
import chat_interactive as ci
from store import Store


def with_buttons():
    m = Message()
    m.buttonsMessage.contentText = "Choose"
    for i, (id_, text) in enumerate((("yes", "Yes"), ("no", "No"))):
        b = m.buttonsMessage.buttons.add()
        b.buttonID, b.type = id_, 1
        b.buttonText.displayText = text
    native = m.buttonsMessage.buttons.add()      # NATIVE_FLOW is not a quick reply
    native.type = 2
    return m


def with_list():
    m = Message()
    m.listMessage.title, m.listMessage.description = "Menu", "Orders"
    m.listMessage.buttonText = "View"
    section = m.listMessage.sections.add()
    section.title = "A"
    for rid, title in (("r1", "One"), ("r2", "Two")):
        row = section.rows.add()
        row.rowID, row.title, row.description = rid, title, "d"
    other = m.listMessage.sections.add()
    row = other.rows.add()
    row.rowID, row.title = "r3", "Three"
    return m


def with_template():
    m = Message()
    t = m.templateMessage.hydratedTemplate
    t.hydratedContentText = "Hi"
    q = t.hydratedButtons.add()
    q.quickReplyButton.displayText, q.quickReplyButton.ID = "Ok", "ok"
    u = t.hydratedButtons.add()
    u.urlButton.displayText, u.urlButton.URL = "Site", "https://x.test"
    t.hydratedButtons.add()
    return m


def with_native_flow():
    m = Message()
    m.interactiveMessage.body.text = "Body"
    b = m.interactiveMessage.nativeFlowMessage.buttons.add()
    b.name, b.buttonParamsJSON = "quick_reply", '{"display_text":"Hi","id":"x"}'
    return m


def test_message_without_buttons_gets_no_fields():
    assert buttons.button_fields(Message(conversation="hi")) == {}
    assert buttons.button_fields(Message()) == {}


def test_reply_buttons():
    fields = buttons.button_fields(with_buttons())
    assert fields["type"] == "buttons"
    assert fields["body"] == "Choose"
    assert fields["dynamicReplyButtons"][:2] == [
        {"buttonId": "yes", "buttonText": {"displayText": "Yes"}, "type": 1},
        {"buttonId": "no", "buttonText": {"displayText": "No"}, "type": 1}]
    assert len(fields["dynamicReplyButtons"]) == 2      # the NATIVE_FLOW does not count


def test_list_becomes_sections_and_rows():
    fields = buttons.button_fields(with_list())
    assert fields["type"] == "list" and fields["body"] == "Orders"
    sections = fields["list"]["sections"]
    assert [r["rowId"] for s in sections for r in s["rows"]] == ["r1", "r2", "r3"]
    assert fields["list"]["buttonText"] == "View"


def test_template_keeps_the_position_of_each_button():
    fields = buttons.button_fields(with_template())
    assert fields["type"] == "template"
    assert fields["body"] == "Hi"
    assert fields["hydratedButtons"][0] == {
        "index": 0, "quickReplyButton": {"displayText": "Ok", "id": "ok"}}
    assert fields["hydratedButtons"][1]["urlButton"]["url"] == "https://x.test"
    assert fields["hydratedButtons"][2] == {}


def test_native_flow():
    fields = buttons.button_fields(with_native_flow())
    assert fields["type"] == "interactive"
    assert fields["body"] == "Body"
    assert fields["interactivePayload"]["buttons"] == [
        {"name": "quick_reply", "buttonParamsJson": '{"display_text":"Hi","id":"x"}'}]


def test_carousel_without_native_buttons_keeps_its_card_text():
    msg = Message()
    card = msg.interactiveMessage.carouselMessage.cards.add()
    card.header.title = "Ticket"
    card.body.text = "Departure at noon"
    shown = buttons.button_fields(msg)
    assert shown == {"type": "interactive", "body": "Ticket\nDeparture at noon"}


def test_carousel_keeps_card_buttons_and_reply_positions():
    msg = Message()
    first = msg.interactiveMessage.carouselMessage.cards.add()
    first.header.title = "Outbound"
    first.body.text = "Trip one"
    first.footer.text = "Sleeper seat"
    link = first.nativeFlowMessage.buttons.add()
    link.name, link.buttonParamsJSON = "cta_url", '{"display_text":"Details","url":"https://example.test/1"}'
    reply = first.nativeFlowMessage.buttons.add()
    reply.name, reply.buttonParamsJSON = "quick_reply", '{"display_text":"Choose","id":"trip-1"}'
    second = msg.interactiveMessage.carouselMessage.cards.add()
    second.header.title = "Return"
    second.body.text = "Trip two"
    reply = second.nativeFlowMessage.buttons.add()
    reply.name, reply.buttonParamsJSON = "quick_reply", '{"display_text":"Choose","id":"trip-2"}'

    fields = buttons.button_fields(msg)
    assert fields["body"] == "Outbound\nTrip one\nSleeper seat\nReturn\nTrip two"
    actions = fields["interactivePayload"]["buttons"]
    assert [(b["name"], b["cardIndex"], b["cardTitle"]) for b in actions] == [
        ("cta_url", 0, "Outbound"), ("quick_reply", 0, "Outbound"),
        ("quick_reply", 1, "Return")]
    assert [b["cardText"] for b in actions] == [
        "Outbound\nTrip one\nSleeper seat", "Outbound\nTrip one\nSleeper seat",
        "Return\nTrip two"]
    assert buttons.reply_plan(fields, "native", 1) == {
        "kind": "carousel", "id": "trip-1", "text": "Choose",
        "index": 1, "cardIndex": 0}
    assert buttons.reply_plan(fields, "native", 2) == {
        "kind": "carousel", "id": "trip-2", "text": "Choose",
        "index": 0, "cardIndex": 1}


def test_reply_messages_keep_the_selected_text():
    msg = Message()
    msg.buttonsResponseMessage.selectedDisplayText = "Tomorrow"
    msg.buttonsResponseMessage.selectedButtonID = "tomorrow"
    assert buttons.button_fields(msg) == {
        "type": "buttons_response", "body": "Tomorrow", "selectedButtonID": "tomorrow"}

    msg = Message()
    msg.listResponseMessage.title = "Bus station"
    msg.listResponseMessage.singleSelectReply.selectedRowID = "station-1"
    assert buttons.button_fields(msg) == {
        "type": "list_response", "body": "Bus station", "selectedRowID": "station-1"}

    msg = Message()
    msg.interactiveResponseMessage.body.text = "Confirm"
    assert buttons.button_fields(msg) == {"type": "chat", "body": "Confirm"}

    msg = Message()
    msg.interactiveResponseMessage.nativeFlowResponseMessage.paramsJSON = '{"display_text":"Confirm"}'
    assert buttons.button_fields(msg) == {"type": "chat", "body": "Confirm"}


def stored(m):
    return buttons.button_fields(m)


def test_plan_of_each_source():
    assert buttons.reply_plan(stored(with_buttons()), "dynamic", 1) == {
        "kind": "button", "id": "no", "text": "No"}
    assert buttons.reply_plan(stored(with_template()), "template", 0) == {
        "kind": "template", "id": "ok", "text": "Ok", "index": 0}
    # the row position counts the sections in sequence, like the client
    assert buttons.reply_plan(stored(with_list()), "list", 2) == {
        "kind": "list", "id": "r3", "title": "Three", "description": ""}


def test_plan_refuses_a_button_that_does_not_exist_or_is_not_a_reply():
    def refuse(wpp, source, index):
        with pytest.raises(buttons.ButtonRefused) as e:
            buttons.reply_plan(wpp, source, index)
        return e.value.code, e.value.status
    assert refuse(stored(with_buttons()), "dynamic", 5) == ("button_not_found", 404)
    assert refuse({}, "dynamic", 0) == ("button_not_found", 404)
    assert refuse(stored(with_template()), "template", 1) == ("button_not_found", 404)   # link
    assert refuse(stored(with_template()), "template", 2) == ("button_not_found", 404)   # empty
    assert refuse(stored(with_list()), "list", 3) == ("button_not_found", 404)
    assert refuse({}, "other", 0) == ("invalid_request", 400)


def test_native_flow_quick_reply_plan():
    wpp = stored(with_native_flow())
    assert buttons.reply_plan(wpp, "native", 0) == {"kind": "flow", "id": "x", "text": "Hi"}


def test_native_flow_refuses_other_buttons_and_broken_params():
    def refuse(wpp, source, index):
        with pytest.raises(buttons.ButtonRefused) as e:
            buttons.reply_plan(wpp, source, index)
        return e.value.code, e.value.status
    other = with_native_flow()
    other.interactiveMessage.nativeFlowMessage.buttons[0].name = "cta_url"
    assert refuse(stored(other), "native", 0) == ("button_not_found", 404)
    for bad in ("", "not json", "[]", '{"id":"x"}'):
        broken = with_native_flow()
        broken.interactiveMessage.nativeFlowMessage.buttons[0].buttonParamsJSON = bad
        assert refuse(stored(broken), "native", 0) == ("button_not_found", 404)


def test_reply_protos():
    ctx = ContextInfo(stanzaID="ID1")
    r = buttons.build_reply({"kind": "button", "id": "yes", "text": "Yes"}, ctx)
    assert r.buttonsResponseMessage.selectedButtonID == "yes"
    assert r.buttonsResponseMessage.selectedDisplayText == "Yes"
    assert r.buttonsResponseMessage.contextInfo.stanzaID == "ID1"
    r = buttons.build_reply({"kind": "template", "id": "ok", "text": "Ok", "index": 3}, ctx)
    assert (r.templateButtonReplyMessage.selectedID,
            r.templateButtonReplyMessage.selectedIndex) == ("ok", 3)
    r = buttons.build_reply(
        {"kind": "list", "id": "r1", "title": "One", "description": "d"}, ctx)
    assert r.listResponseMessage.singleSelectReply.selectedRowID == "r1"
    assert r.listResponseMessage.title == "One" and r.listResponseMessage.description == "d"
    r = buttons.build_reply({"kind": "flow", "id": "x", "text": "Olá"}, ctx)
    flow = r.interactiveResponseMessage.nativeFlowResponseMessage
    assert r.interactiveResponseMessage.body.text == "Olá"
    assert (flow.name, flow.version) == ("quick_reply", 3)
    assert json.loads(flow.paramsJSON) == {"display_text": "Olá", "id": "x"}
    assert r.interactiveResponseMessage.contextInfo.stanzaID == "ID1"
    r = buttons.build_reply(
        {"kind": "carousel", "id": "trip-2", "text": "Select",
         "index": 0, "cardIndex": 1}, ctx)
    card = r.templateButtonReplyMessage
    assert (card.selectedID, card.selectedDisplayText,
            card.selectedIndex, card.selectedCarouselCardIndex) == (
                "trip-2", "Select", 0, 1)
    assert card.contextInfo.stanzaID == "ID1"


def test_valid_request():
    serial = "false_5511999990000@c.us_ABCDEF1234"
    assert ci.valid_request(serial, {"source": "list", "index": 0}) == ("list", 0)
    assert ci.valid_request(serial, {"source": "native", "index": 31}) == ("native", 31)
    bad = [
        ("false_1203@g.us_ABCDEF1234", {"source": "list", "index": 0}),      # group
        (serial, {"source": "x", "index": 0}),
        (serial, {"source": "list", "index": -1}),
        (serial, {"source": "list", "index": 32}),
        (serial, {"source": "list", "index": True}),
        (serial, {"source": "list", "index": "1"}),
        (serial, None),
    ]
    for s, body in bad:
        with pytest.raises(buttons.ButtonRefused) as e:
            ci.valid_request(s, body)
        assert e.value.status == 400


SERIAL = "false_5511999990000@c.us_ABCDEF1234"


def ask(tmp_path, body, connected=True, store=True, fails=False, serial=SERIAL,
        message_fields=None):
    db = Store(str(tmp_path / "x.db"))
    if store:
        original = {"id": {"id": "ABCDEF1234", "_serialized": SERIAL}, "t": 5,
                    "fromMe": False, "type": "chat",
                    **(message_fields if message_fields is not None else stored(with_buttons()))}
        db.message("5511999990000@s.whatsapp.net", original)
    sent = []

    def send_message(destination, message):
        if fails:
            raise RuntimeError("refused")
        sent.append((destination, message))
    s = N(db=db, client=N(send_message=send_message) if connected else None,
          status="CONNECTED" if connected else "CLOSED",
          id_parts=staticmethod(lambda ser: (
              ser.startswith("true_"), "5511999990000@s.whatsapp.net", ser.split("_")[2], "")))

    async def run():
        app = web.Application()
        ci.register_routes(app.router, session=lambda req: s, ok=lambda **kw: web.json_response(kw),
                           destination_of=lambda text: "JID:" + text,
                           quoted_context=lambda session, quoted: ContextInfo(stanzaID="ABCDEF1234"))
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/api/t/reply-button/" + serial, json=body)
            return response.status, await response.json()
    status, answer = asyncio.run(run())
    return status, answer, sent


def test_route_sends_the_reply_once(tmp_path):
    status, body, sent = ask(tmp_path, {"source": "dynamic", "index": 0})
    assert status == 200 and body == {"status": "success", "response": {"sent": True}}
    assert len(sent) == 1
    destination, message = sent[0]
    assert destination == "JID:5511999990000@s.whatsapp.net"
    assert message.buttonsResponseMessage.selectedButtonID == "yes"


def test_route_sends_a_carousel_card_selection(tmp_path):
    msg = Message()
    for card_index in range(2):
        card = msg.interactiveMessage.carouselMessage.cards.add()
        button = card.nativeFlowMessage.buttons.add()
        button.name = "quick_reply"
        button.buttonParamsJSON = json.dumps(
            {"display_text": "Select", "id": f"trip-{card_index}"})
    status, body, sent = ask(
        tmp_path, {"source": "native", "index": 1},
        message_fields=buttons.button_fields(msg))
    assert status == 200 and len(sent) == 1
    reply = sent[0][1].templateButtonReplyMessage
    assert (reply.selectedID, reply.selectedIndex,
            reply.selectedCarouselCardIndex) == ("trip-1", 0, 1)


def test_route_answers_with_the_code_the_client_reads(tmp_path):
    scenarios = [
        ({"source": "dynamic", "index": 9}, {}, 404, "button_not_found"),
        ({"source": "dynamic"}, {}, 400, "invalid_request"),
        ({"source": "native", "index": 0}, {}, 404, "button_not_found"),
        ({"source": "dynamic", "index": 0}, {"store": False}, 404, "message_not_found"),
        ({"source": "dynamic", "index": 0}, {"connected": False}, 503, "reply_button_unavailable"),
        ({"source": "dynamic", "index": 0}, {"fails": True}, 503, "reply_button_unavailable"),
    ]
    for i, (body, extra, status, code) in enumerate(scenarios):
        folder = tmp_path / str(i)       # a new store per scenario
        folder.mkdir()
        got, answer, sent = ask(folder, body, **extra)
        assert (got, answer) == (status, {"status": "error", "code": code}), body
        assert not sent
