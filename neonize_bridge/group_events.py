"""Live group changes: name, description, settings and members.

WhatsApp notifies through two neonize events: `GroupInfoEv` (someone changed
something in a group the account is in: Name, Topic, Locked, Announce, Join,
Leave, Promote, Demote...) and `JoinedGroupEv` (the account joined or created a
group, with the whole `GroupInfo`). Both become the Socket.IO `groups.update`,
which the client reads in core/websocket_client.py (`on_groups_update`):
{"session", "data": [..]}, each item with `id` (or `remoteJid`) and
`subject`/`name`.

Today the client only acts on `id` + `subject` (a new name updates the chat
list). The other fields follow Baileys/WPPConnect's shape (`desc`, `announce`,
`restrict`, `participants` + `action`) and the client ignores them; an item
without `subject` is discarded there, so emitting them is harmless and leaves
the data ready.

The group notices (`gp2`) that WhatsApp Web shows in the chat are built in
system_messages.py and go out through `received-message`.

The pure functions are at the top (tested in test_group_events.py) and do not
import neonize; `register_events` wires the events to a session. The handlers
were NOT proven against a real account.
"""
import logging
import time

import system_messages
import msgmap
from redaction import redact

log = logging.getLogger("bridge")

# GroupInfoEv.<list> -> action of groups.update (Baileys').
ACTIONS = (("Join", "add"), ("Leave", "remove"), ("Promote", "promote"), ("Demote", "demote"))


def _has(msg, field) -> bool:
    """Did the message field (sub-protobuf) come filled in the event?"""
    if msg is None:
        return False
    try:
        return bool(msg.HasField(field))
    except (AttributeError, ValueError):
        return getattr(msg, field, None) is not None


def jid_text(jid) -> str:
    """neonize JID -> 'user@server' ('' if empty)."""
    if jid is None or not getattr(jid, "User", ""):
        return ""
    return f"{jid.User}@{jid.Server}"


def field_update(group_id, name=None, topic=None, locked=None, announcements=None):
    """Name, description and settings -> ONE item of `groups.update` (as Baileys
    joins the fields), or None if nothing changed. `name` is text; `topic` is
    (text, deleted); `locked` = only admins edit the group data
    (`restrict`); `announcements` = only admins send messages (`announce`)."""
    item = {}
    if name is not None and name.strip():
        item["subject"] = name.strip()
    if topic is not None:
        item["desc"] = "" if topic[1] else topic[0]
    if locked is not None:
        item["restrict"] = bool(locked)
    if announcements is not None:
        item["announce"] = bool(announcements)
    if not item:
        return None
    return {"id": group_id, **item}


def member_updates(group_id, lists) -> list:
    """{action: [ids]} -> one item per action with people, in ACTIONS order."""
    return [{"id": group_id, "participants": list(lists[action]), "action": action}
            for _, action in ACTIONS if lists.get(action)]


def event_updates(e, person_id) -> list:
    """GroupInfoEv -> items of `groups.update`. `person_id(jid)` returns the
    client's id for a participant JID (number instead of LID when known)."""
    group_id = msgmap.jid_wpp(jid_text(getattr(e, "JID", None)))
    if not group_id.endswith("@g.us"):
        return []
    name = topic = locked = announcements = None
    if _has(e, "Name"):
        name = e.Name.Name
    if _has(e, "Topic"):
        topic = (e.Topic.Topic, bool(e.Topic.TopicDeleted))
    if _has(e, "Locked"):
        locked = e.Locked.isLocked
    if _has(e, "Announce"):
        announcements = e.Announce.IsAnnounce
    items = []
    fields = field_update(group_id, name, topic, locked, announcements)
    if fields:
        items.append(fields)
    lists = {}
    for list_name, action in ACTIONS:
        lists[action] = [i for i in (person_id(j) for j in getattr(e, list_name, ())) if i]
    return items + member_updates(group_id, lists)


def join_updates(e) -> list:
    """JoinedGroupEv (the account joined/created a group) -> items of `groups.update`
    with the name, the description and the settings of the `GroupInfo`."""
    info = getattr(e, "GroupInfo", None)
    group_id = msgmap.jid_wpp(jid_text(getattr(info, "JID", None)))
    if not group_id.endswith("@g.us"):
        return []
    item = field_update(
        group_id,
        name=info.GroupName.Name if _has(info, "GroupName") else None,
        topic=((info.GroupTopic.Topic, bool(info.GroupTopic.TopicDeleted))
               if _has(info, "GroupTopic") else None),
        locked=info.GroupLocked.isLocked if _has(info, "GroupLocked") else None,
        announcements=info.GroupAnnounce.IsAnnounce if _has(info, "GroupAnnounce") else None)
    return [item] if item else []


def person_id(c, jid):
    """Number before the LID (the app cannot show an LID); without a pair, the LID."""
    text = jid_text(jid)
    if jid is not None and getattr(jid, "Server", "") == "lid":
        try:
            pair = jid_text(c.get_pn_from_lid(jid))
            if pair and not pair.endswith("@lid"):
                text = pair
        except Exception:
            pass
    return msgmap.jid_wpp(text)


def emit_items(s, items):
    if items:
        s.emit("groups.update", {"session": s.name, "data": items})


def group_changed(c, s, e, now=None):
    """GroupInfoEv -> groups.update and the chat's `gp2` messages (joined,
    left, promoted, new name...; see system_messages). What registers the
    GroupInfoEv is server.py (neonize keeps ONE handler per event type: a
    second registration would erase the first), which calls this function.
    Not tested against a real account."""
    try:
        s.roster_changed(e)       # joins and leaves, for the group ack (group_receipts)
    except Exception:
        log.exception("group members")
    try:
        items = event_updates(e, lambda j: person_id(c, j))
        log.info("group %s changed (%d item(s))",
                 redact(jid_text(getattr(e, "JID", None))), len(items))
        emit_items(s, items)
    except Exception:
        log.exception("group changed")
    try:
        now = time.time() if now is None else now
        announcements = system_messages.group_notices(e, lambda j: person_id(c, j), now)
        chat = jid_text(getattr(e, "JID", None))
        for w in announcements:
            system_messages.store_and_emit(s, chat, w, now)
    except Exception:
        log.exception("group notice")


def register_events(cli, s):
    """Wires the neonize client's `cli` JoinedGroupEv to the session `s` (needs
    `s.name` and `s.emit`). Called on neonize's thread. The GroupInfoEv is NOT
    registered here: see group_changed(). Not tested against a real account."""
    import neonize.events as nev

    @cli.event(nev.JoinedGroupEv)
    def _joined_group(c, e):
        try:
            s.roster_register(getattr(e, "GroupInfo", None))   # group_receipts
        except Exception:
            log.exception("group members")
        try:
            items = join_updates(e)
            log.info("group join (%d item(s))", len(items))
            emit_items(s, items)
        except Exception:
            log.exception("group join")
        try:
            me = msgmap.jid_wpp(getattr(s, "me", "") or "")
            notice = system_messages.own_join_notice(e, me, time.time())
            if notice is not None:
                chat = jid_text(getattr(getattr(e, "GroupInfo", None), "JID", None))
                system_messages.store_and_emit(s, chat, notice, time.time())
        except Exception:
            log.exception("own join notice")
