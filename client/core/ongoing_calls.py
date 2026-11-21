"""Native joinable calls are live state, never inferred from stored history."""
import re

from core.call_matching import is_placeholder_call_id

_CHAT = re.compile(r"\d+(?:-\d+)?@(?:g\.us|lid|s\.whatsapp\.net|c\.us)")


def ongoing_entries(body):
    rows = body.get("response") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        raise ValueError("Invalid ongoing calls response")
    result, seen = [], set()
    for row in rows[:500]:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]
                or not isinstance(row.get("chatJid"), str) or not _CHAT.fullmatch(row["chatJid"])
                or type(row.get("isVideo")) is not bool or row["id"] in seen):
            continue
        seen.add(row["id"])
        # Canonical form, so lock checks and name lookups match the stored
        # chat; the runtime treats both phone forms as the same chat.
        chat = re.sub(r"@c\.us$", "@s.whatsapp.net", row["chatJid"])
        result.append({"jid": chat, "ongoing": {
            "id": row["id"], "chatJid": chat, "isVideo": row["isVideo"]}})
    return result


def include_current_call(entries, active, state):
    """An established local 1:1 call is current even without a native join log."""
    rows = list(entries)
    if not isinstance(active, dict) or state not in ("ACTIVE", "REJOINING"):
        return rows
    call_id = active.get("call_id")
    if not isinstance(call_id, str) or is_placeholder_call_id(call_id):
        return rows
    if any(entry["ongoing"]["id"] == call_id for entry in rows):
        return rows
    current = ongoing_entries({"response": [{"id": call_id,
        "chatJid": active.get("peer_jid"), "isVideo": bool(active.get("is_video"))}]})
    return current + rows


def ongoing_for_chat(entries, jid, same_chat):
    """The call of `ongoing_entries()` filed under the chat `jid` is, as the
    conversation panel shows it ({id, groupJid: jid, chatJid, isVideo}), or None.

    A call with several people started from a one-to-one chat is filed under
    the creator's chat, often by @lid while WinZapp keeps the chat under the
    number: `same_chat(a, b)` bridges the two (seen over CDP on 2026-11-17).
    `chatJid` is the engine's own form, the one call/group/join must be given.
    """
    for entry in entries or ():
        call = entry["ongoing"]
        if same_chat(call["chatJid"], jid):
            return {"id": call["id"], "groupJid": jid, "chatJid": call["chatJid"],
                    "isVideo": call["isVideo"]}
    return None


def chat_call_button(jid, ongoing, active, same_chat):
    """What the conversation of `jid` offers in place of its call buttons:
    "return" while this device is in a call with that chat, "join" while a call
    of that chat goes on without it, "" otherwise (the call buttons stay).

    `ongoing` is the cached discovery for that chat, `active` the call this
    device holds (MainWindow._active_voice_call)."""
    if not jid:
        return ""
    active = active if isinstance(active, dict) else {}
    if active:
        if (same_chat(str(active.get("peer_jid") or ""), jid)
                or (active.get("group_jid") and active.get("group_jid") == jid)
                or (ongoing and active.get("call_id") == ongoing.get("id"))):
            return "return"
    if ongoing and ongoing.get("groupJid") == jid:
        return "join"
    return ""
