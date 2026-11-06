"""Buttons of business messages: the fields WinZapp reads in a message
(`client/core/business_buttons.py`) and the reply to a button. Pure functions,
no server; `chat_interactive.py` wires the `reply-button` route.

Two halves:

- `button_fields(message)`: whatsmeow message (already unwrapped) -> the extra
  fields of the WhatsApp Web format (`dynamicReplyButtons`, `list`,
  `hydratedButtons`, `interactivePayload`). The POSITION of each button in the
  list is what the client sends back as `index`, so a button the client does
  not know how to use becomes `{}` instead of leaving the list.
- `reply_plan(wpp, source, index)`: the stored message + the chosen button ->
  what to send, as a plain dictionary; `build_reply` turns it into the proto.

Replies that exist: reply button ("dynamic", ButtonsResponseMessage), list row
("list", ListResponseMessage), template button ("template",
TemplateButtonReplyMessage) and the quick reply of an interactive message
("native", InteractiveResponseMessage with a `quick_reply`
nativeFlowResponseMessage). A carousel card uses TemplateButtonReplyMessage
with its card index. The other native buttons
(link, copy, call) are opened by the client itself and never come here.

The carousel reply format has unit tests but awaits confirmation on a real account.
"""
import json

import commerce_messages

SOURCES = ("dynamic", "native", "template", "list")


class ButtonRefused(Exception):
    """Refusal with the code and the HTTP status that the WinZapp server returns."""

    def __init__(self, code, status):
        super().__init__(code)
        self.code = code
        self.status = status


def _has(message, field):
    try:
        return message.HasField(field)
    except ValueError:
        return False


def _dynamic(body):
    return [{"buttonId": b.buttonID,
             "buttonText": {"displayText": b.buttonText.displayText}, "type": 1}
            if b.buttonID and b.buttonText.displayText else {}
            for b in body.buttons if b.type != 2]      # 2 = NATIVE_FLOW


def _list(body):
    return {"title": body.title, "description": body.description,
            "buttonText": body.buttonText,
            "sections": [{"title": sec.title, "rows": [
                {"rowId": row.rowID, "title": row.title, "description": row.description}
                for row in sec.rows]} for sec in body.sections]}


def _template(body):
    out = []
    for i, b in enumerate(body.hydratedButtons):
        if _has(b, "quickReplyButton"):
            out.append({"index": i, "quickReplyButton": {
                "displayText": b.quickReplyButton.displayText, "id": b.quickReplyButton.ID}})
        elif _has(b, "urlButton"):
            out.append({"index": i, "urlButton": {
                "displayText": b.urlButton.displayText, "url": b.urlButton.URL}})
        elif _has(b, "callButton"):
            out.append({"index": i, "callButton": {
                "displayText": b.callButton.displayText,
                "phoneNumber": b.callButton.phoneNumber}})
        else:
            out.append({})
    return out


def button_fields(message) -> dict:
    """Button fields of the whatsmeow message (already unwrapped); empty if it
    has no buttons."""
    if _has(message, "buttonsMessage"):
        b = message.buttonsMessage
        return {"type": "buttons", "body": b.contentText or b.text,
                "dynamicReplyButtons": _dynamic(b)}
    if _has(message, "listMessage"):
        return {"type": "list", "body": message.listMessage.description,
                "list": _list(message.listMessage)}
    if _has(message, "templateMessage"):
        t = message.templateMessage
        template = t.hydratedTemplate if _has(t, "hydratedTemplate") else \
            t.hydratedFourRowTemplate
        return {"type": "template", "body": template.hydratedContentText,
                "hydratedButtons": _template(template)}
    if _has(message, "interactiveMessage"):
        i = message.interactiveMessage
        parts = [i.header.title, i.body.text, i.footer.text]
        card_buttons = []
        for card_index, card in enumerate(i.carouselMessage.cards):
            card_parts = (card.header.title, card.body.text, card.footer.text)
            parts.extend(card_parts)
            card_buttons.extend(
                {"name": b.name, "buttonParamsJson": b.buttonParamsJSON,
                 "cardIndex": card_index,
                 "cardTitle": card.header.title or card.body.text,
                 "cardText": "\n".join(part for part in card_parts if part)}
                for b in card.nativeFlowMessage.buttons)
        fields = {"type": "interactive", "body": "\n".join(p for p in parts if p)}
        if _has(i, "nativeFlowMessage"):
            buttons = i.nativeFlowMessage.buttons
            fields["interactivePayload"] = {"buttons": [
                {"name": b.name, "buttonParamsJson": b.buttonParamsJSON}
                for b in buttons]}
            flow = commerce_messages.native_flow(buttons[0].name) if buttons else ""
            if flow:        # the client tells the order notice from the rest by this
                fields["nativeFlowName"] = flow
        if card_buttons:
            fields.setdefault("interactivePayload", {"buttons": []})["buttons"].extend(card_buttons)
        return fields
    if _has(message, "buttonsResponseMessage"):
        b = message.buttonsResponseMessage
        return {"type": "buttons_response", "body": b.selectedDisplayText,
                "selectedButtonID": b.selectedButtonID}
    if _has(message, "listResponseMessage"):
        b = message.listResponseMessage
        return {"type": "list_response", "body": b.title or b.description,
                "selectedRowID": b.singleSelectReply.selectedRowID}
    if _has(message, "templateButtonReplyMessage"):
        return {"type": "chat", "body": message.templateButtonReplyMessage.selectedDisplayText}
    if _has(message, "interactiveResponseMessage"):
        response = message.interactiveResponseMessage
        body = response.body.text
        if not body:
            try:
                params = json.loads(response.nativeFlowResponseMessage.paramsJSON or "null")
            except ValueError:
                params = None
            if isinstance(params, dict) and isinstance(params.get("display_text"), str):
                body = params["display_text"]
        return {"type": "chat", "body": body}
    return {}


def _item(items, index):
    if not isinstance(items, list) or not 0 <= index < len(items) \
            or not isinstance(items[index], dict) or not items[index]:
        raise ButtonRefused("button_not_found", 404)
    return items[index]


def reply_plan(wpp, source, index):
    """What to send when button `index` of `source` is tapped in message `wpp`
    (WhatsApp Web format), or ButtonRefused."""
    if source == "dynamic":
        b = _item(wpp.get("dynamicReplyButtons"), index)
        return {"kind": "button", "id": b["buttonId"],
                "text": b["buttonText"]["displayText"]}
    if source == "template":
        b = _item(wpp.get("hydratedButtons"), index).get("quickReplyButton")
        if not b:
            raise ButtonRefused("button_not_found", 404)
        return {"kind": "template", "id": b["id"], "text": b["displayText"],
                "index": index}
    if source == "list":
        rows = [row for sec in (wpp.get("list") or {}).get("sections") or []
                for row in sec.get("rows") or []]
        row = _item(rows, index)
        if not row.get("rowId") or not row.get("title"):
            raise ButtonRefused("button_not_found", 404)
        return {"kind": "list", "id": row["rowId"], "title": row["title"],
                "description": row.get("description") or ""}
    if source == "native":
        native_buttons = (wpp.get("interactivePayload") or {}).get("buttons")
        button = _item(native_buttons, index)
        if button.get("name") != "quick_reply":
            raise ButtonRefused("button_not_found", 404)
        try:
            params = json.loads(button.get("buttonParamsJson") or "")
        except ValueError:
            params = None
        if not isinstance(params, dict) or not params.get("display_text"):
            raise ButtonRefused("button_not_found", 404)
        card_index = button.get("cardIndex")
        if isinstance(card_index, int) and not isinstance(card_index, bool) and card_index >= 0:
            card_button_index = sum(
                1 for earlier in native_buttons[:index]
                if isinstance(earlier, dict) and earlier.get("cardIndex") == card_index)
            return {"kind": "carousel", "id": str(params.get("id") or ""),
                    "text": str(params["display_text"]), "index": card_button_index,
                    "cardIndex": card_index}
        return {"kind": "flow", "id": str(params.get("id") or ""),
                "text": str(params["display_text"])}
    raise ButtonRefused("invalid_request", 400)


def build_reply(plan, ctx):
    """The plan -> whatsmeow `Message`, quoting the message through `ctx`."""
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import (
        ButtonsResponseMessage, InteractiveResponseMessage, ListResponseMessage, Message,
        TemplateButtonReplyMessage)
    if plan["kind"] == "button":
        return Message(buttonsResponseMessage=ButtonsResponseMessage(
            selectedButtonID=plan["id"], selectedDisplayText=plan["text"],
            type=ButtonsResponseMessage.DISPLAY_TEXT, contextInfo=ctx))
    if plan["kind"] == "template":
        return Message(templateButtonReplyMessage=TemplateButtonReplyMessage(
            selectedID=plan["id"], selectedDisplayText=plan["text"],
            selectedIndex=plan["index"], contextInfo=ctx))
    if plan["kind"] == "carousel":
        return Message(templateButtonReplyMessage=TemplateButtonReplyMessage(
            selectedID=plan["id"], selectedDisplayText=plan["text"],
            selectedIndex=plan["index"], selectedCarouselCardIndex=plan["cardIndex"],
            contextInfo=ctx))
    if plan["kind"] == "flow":
        reply = InteractiveResponseMessage(contextInfo=ctx)
        reply.body.text = plan["text"]
        reply.nativeFlowResponseMessage.name = "quick_reply"
        reply.nativeFlowResponseMessage.paramsJSON = json.dumps(
            {"display_text": plan["text"], "id": plan["id"]}, ensure_ascii=False)
        reply.nativeFlowResponseMessage.version = 3
        return Message(interactiveResponseMessage=reply)
    return Message(listResponseMessage=ListResponseMessage(
        title=plan["title"], description=plan["description"],
        listType=ListResponseMessage.SINGLE_SELECT, contextInfo=ctx,
        singleSelectReply=ListResponseMessage.SingleSelectReply(selectedRowID=plan["id"])))
