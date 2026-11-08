"""A channel's updates through `get-messages/<id>@newsletter`, so the channels
window's "Open channel" reads a channel on Neonize as it does on WPPConnect.

The store keeps nothing of channels (a channel post is not a conversation), so
the updates are asked of WhatsApp each time: neonize's get_newsletter_messages,
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
#: which logs "newsletter-server-msg-count-exceeded" above it)
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
    """message id -> server id of the updates already answered (bounded)."""

    def __init__(self, limit=REMEMBERED):
        self.limit = limit
        self._ids = OrderedDict()
        self._lock = threading.Lock()

    def remember(self, pairs):
        with self._lock:
            for w, server_id in pairs:
                self._ids[w["id"]["id"]] = server_id
                self._ids.move_to_end(w["id"]["id"])
            while len(self._ids) > self.limit:
                self._ids.popitem(last=False)

    def of(self, message_id):
        with self._lock:
            return self._ids.get(message_id)


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


def read(s, channel, count, before, jid_from_text, before_server_id=0):
    """The page get-messages answers for `channel`. Runs on a thread. An
    unknown anchor answers [] (never the newest), as for any other chat.

    The anchor's server id is the one remembered from the page that brought
    it, or else *before_server_id*, the one the client stored with the update:
    the remembered ids live only as long as the bridge, so after a restart
    every scroll up in a channel used to answer [] and the client took the
    channel's history for finished. A count above PER_REQUEST is asked in
    pages of it, newest first, at most MAX_REQUESTS of them."""
    ids = server_ids_of(s)
    start = 0
    if before:
        start = ids.of(before) or anchor_server_id(before_server_id)
        if not start:
            return []
    wanted = max(1, int(count))
    jid = jid_from_text(channel)
    pairs, seen = [], set()
    for _request in range(MAX_REQUESTS):
        asked = min(PER_REQUEST, wanted - len(pairs))
        try:
            updates = list(s.client.get_newsletter_messages(jid, asked, int(start)))
        except Exception as error:
            log.info("channel updates refused (%s)", type(error).__name__)
            break
        page = [p for p in updates_to_wpp(channel, updates) if p[0]["id"]["id"] not in seen]
        seen.update(w["id"]["id"] for w, _server_id in page)
        ids.remember(page)
        pairs = page + pairs
        oldest = min((int(u.MessageServerID or 0) for u in updates), default=0)
        if len(updates) < asked or len(pairs) >= wanted or not oldest:
            break
        if start and oldest >= start:
            break       # no progress: the server answered the same window
        start = oldest
    pairs.sort(key=lambda pair: (pair[0]["t"], pair[1]))
    return [w for w, _server_id in pairs]
