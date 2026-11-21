"""Label routes (WhatsApp Business) of the bridge: create, delete, apply to
chats and list. WinZapp does not call these routes; they exist for whoever
talks to the bridge as if it were WPPConnect Server.

What neonize allows: WRITING to the app state (`send_app_state` with a
PatchInfo, the same one whatsmeow builds in BuildLabelEdit / BuildLabelChat).
WinZapp's build also exposes app state mutations. label_events.registry reads
their existing mirror; labels.json remains the fallback on older builds and
before the first complete regular sync. Phone edits and associations therefore
reach both the label routes and onupdatelabel with the same state.

The pure functions are at the top (tested in test_labels.py); `register_routes`
wires the routes to the server.
"""
import asyncio
import json
import logging
import os
import time

from aiohttp import web

log = logging.getLogger("bridge")

FIRST_OWN_ID = 1000
COLORS = 20                 # WhatsApp's palette has 20 colors, indexes 0..19
MUTATION_VERSION = 3        # the version whatsmeow uses in these mutations
LABEL_INDEX = "label_edit"
CHAT_INDEX = "label_jid"
FILE = "labels.json"


def load(folder) -> dict:
    """{id: {"name", "colorIndex", "chats": [jid, ...]}}; empty if there is no file."""
    try:
        with open(os.path.join(folder, FILE), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(folder, labels) -> None:
    path = os.path.join(folder, FILE)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(labels, f, ensure_ascii=False)
    os.replace(path + ".tmp", path)


def next_id(labels) -> str:
    used = [int(i) for i in labels if str(i).isdigit()]
    return str(max([FIRST_OWN_ID - 1] + used) + 1)


def color_index(options, labels) -> int:
    """Color requested in `options.labelColor` (or `color`) when it is a valid
    palette index; otherwise we rotate the colors by the number of labels.
    ARGB / hexadecimal values have no safe match in the palette."""
    requested = None
    if isinstance(options, dict):
        requested = options.get("labelColor", options.get("color"))
    if isinstance(requested, int) and not isinstance(requested, bool) and 0 <= requested < COLORS:
        return requested
    return len(labels) % COLORS


def label_to_wpp(label_id, data) -> dict:
    """An entry of `get-all-labels`."""
    return {"id": str(label_id), "name": data.get("name", ""),
            "colorIndex": int(data.get("colorIndex", 0)),
            "count": len(data.get("chats") or [])}


def list_to_wpp(labels) -> list:
    return [label_to_wpp(i, d) for i, d in sorted(
        labels.items(), key=lambda pair: int(pair[0]) if str(pair[0]).isdigit() else 0)]


def create(labels, name, options) -> dict:
    """Registers the new label in `labels` and returns its entry."""
    label_id = next_id(labels)
    labels[label_id] = {"name": name, "colorIndex": color_index(options, labels),
                        "chats": []}
    return label_to_wpp(label_id, labels[label_id])


def apply(labels, label_id, jid, attach) -> None:
    """Marks / unmarks `jid` in the label, if the bridge knows it."""
    chats = labels.get(str(label_id), {}).get("chats")
    if chats is None:
        return
    if attach and jid not in chats:
        chats.append(jid)
    elif not attach and jid in chats:
        chats.remove(jid)


def label_requests(options) -> list:
    """`options` of add-or-remove-label ([{labelId, type: add|remove}]) ->
    [(id, attach)]. Entries out of the format are ignored."""
    out = []
    for o in options if isinstance(options, list) else []:
        if not isinstance(o, dict) or o.get("labelId") in (None, ""):
            continue
        kind = str(o.get("type", "add")).lower()
        if kind in ("add", "remove"):
            out.append((str(o["labelId"]), kind == "add"))
    return out


def edit_mutation(label_id, name, color, deleted, now_ms) -> dict:
    """What whatsmeow calls BuildLabelEdit, as a plain dictionary."""
    return {"index": [LABEL_INDEX, str(label_id)],
            "value": {"timestamp": now_ms,
                      "labelEditAction": {"name": name, "color": color, "deleted": deleted}}}


def chat_mutation(label_id, chat_jid_text, attach, now_ms) -> dict:
    """What whatsmeow calls BuildLabelChat, as a plain dictionary."""
    return {"index": [CHAT_INDEX, str(label_id), chat_jid_text],
            "value": {"timestamp": now_ms,
                      "labelAssociationAction": {"labeled": attach}}}


def build_patch(mutations):
    """Mutation dictionaries -> neonize PatchInfo (imports neonize here, so the
    functions above can be tested without it)."""
    from neonize.proto.Neonize_pb2 import MutationInfo, PatchInfo
    from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import (
        LabelAssociationAction, LabelEditAction, SyncActionValue)

    patch = PatchInfo(Timestamp=int(time.time() * 1000), Type=PatchInfo.REGULAR)
    for m in mutations:
        value = m["value"]
        if "labelEditAction" in value:
            v = SyncActionValue(timestamp=value["timestamp"],
                                labelEditAction=LabelEditAction(**value["labelEditAction"]))
        else:
            v = SyncActionValue(
                timestamp=value["timestamp"],
                labelAssociationAction=LabelAssociationAction(**value["labelAssociationAction"]))
        patch.Mutations.append(MutationInfo(Index=m["index"], Version=MUTATION_VERSION, Value=v))
    return patch


def register_routes(r, *, session, ok, jid_from_text, jid_text):
    """Wire the label routes to the aiohttp router `r`. None was tested against
    a real account."""

    async def execute(func, *args):
        return await asyncio.get_running_loop().run_in_executor(None, func, *args)

    def error(message, status=500):
        return web.json_response({"status": "error", "message": str(message)}, status=status)

    def connected(s):
        return s.client is not None and s.status == "CONNECTED"

    # load -> send -> save cannot interleave, or two requests would get the
    # same id and one would erase the other's record
    lock = asyncio.Lock()

    def now_ms():
        return int(time.time() * 1000)

    async def send(s, mutations):
        import app_state_mirror
        patch = build_patch(mutations)
        await execute(app_state_mirror.send, s, "regular", [
            (list(m.Index), m.Version, m.Value, False) for m in patch.Mutations])

    def current(s):
        from label_events import registry
        return registry(s)

    def visible(s):
        # Writes keep the full registry (a new id must not reuse a list's id);
        # reads and deletions never reach the account's chat lists.
        from label_events import business_labels
        return business_labels(current(s))

    async def add_new_label(req):
        s = session(req)
        body = await req.json()
        if not connected(s):
            return error("Disconnected", 400)
        name = str(body.get("name") or "").strip()
        if not name:
            return error("name is required", 400)
        async with lock:
            labels = current(s)
            fresh = create(labels, name, body.get("options"))
            try:
                await send(s, [edit_mutation(
                    fresh["id"], name, fresh["colorIndex"], False, now_ms())])
            except Exception as e:
                log.exception("add-new-label")
                return error(e)
            save(s.folder, labels)
        return web.json_response({"status": "success", "response": fresh}, status=201)

    async def add_or_remove_label(req):
        s = session(req)
        body = await req.json()
        if not connected(s):
            return error("Disconnected", 400)
        chats = body.get("chatIds") or []
        requests = label_requests(body.get("options"))
        if not chats or not requests:
            return error("chatIds and options [{labelId, type}] are required", 400)
        jids = [jid_text(jid_from_text(c)) for c in chats]
        now = now_ms()
        async with lock:
            try:
                await send(s, [chat_mutation(i, j, attach, now)
                               for j in jids for i, attach in requests])
            except Exception as e:
                log.exception("add-or-remove-label")
                return error(e)
            labels = current(s)
            for j in jids:
                for i, attach in requests:
                    apply(labels, i, j, attach)
            save(s.folder, labels)
        return ok(status="success", response={"chatIds": list(chats), "options": [
            {"labelId": i, "type": "add" if c else "remove"} for i, c in requests]})

    async def get_all_labels(req):
        s = session(req)
        return ok(status="success", response=list_to_wpp(visible(s)))

    async def delete(s, ids):
        async with lock:
            labels = current(s)
            now = now_ms()
            await send(s, [edit_mutation(
                i, labels.get(i, {}).get("name", ""),
                labels.get(i, {}).get("colorIndex", 0), True, now) for i in ids])
            for i in ids:
                labels.pop(i, None)
            save(s.folder, labels)

    async def delete_label(req):
        s = session(req)
        if not connected(s):
            return error("Disconnected", 400)
        label_id = req.match_info["id"]
        if label_id not in visible(s):
            return error("label not found", 404)
        try:
            await delete(s, [label_id])
        except Exception as e:
            log.exception("delete-label")
            return error(e)
        return ok(status="success", response={"id": label_id})

    async def delete_all_labels(req):
        s = session(req)
        if not connected(s):
            return error("Disconnected", 400)
        try:
            async with lock:
                ids = list(visible(s))
            if ids:
                await delete(s, ids)
        except Exception as e:
            log.exception("delete-all-labels")
            return error(e)
        return ok(status="success", response={"ids": ids})

    r.add_post("/api/{tok}/add-new-label", add_new_label)
    r.add_post("/api/{tok}/add-or-remove-label", add_or_remove_label)
    r.add_get("/api/{tok}/get-all-labels", get_all_labels)
    r.add_put("/api/{tok}/delete-all-labels", delete_all_labels)
    r.add_put("/api/{tok}/delete-label/{id}", delete_label)
