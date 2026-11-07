"""System messages of the chat: group notices (`gp2`) and call records
(`call_log`).

WhatsApp Web keeps both as chat messages and WPPConnect delivers them through
`received-message`; whatsmeow delivers them as events (`GroupInfoEv`,
`CallTerminateEv`), not as a message. Here the event becomes the message in
WPPConnect's format, which the client reads in core/websocket_client.py
(`_normalize_wpp_message`, branches `gp2` and `call_log`):

- `gp2`: `subtype` (add, leave, remove, promote, demote, subject, description,
  restrict, announce, ephemeral), `recipients` (ids of those affected), `author`
  (who did it), `body` (new name/description, or "on"/"off" in the settings) and
  `value` (boolean of the settings). The text of the notice is built by the client.
- `call_log`: `callOutcome`/`finalCallOutcome` (WhatsApp Web's `CallOutcome`
  enum), `isVideoCall`, `callDuration`, `callCreator`. The message id is the
  call id: the client rereads it through `message-by-id` with that id
  (core/call_log.py `call_log_candidate_ids`).

The id is deterministic (hash of the event) so the same event delivered twice
does not become two messages. Neither counts as unread. A call record raises
the chat's date (`on_arrival` as if it were mine: no unread), as a call does
in WhatsApp; a group notice does not, since the client never lets one move a
chat (`_counts_as_last_message`): someone leaving a community used to lift
every one of its groups to the top of the list, notices that arrived late
included. `store.lower_dates_raised_by_notices` puts back the chats a notice
already lifted.

The pure functions are at the top (test_system_messages.py) and do not import
neonize. Not proven against a real account.
"""
import hashlib
import logging

import msgmap
from redaction import redact

log = logging.getLogger("bridge")

# A notice older than this (event queue delivered on reconnect) goes to the
# client marked as history: it stores it in the right order, without treating
# it as new (websocket_client.on_messages_upsert, `isMdHistoryMsg`).
LIVE_WINDOW = 60


def _has(msg, field) -> bool:
    """Did the message field (sub-protobuf) come filled in the event?"""
    if msg is None:
        return False
    try:
        return bool(msg.HasField(field))
    except (AttributeError, ValueError):
        return getattr(msg, field, None) is not None


def _text(jid) -> str:
    if jid is None or not getattr(jid, "User", ""):
        return ""
    return f"{jid.User}@{jid.Server}"


def notice_id(*parts) -> str:
    """Stable id of a notice: the same event always gives the same id. Without '_'
    (the client splits the serialized id by it) and looking like a message id."""
    raw = "\x1f".join(str(p) for p in parts)
    return "WZ" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:22].upper()


def _base(chat_w, msg_id, ts, kind, author="", from_me=False) -> dict:
    serial = f"{'true' if from_me else 'false'}_{chat_w}_{msg_id}"
    if author and chat_w.endswith("@g.us"):
        serial += f"_{author}"       # as msgmap.build does in group messages
    return {
        "id": {"fromMe": from_me, "remote": chat_w, "id": msg_id, "_serialized": serial},
        "fromMe": from_me, "from": chat_w, "to": chat_w,
        "t": ts, "timestamp": ts, "isStatus": False, "notifyName": "",
        "type": kind, "body": "",
    }


# ------------------------------------------------------------- group notices
def group_notices(e, person_id, now) -> list:
    """GroupInfoEv -> one `gp2` message per change (name, description, settings
    and each member list). `person_id(jid)` returns the client's id of a
    participant JID (the number instead of the LID when known)."""
    group = msgmap.jid_wpp(_text(getattr(e, "JID", None)))
    if not group.endswith("@g.us"):
        return []
    ts = msgmap.seconds(getattr(e, "Timestamp", 0)) or int(now)
    author = person_id(getattr(e, "Sender", None)) or ""
    notices = []

    def notice(subtype, who=(), body="", value=None, notice_author=None):
        who = list(who)
        responsible = author if notice_author is None else notice_author
        w = _base(group, notice_id(group, ts, subtype, responsible, sorted(who), body),
                  ts, "gp2", responsible)
        w.update(subtype=subtype, recipients=who, body=body)
        if responsible:
            w["author"] = responsible
        if value is not None:
            w["value"] = value
        notices.append(w)

    def setting(subtype, on):
        notice(subtype, body="on" if on else "off", value=bool(on))

    if _has(e, "Name") and e.Name.Name.strip():
        notice("subject", body=e.Name.Name.strip())
    if _has(e, "Topic"):
        notice("description", body="" if e.Topic.TopicDeleted else e.Topic.Topic)
    if _has(e, "Locked"):
        setting("restrict", e.Locked.isLocked)       # only admins edit the group data
    if _has(e, "Announce"):
        setting("announce", e.Announce.IsAnnounce)   # only admins send messages
    if _has(e, "Ephemeral"):
        setting("ephemeral", e.Ephemeral.IsEphemeral)

    def people(field_name):
        return [i for i in (person_id(j) for j in getattr(e, field_name, ())) if i]

    joined, left = people("Join"), people("Leave")
    if joined:
        # joined by link/invite: nobody added them (the client reads "joined")
        notice("add", joined, notice_author="" if getattr(e, "JoinReason", "") else None)
    if left:
        if author and author not in left:
            notice("remove", left)
        else:
            notice("leave", left, notice_author="")
    if people("Promote"):
        notice("promote", people("Promote"))
    if people("Demote"):
        notice("demote", people("Demote"))
    return notices


# ------------------------------------------------------------ call record
# state of the `end_event` (calls.py) -> WhatsApp Web's CallOutcome
OUTCOMES = {"NOT_ANSWERED": "Missed", "REJECTED": "Rejected",
            "HANDLED_REMOTELY": "AcceptedElsewhere"}


def call_outcome(state, was_ringing):
    """The `callOutcome` of a received call that ended, or None if it cannot be
    told. An end with no reason ("ENDED") of a call that was still ringing here
    is the author hanging up before anyone answered ("Canceled", which the
    client counts as missed, like WhatsApp itself). When there was a
    CallAccept, whoever calls this (calls.refine_end) already swapped the state
    for HANDLED_REMOTELY, so this "Canceled" is only what is left with no sign
    that anyone answered. Without having seen the offer it is not known whether
    anyone answered, and nothing is recorded."""
    if state in OUTCOMES:
        return OUTCOMES[state]
    return "Canceled" if (state == "ENDED" and was_ringing) else None


def call_record(*, call_id, chat_w, creator_w, outcome, video, ts, duration=0,
                from_me=False) -> dict:
    """`call_log` message (id = the call's id). `chat_w` is the peer in an
    ordinary call and the group in a group one; `creator_w` is who called;
    `from_me` is a call this account placed."""
    w = _base(chat_w, call_id, ts, "call_log", creator_w, from_me)
    w.update(callOutcome=outcome, finalCallOutcome=outcome, isVideoCall=bool(video),
             callDuration=int(duration or 0), callCreator=creator_w)
    return w


# ------------------------------------------------------------ store and emit
def store_and_emit(s, chat, w, now) -> bool:
    """Stores `w` in the chat `chat` (the store's key, in whatsmeow's format)
    and emits it as server.py's `_message` does. False if the id was already
    stored (repeated event): nothing is stored nor emitted again."""
    msg_id = w["id"]["id"]
    if not msg_id or s.db.wpp_message(msg_id) is not None:
        return False
    s.db.message(chat, w)
    if w.get("type") == "call_log":
        s.db.on_arrival(chat, w["t"], True)  # raises the date, adds no unread
    else:
        s.db.chat(chat)                      # the chat exists; its date stays
    out = dict(w)
    if now - w["t"] > LIVE_WINDOW:
        out["isMdHistoryMsg"] = True
    s.emit("received-message", {"session": s.name, "response": out})
    log.info("system message %s in chat %s", w["type"], redact(chat))
    return True
