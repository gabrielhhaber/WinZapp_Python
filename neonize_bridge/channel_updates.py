"""A channel's updates through `get-messages/<id>@newsletter`, so the channels
window's "Open channel" reads a channel on Neonize as it does on WPPConnect.

A channel post is not a conversation, so it never goes to the store's chats or
messages; what WhatsApp answers is cached in channel_posts instead (read()).
The updates are asked of WhatsApp: neonize's get_newsletter_messages,
newest first from a server id. Each update goes out in the format of every
other message (msgmap.build), with its server id and view count. Paging back
names the oldest message's id; its server id is remembered from the page that
brought it (a few hundred per session at most).

The id and date of an update need neonize patch 0015, and whether this account
posted it (an admin's own update, shown as sent) patch 0016; an older build
answers the server id as the id, no date, and every update as received.

Pure functions at the top (tested in test_channel_updates.py).
"""
import logging
import threading
from collections import OrderedDict

import msgmap

log = logging.getLogger("bridge")

#: how many update ids each session remembers for paging
REMEMBERED = 2000
#: the most updates one request asks for: WhatsApp Web's own ceiling
#: (WAWebNewsletterGatingUtils.getMaxMsgCountFromServer(), 100 on 2026-11-08,
#: which logs "newsletter-server-msg-count-exceeded" above it). Above it the
#: server stops answering: measured 2026-11-09, the same query answered 100,
#: 200 and 300 at once and 500 and 1000 not in 20 s. A bridge that passed the
#: client's count=1000 through got GetNewsletterMessagesError (whatsmeow's IQ
#: timeout) for every channel, and get-messages timed out at 30 s (a user's
#: bridge.log, 3.0.0.5353alpha: 4340 refusals in one log).
PER_REQUEST = 100
#: requests per get-messages call, so a sync's "everything" (count=100000)
#: stays a few hundred updates
MAX_REQUESTS = 5


def kept_in_store(chat) -> bool:
    """Whether a message of *chat* is stored: a channel update never is, even
    the one an admin just posted (the channel is read from WhatsApp each time)."""
    return not str(chat or "").endswith("@newsletter")


def update_id(update) -> str:
    """The message id of a channel update; its server id on an older build."""
    return str(getattr(update, "MessageID", "") or getattr(update, "MessageServerID", "") or "")


def updates_to_wpp(channel, updates):
    """[(wpp message, server id)] in chronological order, skipping the
    updates with no content (deleted ones) and the ones msgmap cannot show."""
    out = []
    for update in updates or ():
        message = update.Message if update.HasField("Message") else None
        if message is None:
            continue
        mid = update_id(update)
        # An update this account posted (an admin's own) is sent, as WhatsApp
        # shows it: the server's is_sender, neonize patch 0016 (an older
        # build has no such field and shows every update as received).
        mine = bool(getattr(update, "IsSender", False))
        w = msgmap.build(chat=channel, from_me=mine, id=mid, sender=channel,
                         # whatsmeow's zero time is year 1 (-62135596800): no date
                         ts=max(0, int(getattr(update, "Timestamp", 0) or 0)), message=message)
        if w is None or not mid:
            continue
        w["serverId"] = int(update.MessageServerID or 0)
        w["viewCount"] = int(update.ViewsCount or 0)
        out.append((w, w["serverId"]))
    out.sort(key=lambda pair: (pair[0]["t"], pair[1]))
    return out


class ServerIds:
    """message id -> what the bridge answered about an update (bounded): its
    server id, and the type, date and author an edit is judged by. A channel
    post is never stored (kept_in_store), and opening a channel reads its
    updates again, so the ones a user can edit are always remembered here."""

    def __init__(self, limit=REMEMBERED):
        self.limit = limit
        self._ids = OrderedDict()
        self._lock = threading.Lock()

    def remember(self, pairs):
        with self._lock:
            for w, server_id in pairs:
                self._ids[w["id"]["id"]] = {
                    "server_id": server_id, "type": w.get("type") or "",
                    "t": int(w.get("t") or 0), "fromMe": bool(w.get("fromMe"))}
                self._ids.move_to_end(w["id"]["id"])
            while len(self._ids) > self.limit:
                self._ids.popitem(last=False)

    def of(self, message_id):
        details = self.details(message_id)
        return details["server_id"] if details else None

    def details(self, message_id):
        with self._lock:
            found = self._ids.get(message_id)
            return dict(found) if found else None


def server_ids_of(s) -> ServerIds:
    if getattr(s, "_channel_server_ids", None) is None:
        s._channel_server_ids = ServerIds()
    return s._channel_server_ids


def anchor_server_id(value) -> int:
    """The `serverId` the client sends beside a channel anchor, or 0."""
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def is_channel(chat) -> bool:
    return str(chat or "").endswith("@newsletter")


def _store(s):
    return getattr(s, "db", None)


def details_of(s, message_id):
    """What an edit is judged by (ServerIds.details), from this session's
    reads or, after a restart, from the cache."""
    found = server_ids_of(s).details(message_id)
    if found is None and _store(s) is not None:
        cached = _store(s).channel_post(message_id)
        if cached:
            _channel, w, server_id = cached
            found = {"server_id": server_id, "type": w.get("type") or "",
                     "t": int(w.get("t") or 0), "fromMe": bool(w.get("fromMe"))}
    return found


def edit(s, channel, message_id, text):
    """Edits an update this account posted in *channel* (whatsmeow form):
    its text, or the caption of a media update whose media is cached. whatsmeow
    sends a channel's edit as an admin edit of the original id (sendNewsletter).
    Runs on a thread."""
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message
    from neonize.utils import build_jid
    details = details_of(s, message_id) or {}
    store = _store(s)
    record = store.media_record(message_id) if store is not None else None
    if details.get("type") not in ("", "chat") and record:
        new = Message.FromString(record)
        field = msgmap.media_field(new)
        if not field:
            return False
        getattr(new, field).caption = text
        fields = {"caption": text}
    else:
        new = Message(conversation=text)
        fields = {"body": text}
    user, _, server = channel.partition("@")
    try:
        s.client.edit_message(build_jid(user, server), message_id, new)
    except Exception as error:
        log.info("channel update edit refused (%s)", type(error).__name__)
        return False
    if store is not None:
        store.channel_post_change(message_id, **fields)
    return True


def deleted_ids(updates):
    """The ids of the updates WhatsApp answers with no content: deleted ones."""
    return [update_id(u) for u in updates or () if not u.HasField("Message")]


def _raw_records(updates):
    out = {}
    for update in updates or ():
        if update.HasField("Message"):
            out[update_id(update)] = msgmap.media_record_of(update.Message)
    return out


def read(s, channel, count, before, jid_from_text, before_server_id=0):
    """The page get-messages answers for `channel`. Runs on a thread. An
    unknown anchor answers [] (never the newest), as for any other chat.

    The anchor's server id is the one remembered from the page that brought
    it, the one the client stored with the update (*before_server_id*), or the
    cache's: after a restart every scroll up in a channel used to answer []
    and the client took the channel's history for finished. A count above
    PER_REQUEST is asked in pages of it, newest first, at most MAX_REQUESTS.

    Every answer is cached in the store (channel_posts, never a chat). The
    newest page is always asked of WhatsApp, which has the edits, deletions,
    new updates and view counts; an older page the cache holds whole is read
    from disk instead, and with WhatsApp unreachable the cache answers."""
    ids = server_ids_of(s)
    store = _store(s)
    start = 0
    if before:
        start = ids.of(before) or anchor_server_id(before_server_id)
        if not start and store is not None:
            cached = store.channel_post(before)
            start = cached[2] if cached else 0
        if not start:
            return []
    wanted = min(max(1, int(count)), PER_REQUEST * MAX_REQUESTS)
    if start and store is not None:
        cached = store.channel_posts(channel, wanted, start)
        if len(cached) >= wanted:
            ids.remember(cached)
            return [w for w, _server_id in cached]
    jid = jid_from_text(channel)
    pairs, seen, reached = [], set(), False
    request_start = start
    for _request in range(MAX_REQUESTS):
        asked = min(PER_REQUEST, wanted - len(pairs))
        try:
            updates = list(s.client.get_newsletter_messages(jid, asked, int(request_start)))
        except Exception as error:
            log.info("channel updates refused (%s)", type(error).__name__)
            break
        reached = True
        page = [p for p in updates_to_wpp(channel, updates) if p[0]["id"]["id"] not in seen]
        seen.update(w["id"]["id"] for w, _server_id in page)
        ids.remember(page)
        if store is not None:
            raws = _raw_records(updates)
            store.channel_posts_save(
                channel, [(w, sid, raws.get(w["id"]["id"])) for w, sid in page])
            store.channel_posts_forget(channel, deleted_ids(updates))
        pairs = page + pairs
        oldest = min((int(u.MessageServerID or 0) for u in updates), default=0)
        if len(updates) < asked or len(pairs) >= wanted or not oldest:
            break
        if request_start and oldest >= request_start:
            break       # no progress: the server answered the same window
        request_start = oldest
    if not reached and store is not None:
        cached = store.channel_posts(channel, wanted, start)
        ids.remember(cached)
        return [w for w, _server_id in cached]
    pairs.sort(key=lambda pair: (pair[0]["t"], pair[1]))
    return [w for w, _server_id in pairs]
