"""Older messages from the phone: `POST request-older-messages/:jid` and
`GET older-history-state/:jid`, as WinZapp's WPPConnect API answers them.

A linked device only gets a window of each chat at pairing. WhatsApp Web asks
the phone for the rest ("click here to get older messages from your phone")
with a peer message anchored on the oldest message it holds; the phone answers
minutes later with a history block of type ON_DEMAND. neonize 0.6.0 sends that
request (`request_history_sync`); the answer reaches `Session._store_history`
like any other block, and the client, which keeps asking `get-messages` before
its oldest message (MainWindow.wait_for_older_messages), finds it there.

Every request lights up the phone's lock screen ("Syncing WhatsApp...", and
"Sync paused" when it yields nothing: issue #108), so the bridge only sends
one when it can be answered:

- no message of the chat is stored: there is nothing to anchor on, and only
  the phone has the chat (`phoneOnly`);
- the history said the chat ends here (`endOfHistoryTransferType` 1:
  `primaryHasMore` false), or that this device may not ask for more (2 and 3,
  WhatsApp Web's "Use WhatsApp on your phone to see older messages":
  `phoneOnly`);
- the same anchor was asked a moment ago: the answer is still on its way, so
  it says `requested` without sending a second one.

The GET route gives the same verdict and never sends. The pure functions are
at the top (tested in test_older_history.py).
"""
import logging
import threading
import time

log = logging.getLogger("bridge")

#: Conversation.EndOfHistoryTransferType
NO_MORE_ON_PRIMARY = 1
PHONE_ONLY_TYPES = (2, 3)

#: messages asked for per request (WhatsApp Web asks for 50)
PAGE = 50

#: how long the same anchor is not asked again (the phone answers in minutes)
SAME_ANCHOR_WINDOW = 120.0


#: HistorySync.HistorySyncType.ON_DEMAND
ON_DEMAND = 6


def is_on_demand(data) -> bool:
    """Whether a history block (HistorySync) answers an older-history request."""
    try:
        return int(getattr(data, "syncType", -1)) == ON_DEMAND
    except (TypeError, ValueError):
        return False


def verdict(history_end, has_anchor):
    """What to answer without sending anything, or None when the phone may be
    asked. `history_end` is the chat's stored endOfHistoryTransferType (None
    when the history never said)."""
    if not has_anchor:
        return {"requested": False, "primaryHasMore": True, "phoneOnly": True}
    if history_end == NO_MORE_ON_PRIMARY:
        return {"requested": False, "primaryHasMore": False, "phoneOnly": False}
    if history_end in PHONE_ONLY_TYPES:
        return {"requested": False, "primaryHasMore": True, "phoneOnly": True}
    return None


class AnchorGate:
    """Remembers the anchors just asked, so a client that asks again while the
    phone's answer is on its way does not send a second request."""

    def __init__(self, window=SAME_ANCHOR_WINDOW):
        self.window = window
        self._asked = {}
        self._lock = threading.Lock()

    def take(self, key, now):
        """True when `key` (chat, anchor id) may be asked now; it is then
        remembered. False while the same key was asked within the window."""
        with self._lock:
            for old in [k for k, at in self._asked.items() if now - at >= self.window]:
                del self._asked[old]
            if key in self._asked:
                return False
            self._asked[key] = now
            return True

    def forget(self, key):
        with self._lock:
            self._asked.pop(key, None)


def anchor_jid(chat):
    """The neonize JID of a stored chat key ('123@lid', '1-2@g.us'...)."""
    from neonize.utils import build_jid
    user, _, server = chat.partition("@")
    return build_jid(user, server)


def register_routes(r, *, session, ok, in_thread):
    gate = AnchorGate()

    def state_of(s, jid):
        keys = s.chat_keys(jid) or []
        anchor = s.db.oldest_real(keys) if keys else None
        return anchor, verdict(s.db.history_end(keys) if keys else None, anchor is not None)

    async def request_older(req):
        s = session(req)
        if s.status != "CONNECTED" or s.client is None:
            return ok(status="error", response={
                "requested": False, "primaryHasMore": True, "phoneOnly": False,
                "error": "not connected"})
        anchor, answer = await in_thread(state_of, s, req.match_info["jid"])
        if answer is not None:
            return ok(status="success", response=answer)
        key = (anchor["chat"], anchor["id"])
        if not gate.take(key, time.monotonic()):
            return ok(status="success", response={
                "requested": True, "primaryHasMore": True, "phoneOnly": False})

        def send():
            s.client.request_history_sync(anchor_jid(anchor["chat"]), anchor["id"],
                                          anchor["fromMe"], anchor["ts"], PAGE)
        try:
            await in_thread(send)
        except Exception as error:
            gate.forget(key)
            log.info("older history request not sent: %s", type(error).__name__)
            return ok(status="error", response={
                "requested": False, "primaryHasMore": True, "phoneOnly": False,
                "error": type(error).__name__})
        log.info("older history requested from the phone (%d messages)", PAGE)
        return ok(status="success", response={
            "requested": True, "primaryHasMore": True, "phoneOnly": False})

    async def older_state(req):
        s = session(req)
        _, answer = await in_thread(state_of, s, req.match_info["jid"])
        return ok(status="success", response=answer or {
            "requested": False, "primaryHasMore": True, "phoneOnly": False})

    r.add_post("/api/{tok}/request-older-messages/{jid}", request_older)
    r.add_get("/api/{tok}/older-history-state/{jid}", older_state)
