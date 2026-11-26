"""Asking the phone for the conversations its pairing transfer left out.

An iPhone caps the INITIAL_BOOTSTRAP block of a pairing at 399 chats (it was
699 until October 2026), and the RECENT and FULL blocks that follow only carry
messages of chats the bootstrap listed, plus a few more. A user with ~1,000
chats got the 359 most recent ones in the main list (the newest message of the
oldest was from 2026-07-01) and the rest never came, even asking the phone for
the whole history and re-pairing three times (reported 2026-11-21).

`omitted_history.py` handles the phone that tells the older chats apart in
`OmittedConversationMetadata` batches. That phone did not send one: the log of
that report has no such batch in any of its pairings. So, when the bootstrap
came full and no omitted batch followed, the bridge asks for the rest with
WhatsApp's own request for more conversations
(`PeerDataOperationRequestType.RECENT_CONVERSATIONS_ON_DEMAND_REQUEST`, 15,
carrying a `RecentConversationsOnDemandRequest`). The phone answers with
HistorySync blocks of type RECENT_CONVERSATIONS_ON_DEMAND (9), which
`Session._store_history_block` stores like any block of conversations.

Experimental: no phone was at hand to try the request, so what it answers is
logged ("recent conversations answer"). It is sent once the pairing's transfer
is complete (the phone sends one thing at a time and only with WhatsApp open
on its screen), at most MAX_ATTEMPTS times, and until it is answered or given
up the history is not called complete (history_progress.py), so the client's
first sync lists the chats that come with the answer.

The pure parts (Tracker, request, new_chat_previews) are tested in
test_recent_conversations.py.
"""
import logging
import threading
import time

import history_progress

log = logging.getLogger("bridge")

#: PeerDataOperationRequestType.RECENT_CONVERSATIONS_ON_DEMAND_REQUEST
PEER_OPERATION = 15
#: HistorySync.HistorySyncType.RECENT_CONVERSATIONS_ON_DEMAND
SYNC_TYPE = 9
#: HistorySync.HistorySyncType.INITIAL_BOOTSTRAP
BOOTSTRAP = 0
#: conversations an INITIAL_BOOTSTRAP carries when the phone cut it: 399 in the
#: log line "history: 399 chats" of every pairing measured (2026-11-17 and
#: 2026-11-25), status@broadcast and community containers included. The first
#: version counted only the chats of the list (398 or fewer) against 399, so it
#: never asked: the 2026-11-25 pairings (build 3.0.0.5977alpha) wrote no
#: "recent conversations" line. A little below the cap, in case the phone
#: shaves it again.
BOOTSTRAP_CAP = 390
#: conversations and messages of each asked for: more than any account has, the
#: phone sends what it holds
CONVERSATIONS = 2000
MESSAGES_PER_CONVERSATION = 50
#: seconds between two requests while the phone has not answered
RETRY_EVERY = 90
#: requests per pairing
MAX_ATTEMPTS = 3
#: seconds after the transfer completed to stop waiting for an answer
GIVE_UP = MAX_ATTEMPTS * RETRY_EVERY
#: seconds between two looks at whether to ask
WATCH_EVERY = 15


class Tracker:
    """What the pairing's blocks said about the chats left out, and the
    requests made since. The clock comes in as a parameter."""

    def __init__(self):
        self._lock = threading.Lock()
        self.bootstrap_chats = None     # chats of the INITIAL_BOOTSTRAP block
        self.omitted_seen = False       # the phone sent OmittedConversationMetadata
        self.answers = 0                # blocks of type 9 stored
        self.attempts = 0
        self.last_attempt = None
        self.due_since = None           # when the transfer completed and the asking began

    def bootstrap(self, chats):
        """A pairing's INITIAL_BOOTSTRAP came: a new pairing starts over."""
        with self._lock:
            self.bootstrap_chats = int(chats)
            self.omitted_seen = False
            self.answers = 0
            self.attempts = 0
            self.last_attempt = None
            self.due_since = None

    def omitted(self):
        with self._lock:
            self.omitted_seen = True

    def answered(self):
        with self._lock:
            self.answers += 1

    def _wanted(self):
        return (self.bootstrap_chats is not None and self.bootstrap_chats >= BOOTSTRAP_CAP
                and not self.omitted_seen and self.answers == 0)

    def wanted(self):
        """Whether this pairing's chats came cut, with no other way to the rest
        and no answer yet."""
        with self._lock:
            return self._wanted()

    def next_request(self, now, transfer_done):
        """True when the phone is to be asked now, counting the attempt."""
        with self._lock:
            if not self._wanted() or not transfer_done:
                return False
            if self.due_since is None:
                self.due_since = now
            if self.attempts >= MAX_ATTEMPTS:
                return False
            if self.last_attempt is not None and now - self.last_attempt < RETRY_EVERY:
                return False
            self.attempts += 1
            self.last_attempt = now
            return True

    def pending(self, now):
        """Whether the history is still to be held incomplete: the chats came
        cut and nothing answered, until GIVE_UP seconds after the asking began."""
        with self._lock:
            if not self._wanted():
                return False
            if self.due_since is None:
                self.due_since = now        # the hold has a deadline even if nobody asks
            return now - self.due_since < GIVE_UP


def tracker_of(s):
    """The session's Tracker (created on first use)."""
    if getattr(s, "_recent_conversations", None) is None:
        s._recent_conversations = Tracker()
    return s._recent_conversations


def is_answer(data) -> bool:
    """Whether a history block (HistorySync) answers the request for more conversations."""
    try:
        return int(getattr(data, "syncType", -1)) == SYNC_TYPE
    except (TypeError, ValueError):
        return False


def note_block(s, data, omitted_batch=False):
    """Called for every block of the pairing's history before it is stored."""
    tracker = tracker_of(s)
    if omitted_batch:
        tracker.omitted()
    elif int(getattr(data, "syncType", -1)) == BOOTSTRAP:
        tracker.bootstrap(len(getattr(data, "conversations", ())))


def request():
    """The peer message asking the phone for the rest of its conversations."""
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message, ProtocolMessage
    message = Message()
    protocol = message.protocolMessage
    protocol.type = ProtocolMessage.PEER_DATA_OPERATION_REQUEST_MESSAGE
    operation = protocol.peerDataOperationRequestMessage
    operation.peerDataOperationRequestType = PEER_OPERATION
    asked = operation.recentConversationsOnDemandRequest
    asked.conversationCount = CONVERSATIONS
    asked.messagesPerConversation = MESSAGES_PER_CONVERSATION
    return message


def new_chat_previews(chats, rows, known):
    """The newest message of each chat that was not stored before this block and
    is not archived: what the client is told, as a history message, to show the
    chat in its list when it already finished listing the chats. An archived
    chat is left to the client's next list of the chats, which carries its
    state; a message here would put it in the main list.
    `chats`: (jid, fields) as stored; `rows`: (jid, wpp message, media record);
    `known`: the jids that were stored."""
    fresh = {jid for jid, fields in chats
             if jid not in known and not fields.get("archived")}
    newest = {}
    for jid, w, _raw in rows:
        if jid not in fresh:
            continue
        at = _timestamp(w)
        if jid not in newest or at >= newest[jid][0]:
            newest[jid] = (at, w)
    return [w for _at, w in newest.values()]


def _timestamp(w):
    try:
        return int(w.get("messageTimestamp") or w.get("timestamp") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0


def ask_once(s, now):
    """Asks the phone for the rest of its conversations when this pairing's
    chats came cut and the transfer is complete. True when a request went out."""
    tracker = tracker_of(s)
    client = getattr(s, "client", None)
    if not tracker.wanted():
        return False
    try:
        stored = s.db.counts()["chat"]
    except Exception:
        stored = 0
    done = history_progress.progress_of(s, now).complete(now, stored)
    if not tracker.next_request(now, done) or client is None:
        return False
    try:
        client.send_peer_message(request())
    except Exception as error:
        log.info("recent conversations request not sent: %s", type(error).__name__)
        return False
    log.info("recent conversations: the pairing listed %s chats; asked the phone "
             "for the rest (attempt %d of %d)", tracker.bootstrap_chats,
             tracker.attempts, MAX_ATTEMPTS)
    return True


def watch(s, sleep=time.sleep, clock=time.monotonic):
    """While the session is connected, asks whenever it is time. One loop per
    session, even across reconnections."""
    if getattr(s, "_recent_conversations_watch", False):
        return
    s._recent_conversations_watch = True
    try:
        while s.status == "CONNECTED":
            sleep(WATCH_EVERY)
            try:
                ask_once(s, clock())
            except Exception:
                log.exception("recent conversations")
    finally:
        s._recent_conversations_watch = False
