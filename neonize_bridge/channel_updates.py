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


def read(s, channel, count, before, jid_from_text):
    """The page get-messages answers for `channel`. Runs on a thread. An
    unknown anchor answers [] (never the newest), as for any other chat."""
    ids = server_ids_of(s)
    start = 0
    if before:
        start = ids.of(before)
        if not start:
            return []
    try:
        updates = s.client.get_newsletter_messages(jid_from_text(channel), int(count), int(start))
    except Exception as error:
        log.info("channel updates refused (%s)", type(error).__name__)
        return []
    pairs = updates_to_wpp(channel, updates)
    ids.remember(pairs)
    return [w for w, _server_id in pairs]
