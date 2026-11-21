"""WhatsApp's custom chat lists (`GET` / `POST custom-lists`), the contract of
WinZapp's WPPConnect route (chatListsController.ts, docs/reference/chat-lists.md).

A list is a label of type CUSTOM (5). What WhatsApp Web sends, read over CDP
on 2026-10-26 (WAWebBizLabelEditingAction, WAWebLabelSync, WAWebLabelJidSync),
all in the `regular` collection, version 3:

- create: ["label_edit", <next id>] {name, deleted: false, isActive: true,
  type: CUSTOM}; the id is one more than the highest the account has;
- rename: the same entry with the new name and its other fields kept;
- delete: {name, color, deleted: true}, and in the same patch
  ["label_jid", <id>, <chat>] {labeled: false} for every chat of the list
  (checked again by creating and deleting a list over CDP on 2026-11-18);
- add / remove chats: ["label_jid", <id>, <chat>] {labeled}, one per chat,
  where <chat> is a person's LID when known (else the number,
  @s.whatsapp.net) or the group.

The lists and their chats are read from app_state_mirror (WinZapp's neonize
build), which is complete once a full sync of `regular` was seen: the first
read asks for one. Editing is refused (403) on a Business account, as the
WPPConnect route does: its labels are not lists. A neonize build without app
state mutations answers 501, which the client shows as "unavailable".
Not run against a real account.
"""
import asyncio
import logging
import re

from aiohttp import web

import app_state_mirror

log = logging.getLogger("bridge")

COLLECTION = "regular"
LABEL_INDEX, CHAT_INDEX = "label_edit", "label_jid"
VERSION = 3
CUSTOM = 5
MAX_NAME = 100            # UTF-16 code units, as the client counts
MAX_CHATS = 500
ACTIONS = ("create", "rename", "remove", "addChats", "removeChats")
_LIST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_CHAT = re.compile(r"\d+(?:-\d+)?(?::\d+)?@(c\.us|s\.whatsapp\.net|lid|g\.us)\Z")
STATUS = {"lists_not_available": 503, "list_editing_not_available": 403,
          "list_operation_unconfirmed": 503}


class ListError(Exception):
    def __init__(self, code, status=None):
        super().__init__(code)
        self.code = code
        self.status = status or STATUS.get(code, 400)


def chat_key(jid) -> str:
    """The comparable form of a chat: no device, @c.us as @s.whatsapp.net;
    '' when it is not a chat a list can hold."""
    if not isinstance(jid, str) or not _CHAT.fullmatch(jid):
        return ""
    user, _, server = jid.partition("@")
    return f"{user.split(':')[0]}@{'s.whatsapp.net' if server == 'c.us' else server}"


def name_ok(name) -> bool:
    return (isinstance(name, str) and bool(name.strip())
            and len(name.strip().encode("utf-16-le")) // 2 <= MAX_NAME)


def custom_lists(labels) -> dict:
    """{id: label} of the labels that are custom lists still in use.
    `labels` is {id: {"name", "type", "deleted", ...}}."""
    return {i: l for i, l in labels.items()
            if l.get("type") == CUSTOM and not l.get("deleted") and (l.get("name") or "").strip()}


def snapshot(labels, links, can_edit) -> dict:
    """The `response` of a read. `links` is [(label id, chat, labeled)]. A
    read-only snapshot says why with the client's "account_disabled"."""
    lists = custom_lists(labels)
    members = {i: [] for i in lists}
    for label_id, chat, labeled in links:
        key = chat_key(chat)
        if labeled and key and label_id in members and key not in members[label_id]:
            members[label_id].append(key)
    rows = [{"id": i, "name": lists[i]["name"].strip(), "members": members[i]}
            for i in sorted(lists, key=lambda i: (len(i), i))]
    return {"canEdit": bool(can_edit), "editingReason": "" if can_edit else "account_disabled",
            "lists": rows}


def next_label_id(ids) -> str:
    """One more than the highest numeric label id (a deleted one included)."""
    numbers = [int(i) for i in ids if str(i).isdigit()]
    return str(max(numbers, default=0) + 1)


def check_command(command, labels):
    """Validates a write against the lists; raises ListError."""
    if not isinstance(command, dict) or command.get("action") not in ACTIONS:
        raise ListError("list_command_invalid")
    action = command["action"]
    if action != "create":
        list_id = command.get("id")
        if not isinstance(list_id, str) or not _LIST_ID.fullmatch(list_id):
            raise ListError("list_command_invalid")
        if list_id not in custom_lists(labels):
            raise ListError("list_not_found")
    if action in ("create", "rename") and not name_ok(command.get("name")):
        raise ListError("list_name_required")
    if action in ("addChats", "removeChats"):
        chats = command.get("chatIds")
        if (not isinstance(chats, list) or not chats or len(chats) > MAX_CHATS
                or not all(chat_key(c) for c in chats)):
            raise ListError("list_command_invalid")


def chats_to_change(command, links, chat_index):
    """The chat arguments of the label_jid mutations of addChats/removeChats.
    Removing uses the entries the list holds (their exact index); adding, the
    index WhatsApp Web would write (`chat_index(key)`)."""
    targets = list(dict.fromkeys(chat_key(c) for c in command["chatIds"]))
    held = {}
    for label_id, chat, labeled in links:
        if label_id == command["id"] and labeled:
            held.setdefault(chat_key(chat), []).append(chat)
    if command["action"] == "removeChats":
        return [chat for key in targets for chat in held.get(key, [])]
    return [chat_index(key) for key in targets if key not in held]


def members_of(list_id, links):
    """The exact chat arguments of the entries that hold a chat in the list."""
    return list(dict.fromkeys(chat for label_id, chat, labeled in links
                              if label_id == list_id and labeled))


def register_routes(r, *, session, jid_from_text, jid_text):
    """Wire the routes to the aiohttp router `r`."""

    def failure(error):
        return web.json_response({"status": "error", "code": error.code}, status=error.status)

    def actions():
        from neonize.proto.waSyncAction import WAWebProtobufSyncAction_pb2 as pb
        return pb

    def read_mirror(s):
        """({id: label dict}, [(id, chat, labeled)], {id: LabelEditAction})."""
        pb = actions()
        mirror = app_state_mirror.mirror_of(s)
        labels, raw = {}, {}
        for index, _version, value in mirror.entries(LABEL_INDEX):
            if len(index) != 2:
                continue
            action = pb.SyncActionValue.FromString(value).labelEditAction
            raw[index[1]] = action
            labels[index[1]] = {"name": action.name, "type": action.type,
                                "deleted": action.deleted}
        links = []
        for index, _version, value in mirror.entries(CHAT_INDEX):
            if len(index) == 3:
                links.append((index[1], index[2], pb.SyncActionValue.FromString(
                    value).labelAssociationAction.labeled))
        return labels, links, raw

    def business(s):
        # whatsmeow keeps the business name only for a Business account
        # (neonize's Device spells it BussinessName)
        try:
            return bool(getattr(s.client.get_me(), "BussinessName", ""))
        except Exception:
            return False

    def chat_index(s):
        def index(key):
            if key.endswith("@s.whatsapp.net"):
                try:
                    lid = jid_text(s.client.get_lid_from_pn(jid_from_text(key)))
                except Exception:
                    lid = ""
                if lid.endswith("@lid"):
                    return chat_key(lid)
            return key
        return index

    def known_chats(s, command):
        for chat in command["chatIds"]:
            key = chat_key(chat)
            if s.db.chat_exists(key) or s.db.chat_exists(chat_index(s)(key)):
                continue
            raise ListError("list_chat_not_found")

    def ready(s):
        if not app_state_mirror.supported():
            raise ListError("lists_not_available", 501)
        if s.client is None or s.status != "CONNECTED":
            raise ListError("lists_not_available")
        if not app_state_mirror.ensure_seeded(s, COLLECTION):
            raise ListError("lists_not_available")

    def read(s):
        ready(s)
        labels, links, _raw = read_mirror(s)
        return snapshot(labels, links, not business(s))

    def write(s, command):
        ready(s)
        if business(s):
            raise ListError("list_editing_not_available")
        labels, links, raw = read_mirror(s)
        check_command(command, labels)
        pb = actions()
        action = command["action"]
        if action == "addChats" or action == "removeChats":
            known_chats(s, command)
            labeled = action == "addChats"
            chats = chats_to_change(command, links, chat_index(s))
            mutations = [([CHAT_INDEX, command["id"], chat], VERSION, pb.SyncActionValue(
                labelAssociationAction=pb.LabelAssociationAction(labeled=labeled)), False)
                for chat in chats]
            if mutations:
                app_state_mirror.send(s, COLLECTION, mutations)
            return {"action": action, "id": command["id"]}
        if action == "create":
            list_id = next_label_id(labels)
            edit = pb.LabelEditAction(name=command["name"].strip(), deleted=False,
                                      isActive=True, type=CUSTOM)
        else:
            list_id = command["id"]
            current = raw[list_id]
            if action == "rename":
                edit = pb.LabelEditAction()
                edit.CopyFrom(current)
                edit.name = command["name"].strip()
            else:
                edit = pb.LabelEditAction(name=current.name, deleted=True)
                if current.HasField("color"):
                    edit.color = current.color
        mutations = [([LABEL_INDEX, list_id], VERSION,
                      pb.SyncActionValue(labelEditAction=edit), False)]
        if action == "remove":
            # WhatsApp Web unlabels every chat of the list in the same patch
            # (WAWebBizLabelEditingAction.labelDeleteAction, captured over CDP
            # on 2026-11-18); without it the chats kept a list that no longer
            # exists.
            mutations += [([CHAT_INDEX, list_id, chat], VERSION, pb.SyncActionValue(
                labelAssociationAction=pb.LabelAssociationAction(labeled=False)), False)
                for chat in members_of(list_id, links)]
        app_state_mirror.send(s, COLLECTION, mutations)
        if action == "create":
            return {"action": action, "createdId": list_id}
        return {"action": action, "id": list_id}

    lock = asyncio.Lock()   # two creates must not take the same id

    async def handle(req, command):
        s = session(req)
        loop = asyncio.get_running_loop()
        try:
            if command is None:
                response = await loop.run_in_executor(None, read, s)
            else:
                async with lock:
                    response = await loop.run_in_executor(None, write, s, command)
        except ListError as e:
            return failure(e)
        except Exception:
            log.exception("custom-lists")
            return failure(ListError("list_operation_unconfirmed"))
        return web.json_response({"status": "success", "response": response})

    async def read_lists(req):
        return await handle(req, None)

    async def change_list(req):
        try:
            body = await req.json()
        except ValueError:
            body = None
        body = body if isinstance(body, dict) else {}
        return await handle(req, {k: body.get(k) for k in ("action", "id", "name", "chatIds")})

    r.add_get("/api/{tok}/custom-lists", read_lists)
    r.add_post("/api/{tok}/custom-lists", change_list)
