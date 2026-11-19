"""Validated group metadata observations and scoped participant deltas."""
from core.remote_message_state import chat_target, chat_aliases


def participant_id(value):
    jid = chat_target(value)
    return jid if jid and not jid.endswith("@g.us") else None


def group_update(data):
    if not isinstance(data, dict):
        return None
    raw = data.get("id") or data.get("remoteJid")
    if isinstance(raw, dict):
        raw = raw.get("_serialized") or raw.get("id")
    jid = chat_target(raw)
    if not jid or not jid.endswith("@g.us"):
        return None
    fields = {}
    for name in ("subject", "desc"):
        value = data.get(name)
        if isinstance(value, str) and len(value) <= 32768 and (name == "desc" or value.strip()):
            fields[name] = value.strip() if name == "subject" else value
    if "subject" not in fields and isinstance(data.get("name"), str) and data["name"].strip():
        fields["subject"] = data["name"].strip()
    for name in ("announce", "restrict"):
        if type(data.get(name)) is bool:
            fields[name] = data[name]
    members = data.get("participants")
    action = data.get("action")
    complete = data.get("participantsComplete") is True
    membership = None
    if isinstance(members, list) and 0 < len(members) <= 2048:
        if complete:
            if all(isinstance(p, dict) and participant_id(p.get("id"))
                   and type(p.get("isAdmin")) is bool and type(p.get("isSuperAdmin")) is bool for p in members):
                membership = ("snapshot", [{"id": participant_id(p["id"]),
                    "isAdmin": p["isAdmin"], "isSuperAdmin": p["isSuperAdmin"]} for p in members])
        elif action in ("add", "remove", "promote", "demote"):
            ids = [participant_id(p.get("id") if isinstance(p, dict) else p) for p in members]
            if all(ids):
                membership = (action, list(dict.fromkeys(ids)))
    return (jid, fields, membership) if fields or membership else None


def merge_participants(mw, current, action, members):
    if action == "snapshot":
        return [dict(p) for p in members]
    result = [dict(p) for p in current if isinstance(p, dict)]
    for jid in members:
        names = chat_aliases(mw, jid)
        matches = [p for p in result if participant_id(p.get("id")) in names]
        if action == "remove":
            result = [p for p in result if p not in matches]
        elif action == "add" and not matches:
            result.append({"id": jid})
        elif action in ("promote", "demote"):
            for p in matches:
                p.update(isAdmin=action == "promote", admin="admin" if action == "promote" else None)
                if action == "demote":
                    p["isSuperAdmin"] = False
    return result
