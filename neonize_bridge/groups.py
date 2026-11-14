"""Group routes of the bridge: create, participants, description, invitation, join
by code and common groups. The response formats are the ones the WinZapp client
reads (core/group_operations.py, group_join.py, group_invitation.py).

The pure functions are at the top (tested in test_groups.py); `register_routes`
wires the routes to the server.
"""
import asyncio
import logging
import uuid

from aiohttp import web

log = logging.getLogger("bridge")

CODE_OK = 200
CODE_REFUSED = 403


def user_of(text: str) -> str:
    """'5511999@c.us' / '5511999:12@s.whatsapp.net' / '5511999' -> '5511999'."""
    return str(text).split("@")[0].split(":")[0]


def number_forms(digits: str) -> list:
    """The number and, for a Brazilian mobile, also its other form (with or
    without the 9th digit): WhatsApp only knows one of the two."""
    forms = [digits]
    if digits.startswith("55"):
        if len(digits) == 13 and digits[4] == "9":
            forms.append(digits[:4] + digits[5:])
        elif len(digits) == 12 and digits[4] in "6789":
            forms.append(digits[:4] + "9" + digits[4:])
    return forms


def requested_users(text) -> list:
    """The requested digits and, for a Brazilian number, its other form (with or
    without the 9th digit). A `@lid` only counts whole: its digits are not a
    phone."""
    user = user_of(text)
    if str(text).endswith("@lid") or not user.isdigit():
        return [user]
    return number_forms(user)


def participants_result(requests, participants) -> dict:
    """{request: {"code": ...}} in WPPConnect's format, one item per person
    requested (under the key the client sent).

    `participants` are the GroupParticipant returned by neonize. Whoever did not
    come back in the list was not accepted (403). Error 0 is 200; a 403 with an
    invitation carries the invitation code, which the client offers to resend.
    """
    by_user = {}
    for p in participants:
        for u in (p.JID.User, p.LID.User, p.PhoneNumber.User):
            if u:
                by_user[u] = p
    out = {}
    for request in requests:
        p = next((by_user[u] for u in requested_users(request) if u in by_user), None)
        if p is None:
            out[request] = {"code": CODE_REFUSED}
            continue
        code = int(p.Error) or CODE_OK
        info = {"code": code}
        if code == CODE_REFUSED and p.AddRequest.Code:
            info["invite_code"] = p.AddRequest.Code
            info["invite_code_exp"] = int(p.AddRequest.Expiration or 0)
        out[request] = info
    return out


# whatsmeow error texts (errors of group.go) and the server's IQ. Matching a
# loose piece of word ("request", "participant") confused a "bad-request" with
# "request sent": only what is known to mean that goes in here.
_ENTRY_PENDING = ("membership_approval_request", "membership approval",
                  "pending approval", "approval required")
_ENTRY_ALREADY_MEMBER = ("already a participant", "already in that group",
                         "already in the group", "returned status 409")
_ENTRY_BAD_LINK = ("invite link is not valid", "invite link has been revoked")


def entry_result(error: Exception) -> str:
    """What an error of `join_group_with_link` means: 'pending' (an administrator
    has to approve), 'member' (the account is already in the group), 'link'
    (invalid or revoked invitation) or 'failure' (anything else: never becomes
    success)."""
    text = str(error).lower()
    for result, marks in (("pending", _ENTRY_PENDING),
                          ("member", _ENTRY_ALREADY_MEMBER),
                          ("link", _ENTRY_BAD_LINK)):
        if any(m in text for m in marks):
            return result
    return "failure"


def request_to_wpp(request, phone_of_lid="", name="") -> dict:
    """neonize GroupParticipantRequest -> a row of
    `group-membership-requests` as the client reads it: {id, phone, name, method, t}.
    The id is the person's (number@c.us or number@lid); `phone_of_lid` is the
    number behind a @lid, when known."""
    jid = request.Participant
    server = "lid" if jid.Server == "lid" else "c.us"
    row = {"id": "%s@%s" % (user_of(jid.User), server), "method": "",
           "t": int(request.TimeAt or 0)}
    if server == "lid" and phone_of_lid:
        row["phone"] = "%s@c.us" % user_of(phone_of_lid)
    if name:
        row["name"] = name
    return row


def info_refusal(error: Exception) -> str:
    """The definite answer in a failed group-info, or "" for anything else (a
    timeout, a dropped connection): "not_in_group" (whatsmeow's ErrNotInGroup,
    403) or "group_not_found" (ErrGroupNotFound, 404). The client stops asking
    again, for the rest of the run, only after one of these."""
    text = str(error).lower()
    if "not participating in that group" in text:
        return "not_in_group"
    if "group does not exist" in text:
        return "group_not_found"
    return ""


def not_administrator(error: Exception) -> bool:
    text = str(error).lower()
    return "not-authorized" in text or "forbidden" in text or "403" in text


def common_users(groups, searched) -> list:
    """The ids of the groups in which any of the searched people takes part."""
    matches = []
    for g in groups:
        for p in g.Participants:
            if searched & {p.JID.User, p.LID.User, p.PhoneNumber.User}:
                matches.append(g.JID)
                break
    return matches


def invite_info_query(code: str) -> dict:
    """The invitation preview whatsmeow sends (GetGroupInfoFromLink), as a raw
    query: its answer says whether joining needs an admin's approval, which
    neonize's GroupInfo leaves out."""
    import raw_queries as rq
    return {"namespace": "w:g2", "type": "get", "to": "g.us",
            "content": [rq.node("invite", {"code": code})]}


def invite_approval(answer) -> "bool | None":
    """From the invitation preview's answer: True when joining needs an
    admin's approval, False when not, None when the answer says nothing.
    `membership_approval_mode/group_join state="on"` on a group; absent, off
    (whatsmeow reads it the same way). A community says it in its `parent`
    node instead (WhatsApp Web's isParentGroupClosed)."""
    import raw_queries as rq
    group = rq.child(answer, "group")
    if group is None:
        return None
    mode = rq.child(group, "membership_approval_mode")
    if mode is not None:
        return rq.attr(rq.child(mode, "group_join"), "state") == "on"
    parent = rq.child(group, "parent")
    if parent is not None:
        return rq.attr(parent, "default_membership_approval_mode") == "request_required"
    return False


def register_routes(r, *, session, ok, jid_from_text, jid_text, qr_data_url):
    """Wire the group routes to the aiohttp router `r`."""
    from neonize.utils.enum import ParticipantChange

    async def execute(func, *args):
        return await asyncio.get_running_loop().run_in_executor(None, func, *args)

    def error(message, status=500):
        return web.json_response({"status": "error", "message": str(message)}, status=status)

    def connected(s):
        return s.client is not None and s.status == "CONNECTED"

    async def create_group(req):
        s = session(req)
        body = await req.json()
        if not connected(s):
            return error("Disconnected", 400)
        requests = list(body.get("participants") or [])
        try:
            info = await execute(
                s.client.create_group, body["name"], [jid_from_text(x) for x in requests])
        except Exception as e:
            log.exception("create-group")
            return error(e)
        participants = participants_result(requests, info.Participants)
        jid = jid_text(info.JID)
        return ok(status="success", response={
            "gid": jid, "groupInfo": [{"id": jid, "participants": participants}]})

    async def change_participants(req, action):
        s = session(req)
        body = await req.json()
        if not connected(s):
            return error("Disconnected", 400)
        requests = list(body.get("phone") or [])
        try:
            response = await execute(
                s.client.update_group_participants, jid_from_text(body["groupId"]),
                [jid_from_text(x) for x in requests], action)
        except Exception as e:
            log.exception("group participants")
            return error(e)
        return ok(status="success", response={
            "result": [participants_result(requests, response)]})

    async def add_participant(req):
        return await change_participants(req, ParticipantChange.ADD)

    async def remove_participant(req):
        return await change_participants(req, ParticipantChange.REMOVE)

    async def promote_participant(req):
        return await change_participants(req, ParticipantChange.PROMOTE)

    async def demote_participant(req):
        return await change_participants(req, ParticipantChange.DEMOTE)

    async def group_description(req):
        s = session(req)
        body = await req.json()
        if not connected(s):
            return error("Disconnected", 400)
        jid = jid_from_text(body["groupId"])
        try:
            info = await execute(s.client.get_group_info, jid)
            await execute(s.client.set_group_topic, jid, info.GroupTopic.TopicID,
                          uuid.uuid4().hex.upper(), body.get("description", ""))
        except Exception as e:
            log.exception("group-description")
            return error(e)
        return ok(status="success", response={})

    async def group_info_from_invite_link(req):
        s = session(req)
        body = await req.json()
        if not connected(s):
            return error("Disconnected", 400)
        try:
            info = await execute(s.client.get_group_info_from_link, body["invitecode"])
        except Exception as e:
            log.info("group-info-from-invite-link: %s", type(e).__name__)
            return error(e, 404)
        data = {"id": jid_text(info.JID), "subject": info.GroupName.Name,
                "desc": info.GroupTopic.Topic}
        if info.Participants:
            data["size"] = len(info.Participants)
        if info.GroupParent.IsParent:       # a community, as WhatsApp Web says it
            data["isParentGroup"] = True
            data["isParentGroupClosed"] = (
                info.GroupParent.DefaultMembershipApprovalMode == "request_required")
        # whether joining needs approval: neonize's GroupInfo leaves it out,
        # so the same preview is read raw (the route used to say "unknown")
        import raw_queries as rq
        if rq.available(s.client):
            try:
                answer = await execute(rq.send_iq, s.client, invite_info_query(body["invitecode"]))
                approval = invite_approval(answer)
                if approval is not None:
                    data["membershipApprovalMode"] = approval
            except rq.QueryError as e:
                log.info("invite approval: %s", type(e).__name__)
        return ok(status="success", response=data)

    async def join_code(req):
        s = session(req)
        body = await req.json()
        if not connected(s):
            return error("Disconnected", 400)
        code = body["inviteCode"]
        try:
            preview = await execute(s.client.get_group_info_from_link, code)
        except Exception as e:
            return error(e, 404)
        jid = jid_text(preview.JID)
        pending = False
        try:
            await execute(s.client.join_group_with_link, code)
        except Exception as e:
            result = entry_result(e)
            if result == "member":
                return error("already a participant", 409)
            if result == "link":
                return error("invalid or revoked invite link", 404)
            if result != "pending":
                log.warning("join-code: %s", type(e).__name__)
                return error("could not join the group")
            pending = True
        return ok(status="success", response={"id": jid, "pendingApproval": pending})

    async def invitation(req, revoke):
        s = session(req)
        if not connected(s):
            return error("Disconnected", 400)
        try:
            link = await execute(s.client.get_group_invite_link,
                                 jid_from_text(req.match_info["groupId"]), revoke)
        except Exception as e:
            log.exception("group-invitation")
            return error(e)
        return ok(status="success", response={"link": link, "qr": qr_data_url(link)})

    async def group_invitation(req):
        return await invitation(req, False)

    async def group_invitation_reset(req):
        return await invitation(req, True)

    async def common_groups(req):
        s = session(req)
        if not connected(s):
            return error("Disconnected", 400)
        wid = req.match_info["wid"]
        searched = set(requested_users(wid))
        if wid.endswith("@lid"):
            try:
                pn = await execute(s.client.get_pn_from_lid, jid_from_text(wid))
                searched.update(requested_users(pn.User))
            except Exception:
                pass
        try:
            groups = await execute(s.client.get_joined_groups)
        except Exception as e:
            log.exception("common-groups")
            return error(e)
        matches = common_users(groups, searched - {""})
        return ok(status="success", response=[jid_text(j) for j in matches])

    async def list_requests(req):
        s = session(req)
        if not connected(s):
            return error("Disconnected", 400)
        jid = jid_from_text(req.match_info["groupId"])
        try:
            requests = await execute(s.client.get_group_request_participants, jid)
        except Exception as e:
            if not_administrator(e):
                return error("not an administrator", 403)
            log.exception("group-membership-requests")
            return error(e)
        rows = []
        for request in requests:
            phone = ""
            if request.Participant.Server == "lid":
                try:
                    phone = (await execute(s.client.get_pn_from_lid,
                                           request.Participant)).User
                except Exception:
                    phone = ""
            name = ""
            try:
                found = s.db.contact_of(jid_text(request.Participant), phone)
                name = found.get("name") or found.get("pushname") or ""
            except Exception:
                pass
            rows.append(request_to_wpp(request, phone, name))
        return ok(status="success", response={"requests": rows, "refreshed": True})

    async def answer_requests(req, approve):
        s = session(req)
        body = await req.json()
        if not connected(s):
            return error("Disconnected", 400)
        if not hasattr(s.client, "update_group_request_participants"):
            return error("This neonize build cannot answer join requests yet "
                         "(it needs the 0.5.2-winzapp-teste.2 build).", 501)
        from neonize.utils.enum import ParticipantRequestChange
        action = (ParticipantRequestChange.APPROVE if approve
                  else ParticipantRequestChange.REJECT)
        try:
            await execute(s.client.update_group_request_participants,
                          jid_from_text(req.match_info["groupId"]),
                          [jid_from_text(x) for x in body.get("participants") or []], action)
        except Exception as e:
            if not_administrator(e):
                return error("not an administrator", 403)
            log.exception("group-membership-requests answer")
            return error(e)
        return ok(status="success", response={})

    async def approve_requests(req):
        return await answer_requests(req, True)

    async def refuse_requests(req):
        return await answer_requests(req, False)

    r.add_post("/api/{tok}/create-group", create_group)
    r.add_post("/api/{tok}/add-participant-group", add_participant)
    r.add_post("/api/{tok}/remove-participant-group", remove_participant)
    r.add_post("/api/{tok}/promote-participant-group", promote_participant)
    r.add_post("/api/{tok}/demote-participant-group", demote_participant)
    r.add_post("/api/{tok}/group-description", group_description)
    r.add_post("/api/{tok}/group-info-from-invite-link", group_info_from_invite_link)
    r.add_post("/api/{tok}/join-code", join_code)
    r.add_get("/api/{tok}/group-invitation/{groupId}", group_invitation)
    r.add_post("/api/{tok}/group-invitation/{groupId}/reset", group_invitation_reset)
    r.add_get("/api/{tok}/common-groups/{wid}", common_groups)
    r.add_get("/api/{tok}/group-membership-requests/{groupId}", list_requests)
    r.add_post("/api/{tok}/group-membership-requests/{groupId}/approve", approve_requests)
    r.add_post("/api/{tok}/group-membership-requests/{groupId}/reject", refuse_requests)
