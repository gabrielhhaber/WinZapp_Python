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
  `primaryHasMore` false);
- the phone already answered this very anchor with nothing new
  (`history_exhausted`, kept in the store): Home goes on asking until the
  phone has nothing more, and stops there (`primaryHasMore` false) until an
  older message gives it a new anchor;
- the same anchor was asked a moment ago: the answer is still on its way, so
  it says `requested` without sending a second one.

WhatsApp Web stops at types 2 and 3 ("Use WhatsApp on your phone to see older
messages"), and so does the bridge (`phoneOnly`), except when the user asked
(Home at the top of the chat: the client sends {"userAsked": true}; the
automatic backfill never does). Then the phone is asked once per anchor, as
TeleZapp asks it: that anchor is marked exhausted as the request goes out,
not when an answer comes, since in that state the phone often answers
nothing at all ("Sync paused", docs/traps/sync-completion.md, issue #220).
A batch that does come brings older messages and so a new anchor, which Home
may ask again; a silent phone costs one notification per anchor, never one
every two minutes. An exhausted anchor of such a chat says `phoneOnly`, so
the top of the conversation says the older messages are on the phone.

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


def verdict(history_end, has_anchor, exhausted=False, user_asked=False):
    """What to answer without sending anything, or None when the phone may be
    asked. `history_end` is the chat's stored endOfHistoryTransferType (None
    when the history never said); `exhausted`, that the current anchor was
    already asked and brought nothing new (or, for types 2/3, was asked at
    all); `user_asked`, that the user pressed Home (types 2/3 only then)."""
    if not has_anchor:
        return {"requested": False, "primaryHasMore": True, "phoneOnly": True}
    phone_only = history_end in PHONE_ONLY_TYPES
    if phone_only and (exhausted or not user_asked):
        return {"requested": False, "primaryHasMore": True, "phoneOnly": True}
    if history_end == NO_MORE_ON_PRIMARY or exhausted:
        return {"requested": False, "primaryHasMore": False, "phoneOnly": False}
    return None


def marks_on_send(history_end) -> bool:
    """Whether the anchor is marked exhausted when the request goes out (types
    2/3: the phone may never answer), not only when an empty answer comes."""
    return history_end in PHONE_ONLY_TYPES


def user_asked_of(body) -> bool:
    """The client's {"userAsked": true}: Home pressed, not the backfill."""
    return isinstance(body, dict) and body.get("userAsked") is True


def brought_nothing_new(rows, already_stored) -> bool:
    """Whether an on-demand answer's rows (chat, message, ...) hold no message
    that `already_stored(id)` says is missing."""
    return not any(not already_stored(row[1]["id"]["id"]) for row in rows)


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


def anchor_jid(chat):
    """The neonize JID of a stored chat key ('123@lid', '1-2@g.us'...)."""
    from neonize.utils import build_jid
    user, _, server = chat.partition("@")
    return build_jid(user, server)


def register_routes(r, *, session, ok, in_thread):
    gate = AnchorGate()

    def state_of(s, jid, user_asked=False):
        keys = s.chat_keys(jid) or []
        anchor = s.db.oldest_real(keys) if keys else None
        exhausted = anchor is not None and anchor["id"] in s.db.history_exhausted(keys)
        history_end = s.db.history_end(keys) if keys else None
        if anchor is not None:
            anchor = dict(anchor, keys=keys, history_end=history_end)
        return anchor, verdict(history_end, anchor is not None, exhausted, user_asked)

    async def request_older(req):
        s = session(req)
        if s.status != "CONNECTED" or s.client is None:
            return ok(status="error", response={
                "requested": False, "primaryHasMore": True, "phoneOnly": False,
                "error": "not connected"})
        try:
            body = await req.json() if req.can_read_body else {}
        except Exception:
            body = {}
        anchor, answer = await in_thread(state_of, s, req.match_info["jid"], user_asked_of(body))
        if answer is not None:
            return ok(status="success", response=answer)
        ask = getattr(s.client, "request_history_sync", None)
        if not callable(ask):
            # Refused before any send: "missing from" is one of the words
            # backfill.py reads as a definite refusal, not an ambiguous one.
            return ok(status="error", response={
                "requested": False, "primaryHasMore": True, "phoneOnly": False,
                "error": "request_history_sync missing from this neonize build"})
        # Per account: two accounts of one bridge can hold the same message of
        # the same group, and each one's phone has to be asked.
        key = (s.name, anchor["chat"], anchor["id"])
        if not gate.take(key, time.monotonic()):
            return ok(status="success", response={
                "requested": True, "primaryHasMore": True, "phoneOnly": False})

        def send():
            if marks_on_send(anchor["history_end"]):
                # before the send: a send that fails waiting for the server
                # may still have lit up the phone
                s.db.mark_history_exhausted(anchor["keys"], anchor["id"])
            ask(anchor_jid(anchor["chat"]), anchor["id"], anchor["fromMe"], anchor["ts"], PAGE)
        try:
            await in_thread(send)
        except Exception as error:
            # A send that failed waiting for the server may still have reached
            # the phone, which then already showed its notification: the anchor
            # stays taken and `requested` is left out, so the client keeps its
            # reply grace (an ambiguous outcome) and does not ask again at once.
            log.info("older history request unconfirmed: %s", type(error).__name__)
            return ok(status="error", response={
                "primaryHasMore": True, "phoneOnly": False, "error": "send_unconfirmed"})
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
