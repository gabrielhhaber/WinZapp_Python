"""`POST call/add-participants`: someone else into the group call this account
is in, as WhatsApp Web's "Add participant" does. The WPPConnect side calls
WhatsApp Web's own WAWebVoipStartCall.inviteToCall once per person
(client/api_patches/src/util/groupCallRuntime.ts); here meowcaller's
Call.AddParticipant sends the same group invite offer (neonize patch 0014,
`CallClient.add_participant`), and someone already on the call's list who has
not joined is rung again, as the "Ring" button there.

Body `{"callId", "participants": [jid, ...]}`; answer `{"id", "results":
[{"jid", "status": "invited" | "rang" | "failed", "error"?}]}`, one per person
in the order asked. Only into a call this device is in, at most 31 people (a
WhatsApp group call holds 32).

The pure functions are at the top (tested in test_call_invites.py).
"""
import asyncio
import logging
import re

from aiohttp import web

log = logging.getLogger("bridge")

MAX_INVITED = 31
_ADDRESS = re.compile(r"\d{5,20}@(lid|s\.whatsapp\.net|c\.us)")


def target_of(jid) -> str:
    """The address meowcaller takes ('<digits>@lid' or '<digits>@s.whatsapp.net'),
    or '' for anything that is not a person's address."""
    text = str(jid or "").strip()
    if not _ADDRESS.fullmatch(text):
        return ""
    return text.replace("@c.us", "@s.whatsapp.net")


def outcome(jid, answer=None, error=None) -> dict:
    """One row of the answer: what happened when `jid` was asked. A failure
    says only its kind: meowcaller's text can carry the person's number."""
    if error is not None:
        kind = error if isinstance(error, str) else type(error).__name__
        return {"jid": jid, "status": "failed", "error": kind[:60]}
    return {"jid": jid, "status": "rang" if (answer or {}).get("rang") else "invited"}


def register_routes(r, *, session, ok):
    def error(message, status=500):
        return web.json_response({"status": "error", "message": str(message)}, status=status)

    async def add_participants(req):
        s = session(req)
        try:
            body = await req.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        call_id = str(body.get("callId") or "")
        people = body.get("participants")
        if not call_id or len(call_id) > 512 or not isinstance(people, list) or not people:
            return error("Parameters callId and participants are required!", 400)
        if len(people) > MAX_INVITED:
            return error(f"Between 1 and {MAX_INVITED} participants are required", 400)
        media = getattr(s, "call_media", None)
        add = getattr(getattr(media, "calls", None), "add_participant", None)
        if media is None or not callable(add):
            return error("this neonize build does not add people to a call", 501)
        if s.client is None or s.status != "CONNECTED":
            return error("Disconnected", 400)
        if not media.in_call(call_id):
            return error("This device is not in that call", 409)

        def invite_all():
            rows = []
            for jid in people:
                target = target_of(jid)
                if not target:
                    rows.append(outcome(jid, error="invalid address"))
                    continue
                try:
                    rows.append(outcome(jid, add(call_id, target)))
                except Exception as failure:
                    log.info("call add participant refused (%s)", type(failure).__name__)
                    rows.append(outcome(jid, error=failure))
            return rows
        rows = await asyncio.get_running_loop().run_in_executor(None, invite_all)
        return ok(status="success", response={"id": call_id, "results": rows})

    r.add_post("/api/{tok}/call/add-participants", add_participants)
