"""Route `reply-button/:messageId`: reply to a button of a business message
(WinZapp calls it; see client/core/business_buttons.py). The buttons and the
format of each reply are in buttons.py.

The client reads the error code (`code`) from the body, not the message: 400
invalid_request, 404 message_not_found / button_not_found, 501
reply_button_unavailable (what the bridge does not know how to send).

Without effect until msgmap.build copies `buttons.button_fields` onto the
received messages: without those fields the stored message has no button.
The reply sent is not stored in the store (the client only wants to know whether
it went out). Sent once, never repeated: an error may have reached the business.

Sending buttons and lists is interactive_send.py. Nothing here was tested
against a real account.

The pure functions are at the top (tested in test_chat_interactive.py);
`register_routes` wires the route to the server.
"""
import asyncio
import logging
import re

from aiohttp import web

import buttons

log = logging.getLogger("bridge")

#: "true_<chat>_<ID>" of an individual chat (the same rule as the client's)
_MESSAGE = re.compile(r"(?:true|false)_\d+@(?:c\.us|lid)_[A-Za-z0-9]{8,64}")
MAX_BUTTONS = 32


def valid_request(serial, body):
    """(source, index) of the request, or ButtonRefused(invalid_request)."""
    source = body.get("source") if isinstance(body, dict) else None
    index = body.get("index") if isinstance(body, dict) else None
    if (not isinstance(serial, str) or not _MESSAGE.fullmatch(serial)
            or source not in buttons.SOURCES or isinstance(index, bool)
            or not isinstance(index, int) or not 0 <= index < MAX_BUTTONS):
        raise buttons.ButtonRefused("invalid_request", 400)
    return source, index


def register_routes(r, *, session, ok, destination_of, quoted_context):
    """Wire `reply-button` to the aiohttp router `r`."""

    def refusal(code, status):
        return web.json_response({"status": "error", "code": code}, status=status)

    async def reply_button(req):
        s = session(req)
        serial = req.match_info["messageId"]
        try:
            body = await req.json()
        except ValueError:
            body = None
        try:
            source, index = valid_request(serial, body)
            _, chat, mid, _ = s.id_parts(serial)
            original = s.db.wpp_message(mid)
            if original is None:
                raise buttons.ButtonRefused("message_not_found", 404)
            plan = buttons.reply_plan(original, source, index)
        except buttons.ButtonRefused as e:
            return refusal(e.code, e.status)
        if s.client is None or s.status != "CONNECTED":
            return refusal("reply_button_unavailable", 503)
        ctx = quoted_context(s, serial)
        if ctx is None:
            return refusal("message_not_found", 404)
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, s.client.send_message, destination_of(chat),
                buttons.build_reply(plan, ctx))
        except Exception:
            log.exception("reply-button")
            return refusal("reply_button_unavailable", 503)
        return ok(status="success", response={"sent": True})

    r.add_post("/api/{tok}/reply-button/{messageId}", reply_button)
