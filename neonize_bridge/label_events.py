"""Read label state from the existing mirror and forward chat associations.

Full sync replays hydrate the mirror, but are not new add/remove events.
There is one AppStateMutationEv handler, owned by app_state_mirror.py.
"""
import app_state_mirror
import labels
import msgmap
from socket_events import wid


def registry(s):
    """Current labels, including metadata and associations made on the phone.

    Before a complete regular sync, retain legacy locally-created labels. After
    that sync the mirror is authoritative, including labels that disappeared.
    """
    from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import SyncActionValue
    mirror = app_state_mirror.mirror_of(s)
    result = {} if mirror.seeded("regular") else labels.load(s.folder)
    for data in result.values():
        data["chats"] = list(dict.fromkeys(
            msgmap.jid_whatsmeow(chat) for chat in data.get("chats", [])))
    for index, _, value in mirror.entries("label_edit"):
        if len(index) != 2:
            continue
        action = SyncActionValue.FromString(value).labelEditAction
        label_id = str(index[1])
        if action.deleted:
            result.pop(label_id, None)
            continue
        result[label_id] = {"name": action.name, "colorIndex": action.color,
                            "type": action.type, "isActive": action.isActive,
                            "isImmutable": action.isImmutable,
                            "predefinedID": action.predefinedID,
                            "chats": result.get(label_id, {}).get("chats", [])}
    for index, _, value in mirror.entries("label_jid"):
        if len(index) != 3:
            continue
        action = SyncActionValue.FromString(value).labelAssociationAction
        labels.apply(result, str(index[1]), msgmap.jid_whatsmeow(index[2]), action.labeled)
    return result


# LabelEditAction.ListType: NONE (a Business label) and PREDEFINED with a
# predefinedID (Business's predefined labels). Every other type is one of the
# account's chat lists (Unread, Favorites, CUSTOM lists...), which the label
# routes must not show or delete; custom_lists.py owns those.
NONE, PREDEFINED = 0, 4


def business_labels(labels):
    """Only the labels the label routes may list or delete."""
    def is_label(data):
        kind = int(data.get("type", NONE) or NONE)
        if data.get("isImmutable"):
            return False
        return kind == NONE or (kind == PREDEFINED and int(data.get("predefinedID") or 0) > 0)
    return {i: data for i, data in labels.items() if is_label(data)}


def association(s, index, remove, full_sync, action):
    """Emit only chat label associations, after their state was recorded."""
    index = list(index)
    if (full_sync or remove or len(index) != 3 or index[0] != "label_jid"
            or action is None or not action.HasField("labelAssociationAction")):
        return
    if not callable(getattr(s, "emit", None)):
        return
    label_id, chat = str(index[1]), msgmap.jid_whatsmeow(index[2])
    if not chat.endswith(("@g.us", "@lid", "@s.whatsapp.net")):
        return
    current = registry(s)
    linked = [i for i, data in current.items() if chat in data.get("chats", [])]
    label = current.get(label_id)
    s.emit("onupdatelabel", {
        "session": s.name, "chat": {"id": wid(chat), "labels": linked},
        "ids": [label_id],
        "labels": [labels.label_to_wpp(label_id, label) if label else None],
        "type": "add" if action.labelAssociationAction.labeled else "remove"})
