"""Native joinable calls are live state, never inferred from stored history."""
import re

from core.call_matching import is_placeholder_call_id

_CHAT = re.compile(r"\d+(?:-\d+)?@(?:g\.us|lid|s\.whatsapp\.net|c\.us)")
_PERSON = re.compile(r"\d+@(?:lid|s\.whatsapp\.net|c\.us)")
_PEOPLE_LIMIT = 64


def _canonical(jid):
    return re.sub(r"@c\.us$", "@s.whatsapp.net", jid)


def _people(raw):
    """The `people` of an ongoing call (who is connected to it, this account
    left out), canonical and valid only; [] when the engine sent none."""
    if not isinstance(raw, list):
        return []
    people = []
    for jid in raw[:_PEOPLE_LIMIT]:
        if isinstance(jid, str) and _PERSON.fullmatch(jid) and _canonical(jid) not in people:
            people.append(_canonical(jid))
    return people


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
        chat = _canonical(row["chatJid"])
        ongoing = {"id": row["id"], "chatJid": chat, "isVideo": row["isVideo"]}
        people = _people(row.get("people"))
        if people:
            ongoing["people"] = people
        result.append({"jid": chat, "ongoing": ongoing})
    return result


def is_own_chat_call(ongoing, is_self):
    """True for a call filed under this account's own chat: one it started on
    another device (its phone) with no group. WhatsApp files such a call
    under its creator's chat, which here is the account itself, so it is
    named after and offered in the chats of the people in it instead."""
    return bool(isinstance(ongoing, dict) and ongoing.get("people")
                and not str(ongoing.get("chatJid") or "").endswith("@g.us")
                and is_self(str(ongoing.get("chatJid") or "")))


def ongoing_conversation_jid(entry, is_self):
    """The chat "Open conversation" of a live Calls row goes to: the first
    person in a call filed under this account's own chat, the call's chat
    otherwise."""
    ongoing = entry.get("ongoing") or {}
    if is_own_chat_call(ongoing, is_self):
        return ongoing["people"][0]
    return entry["jid"]


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


def ongoing_for_chat(entries, jid, same_chat, is_self=lambda _jid: False):
    """The call of `ongoing_entries()` filed under the chat `jid` is, as the
    conversation panel shows it ({id, groupJid: jid, chatJid, isVideo}), or None.

    A call with several people started from a one-to-one chat is filed under
    the creator's chat, often by @lid while WinZapp keeps the chat under the
    number: `same_chat(a, b)` bridges the two (seen over CDP on 2026-11-17).
    `chatJid` is the engine's own form, the one call/group/join must be given.

    When this account is the creator (it started the call on its phone), that
    chat is its own: the call is offered in the chat of each person connected
    to it (is_own_chat_call; reported 2026-11-22, the creator who left on the
    phone could not join from WinZapp).
    """
    for entry in entries or ():
        call = entry["ongoing"]
        if same_chat(call["chatJid"], jid) or (
                is_own_chat_call(call, is_self)
                and any(same_chat(person, jid) for person in call["people"])):
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
