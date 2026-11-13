"""Routes left over after the areas: what neonize cannot do (501, with the
reason), the Meta AI terms (read and accepted with WhatsApp Web's own
queries), the platform of a message, and the wiring of the routes
WinZapp's neonize build made possible (raw queries and app state
mutations, neonize_patches/): custom lists (custom_lists.py), joining and
leaving a community's groups and deactivating it (community_membership.py),
editing and deleting a channel (channel_admin.py), who sees my Status
(status_privacy.py) and removing a contact (contact_removal.py). Each of those
answers 501 itself on an older build.

Still impossible:

- battery level: multi-device does not deliver the phone's battery;
- a screenshot: there is no browser page to capture.

`unhealthy` is left out on purpose: in the reference server it answers 503 and
ends the process (it only serves to test an orchestrator), and the client does not use it.
`profile/:phone` was also left out: it is an obsolete route (`getNumberProfile`,
@deprecated) that the client does not call and whose format is not defined anywhere
it can be checked.

The pure functions are at the top (tested in test_remaining.py); `register_routes` wires
the routes.
"""
import asyncio
import logging

from aiohttp import web

import channel_admin
import chat_archive
import community_membership
import contact_removal
import custom_lists
import maintenance
import raw_queries as rq
import status_privacy

log = logging.getLogger("bridge")

# (method, route, code, reason). The `code` is what the client reads to decide the
# message: "unavailable" is what the reference server uses when there is no
# better answer (communityJoinController.ts).
IMPOSSIBLE = (
    ("GET", "get-battery-level", "battery_unsupported",
     "a linked device does not receive the phone battery level"),
    ("GET", "take-screenshot", "screenshot_unsupported",
     "neonize has no browser page to capture"),
)

# The Meta AI terms notice that WhatsApp Web uses (deviceController.ts,
# META_AI_NOTICE_ID_FALLBACK). Until it is accepted, WhatsApp refuses every
# message to Meta AI (core/meta_ai.py in the client).
META_AI_NOTICE = "20250502"
# The `stage` whatsmeow sends when it accepts a ToS notice (AcceptTOSNotice).
TERMS_STAGE = "5"
# Each tos query waits at most this long for WhatsApp (the raw IQ default is 30 s,
# longer than the client waits for the accept: 20 s). Three stanzas and a read
# must fit in that.
TERMS_TIMEOUT_MS = 4000


def terms_query(notice=META_AI_NOTICE) -> dict:
    """WhatsApp Web's own read of a notice (WAWebTosJob.queryTosState)."""
    return {"namespace": "tos", "type": "get", "timeout_ms": TERMS_TIMEOUT_MS, "content": [
        rq.node("request", None, [rq.node("notice", {"id": notice})])]}


def terms_accept_queries(notice=META_AI_NOTICE) -> list:
    """The stanzas that can record the acceptance, in the order they are tried.

    Measured 2026-11-13 on a real account: the `session_update` stanza alone
    (the last one here) was refused by WhatsApp with 400, so the accept never
    went through. The first is the one whatsmeow's own `AcceptTOSNotice` sends
    (a `notice` with `id` and `stage` straight under the iq, no `request`
    wrapper); the second is the same notice inside the `request` WhatsApp Web
    wraps its queries in. Which of them WhatsApp takes for this notice is
    confirmed only by reading the state again after each, never assumed."""
    return [
        {"namespace": "tos", "type": "set", "timeout_ms": TERMS_TIMEOUT_MS, "content": [
            rq.node("notice", {"id": notice, "stage": TERMS_STAGE})]},
        {"namespace": "tos", "type": "set", "timeout_ms": TERMS_TIMEOUT_MS, "content": [
            rq.node("request", None, [
                rq.node("notice", {"id": notice, "stage": TERMS_STAGE})])]},
        terms_accept_query(notice),
    ]


def terms_accept_query(notice=META_AI_NOTICE) -> dict:
    """What WhatsApp Web sends once its accept button has set the notice
    (TosManager.maybeUpdateServer -> WAWebTosJob.updateTosState), as the first
    version of this bridge wrote it. WhatsApp answered it with 400 on
    2026-11-13, so it is now only the last stanza tried."""
    return {"namespace": "tos", "type": "set", "timeout_ms": TERMS_TIMEOUT_MS, "content": [
        rq.node("request", {"type": "session_update"}, [rq.node("notice", {"id": notice})])]}


def terms_state(response, notice=META_AI_NOTICE) -> str:
    """ACCEPTED / NOT_ACCEPTED from the answer to terms_query(); UNKNOWN when it
    does not name the notice. As WhatsApp Web reads it: a notice is accepted
    unless its `state` is "false"."""
    tos = rq.child(response, "tos") if isinstance(response, dict) else None
    for item in rq.children(tos, "notice"):
        attrs = item.get("attrs") or {}
        if attrs.get("id") == notice:
            return "NOT_ACCEPTED" if attrs.get("state") == "false" else "ACCEPTED"
    return "UNKNOWN"


def message_platform(serial) -> str:
    """'android', 'iphone', 'web' or 'unknown' from the message id, by the same
    rules as wa-js (chat/functions/getPlatformFromMessage.ts, based on
    Baileys): an id longer than 21 characters is android, '3A...' is iphone,
    '3EB0...' is web. `serial` is 'fromMe_chat_id[_participant]' (or just the id)."""
    text = str(serial or "")
    parts = text.split("_")
    short_id = parts[2] if len(parts) >= 3 else text
    if len(short_id) > 21:
        return "android"
    if short_id.startswith("3A"):
        return "iphone"
    if short_id.startswith("3EB0"):
        return "web"
    return "unknown"


def meta_ai_terms_response(state) -> dict:
    """The `response` of meta-ai-terms: `state` in capitals, as the client reads it."""
    return {"noticeId": META_AI_NOTICE, "state": state}


def register_routes(r, *, session, ok, jid_from_text, jid_text, destination_of=None,
                    store_sent=None, send_media=None):
    """Wire the remaining routes to the aiohttp router `r`."""
    maintenance.register_routes(r, session=session, ok=ok, destination_of=destination_of,
                                store_sent=store_sent, send_media=send_media)
    community_membership.register_routes(r, session=session)
    channel_admin.register_routes(r, session=session)
    status_privacy.register_routes(r, session=session)
    chat_archive.register_routes(r, session=session)
    contact_removal.register_routes(r, session=session, jid_from_text=jid_from_text)
    custom_lists.register_routes(r, session=session, jid_from_text=jid_from_text,
                                 jid_text=jid_text)

    def error(code, status, message=""):
        return web.json_response(
            {"status": "error", "code": code, "message": message}, status=status)

    def impossible(code, reason):
        async def handle(req):
            return error(code, 501, reason)
        return handle

    def _can_ask(s):
        return s.client is not None and s.status == "CONNECTED" and rq.available(s.client)

    def _ask_state(s):
        return terms_state(rq.send_iq(s.client, terms_query()))

    async def meta_ai_terms(req):
        # "UNKNOWN" when it cannot be read: the client sends anyway, as before.
        s = session(req)
        state = "UNKNOWN"
        if _can_ask(s):
            try:
                state = await asyncio.get_running_loop().run_in_executor(None, _ask_state, s)
            except rq.QueryError as refusal:
                log.info("meta-ai-terms not read (%s)", refusal.code)
        return ok(status="success", response=meta_ai_terms_response(state))

    async def accept_meta_ai_terms(req):
        """Only after the user ticked the box in WinZapp's dialog. Answers success
        only when WhatsApp, asked again, says the notice is accepted."""
        s = session(req)
        if s.client is None or s.status != "CONNECTED":
            return error("disconnected", 503, "the session is not connected")
        if not rq.available(s.client):
            return error("meta_ai_terms_unsupported", 501,
                         "this neonize build sends no raw queries")

        def accept():
            """Try each stanza until WhatsApp, read again, says accepted. A 400 or no answer
            in time (code 0) moves on to the next stanza; any other refusal stops, and the
            last refusal is raised if none worked."""
            state, refusal = "UNKNOWN", None
            for index, query in enumerate(terms_accept_queries()):
                try:
                    rq.send_iq(s.client, query)
                except rq.QueryError as error:
                    log.info("meta-ai-terms accept stanza %d refused (%s): %s",
                             index, error.code, error)
                    if error.code not in (0, 400):
                        raise
                    refusal = error
                    continue
                state = _ask_state(s)
                log.info("meta-ai-terms accept stanza %d sent -> %s", index, state)
                if state == "ACCEPTED":
                    return state
            if refusal is not None and state != "NOT_ACCEPTED":
                raise refusal
            return state

        try:
            state = await asyncio.get_running_loop().run_in_executor(None, accept)
        except rq.QueryError as refusal:
            log.info("meta-ai-terms accept refused (%s)", refusal.code)
            return error("meta_ai_terms_refused", 502, str(refusal))
        log.info("meta-ai-terms accept -> %s", state)
        if state == "ACCEPTED":
            return ok(status="success", response=meta_ai_terms_response(state))
        return web.json_response(
            {"status": "error", "response": meta_ai_terms_response(state)}, status=409)

    async def platform_from_message(req):
        # Like the reference server: the body is the word itself, in JSON.
        return web.json_response(message_platform(req.match_info["messageId"]))

    r.add_get("/api/{tok}/meta-ai-terms", meta_ai_terms)
    r.add_post("/api/{tok}/meta-ai-terms/accept", accept_meta_ai_terms)
    r.add_get("/api/{tok}/get-platform-from-message/{messageId}", platform_from_message)
    for method, route, code, reason in IMPOSSIBLE:
        r.add_route(method, "/api/{tok}/" + route, impossible(code, reason))
