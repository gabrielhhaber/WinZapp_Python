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
import time

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

# The Meta AI terms notice: WhatsApp Web's master bot notice
# (WAWebBotGating.getMasterBotNoticeId(), and wppconnect's deviceController.ts
# META_AI_NOTICE_ID_FALLBACK). Until it is accepted, WhatsApp refuses every
# message to Meta AI (core/meta_ai.py in the client).
META_AI_NOTICE = "20250502"
# The disclosure stages WhatsApp Web sends for it (WAWebPDFNTypes.DISCLOSURE_STAGE):
# SHOWN_0 when its dialog appears, ACCEPTED when the user agrees (the notice's
# content, www.whatsapp.com/user-notice/v1/?id=20250502, names the button
# "ACCEPT"; a button named "OK" would send OK, 155).
TERMS_STAGE_SHOWN = "105"
TERMS_STAGE_ACCEPTED = "5"
# Each tos query waits at most this long for WhatsApp (the raw IQ default is 30 s,
# longer than the client waits for the accept: 20 s). Five queries and the
# re-read delay must fit in that.
TERMS_TIMEOUT_MS = 3000
# A stanza WhatsApp took is read again after this delay when the first read still
# says not accepted.
TERMS_REREAD_DELAY_SECONDS = 1.5


def terms_query(notice=META_AI_NOTICE) -> dict:
    """WhatsApp Web's own read of a notice (WAWebTosJob.queryTosState)."""
    return {"namespace": "tos", "type": "get", "timeout_ms": TERMS_TIMEOUT_MS, "content": [
        rq.node("request", None, [rq.node("notice", {"id": notice})])]}


def terms_stage_query(stage, notice=META_AI_NOTICE) -> dict:
    """WhatsApp Web's record of a disclosure stage for the notice
    (WASmaxUserNoticeSetResultRPC, sent by updateUserDisclosureStateAction from
    the Meta AI terms dialog, WAWebPDFNModal):
    `<iq xmlns="tos" type="set"><trackable id="20250502" result="5"/></iq>`.

    Read from WhatsApp Web's own code over CDP on 2026-11-13. The Meta AI notice
    is a privacy disclosure (PDFN), not a legacy ToS notice: the formats tried
    before (`session_update`, `notice` with `stage` or `state`) are the other
    system's and got 400, an answer that changed nothing, or no answer at all.
    On an account that accepted it, the read (terms_query) answers the notice
    without state="false", so the read confirms this stanza."""
    return {"namespace": "tos", "type": "set", "timeout_ms": TERMS_TIMEOUT_MS, "content": [
        rq.node("trackable", {"id": notice, "result": stage})]}


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


def terms_notices(response) -> list:
    """What the answer to terms_query() names, for the log: the attributes of every
    notice (ids, state, stage - no personal data), or the tags it holds instead."""
    tos = rq.child(response, "tos") if isinstance(response, dict) else None
    if tos is None:
        content = response.get("content") if isinstance(response, dict) else None
        return ["no tos node; holds " + ",".join(
            str(c.get("tag")) for c in content if isinstance(c, dict))
            if isinstance(content, list) else "no tos node"]
    return [dict(item.get("attrs") or {}) for item in rq.children(tos, "notice")]


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

    def _ask_state(s, when=""):
        answer = rq.send_iq(s.client, terms_query())
        state = terms_state(answer)
        log.info("meta-ai-terms read%s: %s -> %s", when, terms_notices(answer), state)
        return state

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
            """WhatsApp Web's sequence: the dialog was shown (SHOWN_0, as its
            dialog sends when it appears; a refusal only logged), then accepted,
            then read again. A refusal of the accept is raised; a read that fails
            after it gives UNKNOWN (not a success, but not a refused accept)."""
            def read_after(when):
                try:
                    return _ask_state(s, when)
                except rq.QueryError as refusal:
                    log.info("meta-ai-terms read%s failed (%s): %s", when, refusal.code, refusal)
                    return "UNKNOWN"

            try:
                if _ask_state(s, " before") == "ACCEPTED":
                    return "ACCEPTED"
            except rq.QueryError as refusal:
                log.info("meta-ai-terms read before refused (%s): %s", refusal.code, refusal)
            try:
                rq.send_iq(s.client, terms_stage_query(TERMS_STAGE_SHOWN))
            except rq.QueryError as refusal:
                log.info("meta-ai-terms shown refused (%s): %s", refusal.code, refusal)
            rq.send_iq(s.client, terms_stage_query(TERMS_STAGE_ACCEPTED))
            log.info("meta-ai-terms accept sent")
            state = read_after(" after accept")
            if state != "ACCEPTED":
                # The server may take a moment to show what it has just taken.
                time.sleep(TERMS_REREAD_DELAY_SECONDS)
                state = read_after(" again after accept")
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
