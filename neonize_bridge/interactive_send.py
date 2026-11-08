"""Interactive and commerce messages: send-buttons, send-list-message,
send-pix-key, send-order-message and send-link-catalog (WPPConnect's
messageController / catalogController).

Each message is the proto WhatsApp Web builds from what wa-js hands it (read
over CDP on 2026-10-26):

- buttons: an InteractiveMessage whose native flow buttons are quick_reply,
  cta_url, cta_call or cta_copy, each {display_text, ..} as JSON
  (wa-js createNativeFlowButtons, WAWebGenerateInteractiveMessageProto);
- a list: a ListMessage {buttonText, description, title, footerText,
  listType SINGLE_SELECT, sections} (wa-js sendListMessage,
  WAWebGenerateListMessageProto);
- a Pix key: a native flow "payment_info" message whose button carries the
  order with a pix_static_code payment setting (wa-js sendPixKeyMessage,
  WAWebBizSendOrderAction), with a fresh message secret;
- an order (a charge): a native flow "review_and_pay" (order_details)
  message with the order as JSON, amounts {value, offset 1000} (wa-js
  sendChargeMessage);
- the catalog link: text with https://wa.me/c/<my number> and a cta_url
  button "Abrir catálogo" (WPPConnect's sendLinkCatalog).

The <biz> node these need is added by whatsmeow (neonize_patches/whatsmeow
0004-0006) as WPPConnect sends it, so a phone shows the same message:
reply and action buttons as <native_flow v="9" name="mixed"/>, a Pix key as
<native_flow name="payment_info"/>, a list as <list v="2"
type="product_list"/> (wa-js applyNativeFlowBiz and createFanoutMsgStanza,
read 2026-10-26); neonize's own GenerateWABinary used to add a second
<biz>, which the server refused with 479 (smax-invalid), so neonize 0008
leaves it to whatsmeow. A list goes as <message type="text"> to a person and
"media" to a group, as WhatsApp Web's stanza captured on 2026-10-27 (whatsmeow
0007). WPPConnect's own send-buttons sends the InteractiveMessage
unwrapped, the proto built here (checked over CDP without sending), so
wa-js's legacy copy for linked devices (encryptAndParserMsgButtons, only for
a viewOnce message) does not apply to it either. The pure functions are at
the top (tested in test_interactive_send.py).
"""
import asyncio
import base64
import json
import logging
import secrets

from aiohttp import web

log = logging.getLogger("bridge")

OFFSET = 1000
MAX_BUTTONS = 3
MAX_SECTIONS = 10
LIST_SINGLE_SELECT = 1
# The currency of the seller's country, by its phone prefix (wa-js
# currencyForCountryShortcode); the longest prefix wins.
CURRENCIES = {"55": "BRL", "1": "USD", "44": "GBP", "48": "PLN", "40": "RON", "90": "TRY",
              "351": "EUR", "34": "EUR", "33": "EUR", "49": "EUR", "39": "EUR", "52": "MXN",
              "54": "ARS", "57": "COP", "56": "CLP", "51": "PEN", "91": "INR", "62": "IDR"}


class SendRefused(ValueError):
    pass


def order_reference() -> str:
    """A reference id like WhatsApp Web's friendly random ids."""
    return secrets.token_hex(5).upper()


def native_flow_buttons(buttons) -> list:
    """wa-js createNativeFlowButtons: [{name, buttonParamsJSON}]."""
    if not isinstance(buttons, list) or not 1 <= len(buttons) <= MAX_BUTTONS:
        raise SendRefused("buttons must have between 1 and 3 options")
    actions = any(isinstance(b, dict) and ("phoneNumber" in b or "url" in b) for b in buttons)
    replies = any(isinstance(b, dict) and b.get("id") and b.get("text") for b in buttons)
    if actions and replies:
        raise SendRefused("reply buttons and action buttons cannot be sent together")
    out = []
    for index, b in enumerate(buttons):
        if not isinstance(b, dict) or not str(b.get("text") or "").strip():
            raise SendRefused("every button needs a text")
        text = str(b["text"])
        if "phoneNumber" in b:
            out.append(("cta_call", {"display_text": text, "phone_number": str(b["phoneNumber"])}))
        elif "url" in b:
            out.append(("cta_url", {"display_text": text, "url": str(b["url"]),
                                    "merchant_url": str(b["url"])}))
        elif "code" in b:
            out.append(("cta_copy", {"display_text": text, "copy_code": str(b["code"])}))
        else:
            out.append(("quick_reply", {"display_text": text, "id": str(b.get("id") or index)}))
    return [{"name": name, "buttonParamsJSON": json.dumps(params, ensure_ascii=False)}
            for name, params in out]


def interactive(body_text, buttons, footer="", title="") -> dict:
    """An InteractiveMessage (as a Message dict) with native flow buttons."""
    message = {"nativeFlowMessage": {"buttons": buttons, "messageVersion": 1},
               "header": {"title": title or "", "hasMediaAttachment": False}}
    if body_text:
        message["body"] = {"text": body_text}
    if footer:
        message["footer"] = {"text": footer}
    return {"interactiveMessage": message}


def list_message(body) -> dict:
    """A ListMessage (as a Message dict) from send-list-message's body."""
    sections = body.get("sections")
    if not isinstance(sections, list) or not 1 <= len(sections) <= MAX_SECTIONS:
        raise SendRefused("sections must have between 1 and 10 options")
    out = []
    for section in sections:
        rows = section.get("rows") if isinstance(section, dict) else None
        if not isinstance(rows, list) or not rows:
            raise SendRefused("every section needs rows")
        out.append({"title": str(section.get("title") or ""), "rows": [
            {"title": str(row.get("title") or ""), "description": str(row.get("description") or ""),
             "rowID": str(row.get("rowId") or row.get("rowID") or index)}
            for index, row in enumerate(rows) if isinstance(row, dict)]})
    message = {"buttonText": str(body.get("buttonText") or "SELECIONE UMA OPÇÃO"),
               "description": str(body.get("description") or " "),
               "listType": LIST_SINGLE_SELECT, "sections": out}
    for key, name in (("title", "title"), ("footer", "footerText")):
        if body.get(key):
            message[name] = str(body[key])
    return {"listMessage": message}


def pix_params(key_type, name, key, instructions="") -> dict:
    """The order a Pix key message carries (wa-js sendPixKeyMessage)."""
    return {
        "order": {"items": [{"name": "", "retailer_id": f"custom-item-{order_reference()}",
                             "amount": {"offset": 1, "value": 0}, "quantity": 0}],
                  "order_type": "ORDER_WITHOUT_AMOUNT", "status": "payment_requested",
                  "subtotal": {"value": 0, "offset": 1}},
        "total_amount": {"value": 0, "offset": 1}, "reference_id": order_reference(),
        "payment_settings": [
            {"type": "pix_static_code",
             "pix_static_code": {"key_type": key_type, "merchant_name": name, "key": key}},
            {"type": "cards", "cards": {"enabled": False}}],
        "external_payment_configurations": [
            {"payment_instruction": instructions or "", "type": "payment_instruction"}],
        "additional_note": "", "currency": "BRL", "type": "physical-goods"}


def currency_of(phone_user) -> str:
    for size in (3, 2, 1):
        found = CURRENCIES.get(str(phone_user)[:size])
        if found:
            return found
    return "BRL"


def charge_params(items, options, currency) -> dict:
    """The order of send-order-message (wa-js sendChargeMessage). `items`:
    [{name, price (thousandths), quantity, product_id or None}]."""
    rows, subtotal = [], 0
    for item in items:
        retailer = item.get("product_id") or f"custom-item-{order_reference()}"
        rows.append({"retailer_id": retailer, "name": item["name"],
                     "amount": {"value": item["price"], "offset": OFFSET},
                     "quantity": item["quantity"], "isCustomItem": not item.get("product_id"),
                     "isQuantitySet": True})
        subtotal += item["price"] * item["quantity"]
    extra = {k: int(options.get(k) or 0) for k in ("tax", "shipping", "discount")}
    total = subtotal + extra["tax"] + extra["shipping"] - extra["discount"]
    order = {"status": "pending", "items": rows, "subtotal": {"value": subtotal, "offset": OFFSET}}
    for key in ("tax", "shipping", "discount"):
        order[key] = {"value": extra[key], "offset": OFFSET} if extra[key] else None
    params = {"reference_id": order_reference(), "type": "physical-goods",
              "payment_configuration": "merchant_categorization_code", "currency": currency,
              "total_amount": {"value": total, "offset": OFFSET}, "order_type": "ORDER",
              "order": order}
    if options.get("payment_instruction"):
        params["external_payment_configurations"] = [
            {"type": "payment_instruction", "payment_instruction": options["payment_instruction"]}]
    return params


def charge_items(rows, products) -> list:
    """send-order-message's items -> charge items. `products` {id: product}
    of the account's own catalog, for the items given by product id."""
    if not isinstance(rows, list) or not rows:
        raise SendRefused("items is required")
    items = []
    for row in rows:
        if not isinstance(row, dict):
            raise SendRefused("invalid item")
        quantity = int(row.get("qnt") or 1)
        if row.get("type") == "product" or (row.get("id") and not row.get("name")):
            product = products.get(str(row.get("id")))
            if product is None:
                raise SendRefused(f"The product id {row.get('id')} not found")
            price = int(product["price"] or 0)
            items.append({"name": product["name"], "price": price, "quantity": quantity,
                          "product_id": product["id"]})
        else:
            if not str(row.get("name") or "").strip():
                raise SendRefused("every custom item needs a name")
            items.append({"name": str(row["name"]), "price": int(row.get("price") or 0),
                          "quantity": quantity})
    return items


def to_message(fields):
    """A Message proto from a Message dict (proto field names)."""
    from google.protobuf import json_format
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message
    return json_format.ParseDict(fields, Message())


def phones_of(value) -> list:
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return [str(value)] if value else []


def register_routes(r, *, session, destination_of, store_sent, jid_from_text):
    """Wire the routes to the aiohttp router `r`."""
    import commerce_common as cc

    async def execute(func, *args):
        return await asyncio.get_running_loop().run_in_executor(None, func, *args)

    def error(message, status=500):
        return web.json_response({"status": "error", "message": str(message)}, status=status)

    def ready(s):
        return s.client is not None and s.status == "CONNECTED"

    async def body_of(req):
        try:
            body = await req.json()
        except ValueError:
            body = {}
        return body if isinstance(body, dict) else {}

    async def send_each(s, phones, build):
        """Sends `build()` (a Message dict) to each phone; their results."""
        results = []
        for phone in phones:
            destination = destination_of(phone)
            message = to_message(build())
            resp = await execute(s.client.send_message, destination, message)
            results.extend(json.loads(store_sent(s, destination, resp).text)["response"])
        return results

    async def sent(req, label, build, phones=None):
        s, body = session(req), await body_of(req)
        phones = phones_of(body.get("phone")) if phones is None else phones
        if not phones:
            return error("phone is required", 400)
        if not ready(s):
            return error("Disconnected", 400)
        try:
            # an order looks its products up: off the loop
            builder = await execute(build, s, body)
        except SendRefused as e:
            return error(e, 400)
        except Exception:
            log.exception(label)
            return error(f"Error on {label}")
        try:
            results = await send_each(s, phones, builder)
        except Exception:
            log.exception(label)
            return error(f"Error on {label}")
        return web.json_response({"status": "success", "response": results, "mapper": "return"},
                                 status=201)

    async def send_buttons(req):
        def build(s, body):
            options = body.get("options") if isinstance(body.get("options"), dict) else {}
            buttons = native_flow_buttons(options.get("buttons"))
            text = str(body.get("message") or "")
            return lambda: interactive(text, buttons, options.get("footer") or "",
                                       options.get("title") or "")
        return await sent(req, "send buttons", build)

    async def send_list_message(req):
        def build(s, body):
            fields = list_message(body)
            return lambda: fields
        return await sent(req, "send list message", build)

    async def send_pix_key(req):
        def build(s, body):
            if not (body.get("keyType") and body.get("name") and body.get("key")):
                raise SendRefused("Please, send all the required parameters")

            def message():
                params = pix_params(str(body["keyType"]), str(body["name"]), str(body["key"]),
                                    str(body.get("instructions") or ""))
                fields = interactive("", [{"name": "payment_info",
                                           "buttonParamsJSON": json.dumps(params)}])
                fields["messageContextInfo"] = {
                    "messageSecret": base64.b64encode(secrets.token_bytes(32)).decode()}
                return fields
            return message
        return await sent(req, "send pix key", build)

    async def send_order_message(req):
        def build(s, body):
            rows = body.get("items")
            options = body.get("options") if isinstance(body.get("options"), dict) else {}
            ids = [str(r.get("id")) for r in rows or () if isinstance(r, dict)
                   and (r.get("type") == "product" or (r.get("id") and not r.get("name")))]
            me = (s.me or "").split("@")[0].split(":")[0]
            products = cc.products_by_id(s.client, f"{me}@s.whatsapp.net", ids) if ids else {}
            items = charge_items(rows, products)
            currency = options.get("currency") or currency_of(me)
            notes = str(options.get("notes") or "")

            def message():
                params = charge_params(items, options, currency)
                return interactive(notes, [{"name": "review_and_pay",
                                            "buttonParamsJSON": json.dumps(params)}])
            return message
        return await sent(req, "send order message", build)

    async def send_link_catalog(req):
        s, body = session(req), await body_of(req)
        phones = phones_of(body.get("phones"))
        if not phones:
            return web.json_response({"message": "phones was not informed"}, status=401)
        if not ready(s):
            return error("Disconnected", 400)
        me = (s.me or "").split("@")[0].split(":")[0]
        link = f"https://wa.me/c/{me}"
        text = f"{body.get('message') or ''} {link}"
        buttons = native_flow_buttons([{"url": link, "text": "Abrir catálogo"}])
        results = []
        try:
            for phone in phones:
                (result,) = await send_each(s, [phone], lambda: interactive(text, buttons))
                results.append({"phone": phone, "status": result["id"]})
        except Exception:
            log.exception("send-link-catalog")
            return error("Error on send link catalog")
        return web.json_response({"status": "success", "response": results})

    r.add_post("/api/{tok}/send-buttons", send_buttons)
    r.add_post("/api/{tok}/send-list-message", send_list_message)
    r.add_post("/api/{tok}/send-pix-key", send_pix_key)
    r.add_post("/api/{tok}/send-order-message", send_order_message)
    r.add_post("/api/{tok}/send-link-catalog", send_link_catalog)
