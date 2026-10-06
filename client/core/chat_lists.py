"""Account list snapshots and membership rules; no UI, network or persistence."""

from dataclasses import dataclass
import re


_LIST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_CHAT_ID = re.compile(r"\d+(?:-\d+)?(?::\d+)?@(c\.us|s\.whatsapp\.net|lid|g\.us)\Z")


@dataclass(frozen=True)
class WhatsAppList:
    id: str
    name: str
    members: frozenset[str]


@dataclass(frozen=True)
class ListSnapshot:
    lists: tuple[WhatsAppList, ...] = ()
    can_edit: bool = False

    def find(self, list_id):
        return next((item for item in self.lists if item.id == list_id), None)


def valid_chat_id(value):
    return isinstance(value, str) and bool(_CHAT_ID.fullmatch(value))


def normalize_list_jid(jid):
    """Match companion/legacy JIDs without treating an LID as a phone."""
    if not valid_chat_id(jid):
        return ""
    user, server = jid.split("@")
    user = user.split(":")[0]
    return f"{user}@{'s.whatsapp.net' if server == 'c.us' else server}"


def list_identity(jid, lid_to_phone):
    normalized = normalize_list_jid(jid)
    # Only explicit mappings bridge LIDs. Group digits are never phone digits.
    if normalized.endswith("@lid"):
        phone = normalize_list_jid(lid_to_phone.get(normalized, ""))
        if phone.endswith("@s.whatsapp.net"):
            return phone
    return normalized


def list_contains(item, jid, lid_to_phone):
    identity = list_identity(jid, lid_to_phone)
    return bool(identity) and any(
        list_identity(member, lid_to_phone) == identity for member in item.members
    )


def parse_list_snapshot(body):
    """Reject partial/malformed snapshots instead of inventing empty lists."""
    if not isinstance(body, dict) or body.get("status") != "success":
        raise ValueError("list_response_invalid")
    data = body.get("response")
    if not isinstance(data, dict) or type(data.get("canEdit")) is not bool:
        raise ValueError("list_response_invalid")
    rows = data.get("lists")
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ValueError("list_response_invalid")
    items, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("list_response_invalid")
        list_id, name, members = row.get("id"), row.get("name"), row.get("members")
        if (not isinstance(list_id, str) or not _LIST_ID.fullmatch(list_id)
                or list_id in seen or not isinstance(name, str) or not name.strip()
                or not isinstance(members, list) or len(members) > 10000
                or not all(valid_chat_id(jid) for jid in members)):
            raise ValueError("list_response_invalid")
        seen.add(list_id)
        items.append(WhatsAppList(list_id, name.strip(), frozenset(members)))
    return ListSnapshot(tuple(items), data["canEdit"])


def list_change_verified(command, acknowledgement, snapshot):
    """A successful setter alone is insufficient; read the resulting store."""
    if not isinstance(acknowledgement, dict):
        return False
    if acknowledgement.get("action") != command.get("action"):
        return False
    action, list_id = command.get("action"), command.get("id")
    if action == "create":
        item = snapshot.find(acknowledgement.get("createdId"))
        return item is not None and item.name == command.get("name")
    if acknowledgement.get("id") != list_id:
        return False
    item = snapshot.find(list_id)
    if action == "remove":
        return item is None
    if item is None:
        return False
    if action == "rename":
        return item.name == command.get("name")
    targets = {normalize_list_jid(jid) for jid in command.get("chatIds", [])}
    members = {normalize_list_jid(jid) for jid in item.members}
    if action == "addChats":
        return bool(targets) and targets <= members
    if action == "removeChats":
        return bool(targets) and targets.isdisjoint(members)
    return False
