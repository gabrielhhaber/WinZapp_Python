"""Chat and message reading routes of the bridge: chat lists (all, archived,
with messages, muted, broadcast lists), a chat by id, the stored messages of a
chat, the unread ones and the votes of a poll. Everything comes from the store
(store.py); no route talks to WhatsApp.

The format of each chat is that of `list-chats` (server.py), which is what
WinZapp reads. The client does not call these routes today; the formats of the
ones it does not use follow what WPPConnect returns, from memory, and were not
checked against a real account.

The pure functions are at the top (tested in test_chat_listing.py);
`register_routes` wires the routes to the server.
"""
import contacts
import msgmap

EVERYTHING = 1_000_000  # "all" the messages: more than any chat keeps
NO_END = 2**31 - 1      # mute "forever" in the store


def boolean(text, default):
    """'true'/'1'/'yes' -> True, 'false'/'0'/'no' -> False, empty -> default."""
    if text is None or str(text).strip() == "":
        return default
    return str(text).strip().lower() in ("true", "1", "yes", "y")


def chat_jid(text, group=False):
    """Id received in the route -> whatsmeow JID. Without '@': a number, or a
    group if `group`."""
    text = str(text or "").strip()
    if "@" not in text and group:
        text += "@g.us"
    return msgmap.jid_whatsmeow(text)


def contact_names(contacts):
    """{jid in WPPConnect format: name saved in the address book} of the stored
    contacts. A nickname is not in it: the client files `name` as the address
    book's, and a nickname there replaced the name the user gave."""
    return {msgmap.jid_wpp(c["jid"]): c["name"] for c in contacts if c["name"]}


def listing_identities(chat_rows, contact_rows, phone_of):
    """({chat id in WPPConnect format: address-book name}, {...: nickname}) of
    the person behind each private chat. Most chats are kept under an LID, whose
    own row has no address-book name: `phone_of(lid)` gives the number's JID
    ('' when unknown), where the name is."""
    rows = {c["jid"]: c for c in contact_rows}
    names, nicknames = {}, {}
    for c in chat_rows:
        jid = c["jid"]
        if not jid.endswith(("@lid", "@s.whatsapp.net")):
            continue
        # a chat under a number looks for its address-book name under both forms of it
        phone = phone_of(jid) if jid.endswith("@lid") else jid
        name, nickname = contacts.book_identity(rows, jid, phone)
        wpp = msgmap.jid_wpp(jid)
        if name:
            names[wpp] = name
        if nickname:
            nicknames[wpp] = nickname
    return names, nicknames


def chat_to_wpp(c, names, wpp_id, messages=None, nicknames=None):
    """A store row -> the chat as `list-chats` returns it. The name is the one in
    the address book, else the one the phone gave the conversation; the person's
    own nickname goes in `contact.pushname`, where the client reads it."""
    jid = msgmap.jid_wpp(c["jid"])
    chat = {
        "id": wpp_id(c["jid"]),
        "name": names.get(jid) or c["name"] or None,
        "isGroup": jid.endswith("@g.us"),
        "unreadCount": c["unread"], "t": c["ts"],
        "archive": bool(c["archived"]), "pin": c["pinned"] or False,
        "muteExpiration": c["muted"], "isLocked": bool(c.get("locked")),
        "msgs": messages,
    }
    nickname = (nicknames or {}).get(jid)
    if nickname:
        chat["contact"] = {"pushname": nickname}
    return chat


def is_broadcast(jid):
    """Broadcast list (not the status board, which is never a chat)."""
    return jid.endswith("@broadcast") and jid != "status@broadcast"


def is_muted(c, now):
    return int(c["muted"] or 0) > now


def select(chat_rows, criterion, now=0):
    """The chats that meet the criterion: 'all', 'archived', 'broadcast' or
    'muted'. The status board always stays out."""
    without_status = [c for c in chat_rows if c["jid"] != "status@broadcast"
                      and not c["jid"].endswith("@newsletter")]
    if criterion == "archived":
        return [c for c in without_status if c["archived"]]
    if criterion == "broadcast":
        return [c for c in without_status if is_broadcast(c["jid"])]
    if criterion == "muted":
        return [c for c in without_status if is_muted(c, now)]
    return without_status


def of_kind(chat_rows, kind):
    """Filter of `list-mutes/:type`: 'all', 'group' (or 'groups') and 'user'
    (or 'contact'). Another type (for example 'community') is not recognized
    by the JID: returns empty."""
    kind = str(kind or "all").lower()
    if kind == "all":
        return list(chat_rows)
    if kind in ("group", "groups"):
        return [c for c in chat_rows if c["jid"].endswith("@g.us")]
    if kind in ("user", "users", "contact", "contacts"):
        return [c for c in chat_rows
                if c["jid"].endswith(("@s.whatsapp.net", "@c.us", "@lid"))]
    return []


def without_mine(messages, include_mine):
    return list(messages) if include_mine else [m for m in messages if not m.get("fromMe")]


def received_unread(messages, quantity):
    """The last `quantity` received messages (not mine), in chronological
    order: the ones the chat's unread counter counts."""
    if quantity <= 0:
        return []
    received = [m for m in messages if not m.get("fromMe")]
    return received[-quantity:]


def register_routes(r, *, session, ok, error, wpp_id, now):
    """Wire the chat reading routes to the aiohttp router `r`."""

    def chats(s, criterion, with_messages=False):
        names = contact_names(s.db.contacts())
        out = []
        for c in select(s.db.chats(), criterion, now()):
            latest = s.db.messages(c["jid"], 1)
            if with_messages and not latest:
                continue
            out.append(chat_to_wpp(c, names, wpp_id, latest if with_messages else None))
        return out

    async def all_chats(req):
        return ok(status="success", response=chats(session(req), "all"))

    async def all_chats_archived(req):
        return ok(status="success", response=chats(session(req), "archived"))

    async def all_chats_with_messages(req):
        s = session(req)
        listing = chats(s, "all", with_messages=True)
        if boolean(req.query.get("withNewMessageOnly"), False):
            listing = [c for c in listing if c["unreadCount"] > 0]
        return ok(status="success", response=listing)

    async def all_broadcast_list(req):
        return ok(status="success", response=chats(session(req), "broadcast"))

    async def list_mutes(req):
        s = session(req)
        names = contact_names(s.db.contacts())
        muted_chats = of_kind(select(s.db.chats(), "muted", now()),
                              req.match_info.get("type"))
        return ok(status="success", response=[chat_to_wpp(c, names, wpp_id) for c in muted_chats])

    async def chat_by_id(req):
        s = session(req)
        jid = chat_jid(req.match_info["phone"], boolean(req.query.get("isGroup"), False))
        found_chat = [c for c in s.db.chats() if c["jid"] == jid]
        if not found_chat:
            return error("chat not found", 404)
        names = contact_names(s.db.contacts())
        return ok(status="success", response=chat_to_wpp(found_chat[0], names, wpp_id))

    async def chat_messages(req):
        s = session(req)
        jid = chat_jid(req.match_info["phone"], boolean(req.query.get("isGroup"), False))
        items = s.db.messages(jid, EVERYTHING)
        return ok(status="success",
                  response=without_mine(items, boolean(req.query.get("includeMe"), True)))

    def unread_by_chat(s):
        """[(chat, [unread received messages])] of the chats with unread."""
        found = []
        for c in s.db.chats():
            if int(c["unread"] or 0) > 0 and c["jid"] != "status@broadcast":
                recent = s.db.messages(c["jid"], int(c["unread"]) * 3 + 50)
                found.append((c, received_unread(recent, int(c["unread"]))))
        return found

    async def unread_messages(req):
        s = session(req)
        names = contact_names(s.db.contacts())
        return ok(status="success", response=[
            {**chat_to_wpp(c, names, wpp_id), "messages": items}
            for c, items in unread_by_chat(s)])

    async def all_unread_messages(req):
        s = session(req)
        return ok(status="success", response=[
            m for _, items in unread_by_chat(s) for m in items])

    async def votes(req):
        s = session(req)
        vote_rows = s.votes_wpp(req.match_info["id"])
        if vote_rows is None:
            return error("poll not found", 404)
        return ok(status="success", response=vote_rows)

    p = "/api/{tok}/"
    r.add_get(p + "all-chats", all_chats)
    r.add_get(p + "all-chats-archived", all_chats_archived)
    r.add_get(p + "all-chats-with-messages", all_chats_with_messages)
    r.add_get(p + "all-broadcast-list", all_broadcast_list)
    r.add_get(p + "list-mutes/{type}", list_mutes)
    r.add_get(p + "chat-by-id/{phone}", chat_by_id)
    # neonize does not ask the phone for old history: "load" is reading what
    # the bridge already stored, the same as all-messages-in-chat.
    r.add_get(p + "all-messages-in-chat/{phone}", chat_messages)
    r.add_get(p + "load-messages-in-chat/{phone}", chat_messages)
    r.add_get(p + "unread-messages", unread_messages)
    r.add_get(p + "all-unread-messages", all_unread_messages)
    r.add_get(p + "votes/{id}", votes)
