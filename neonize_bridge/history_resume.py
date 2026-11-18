"""Where the phone's history transfer stands, and picking it up when it stops.

Reported 2026-11-17 (iPhone, newly paired asking for a long period): the phone
sent the FULL history in blocks of ~5,000 messages, one every ~4 s, and
stopped at 40% three minutes after pairing, while the phone said "Não foi
possível completar a sincronização". The bridge had stored every block it got
without an error and the client had synced every chat: the phone itself
stopped sending. An iPhone only sends while WhatsApp is open on its screen;
the screen locking cuts the transfer, and opening WhatsApp again two minutes
later did not start it again.

So, from here:
- history-sync-status says how far the transfer got and whether it stalled
  (`historyTransfer`), and the client tells the user to open WhatsApp on the
  phone and keep it open (client/main_window/history_transfer.py);
- while the transfer is stalled the bridge asks the phone for the next block
  with WhatsApp's own request for a block that did not arrive
  (HISTORY_SYNC_CHUNK_RETRY), every RETRY_EVERY seconds and at most
  MAX_ATTEMPTS times per stall; an answer saying the session or the block is
  gone stops the asking. Experimental: whether a phone answers it for a
  transfer it gave up on is not known yet, so its answer is logged
  ("history resume answer").

Only RECENT and FULL come in numbered blocks with a progress; the others
(INITIAL_BOOTSTRAP, PUSH_NAME, NON_BLOCKING_DATA, ON_DEMAND) leave the
transfer as it was.

The pure parts (Tracker, request, answer_of) are tested in
test_history_resume.py.
"""
import logging
import threading
import time

log = logging.getLogger("bridge")

#: HistorySync.HistorySyncType values that come in numbered blocks
TRANSFER_TYPES = {2: "FULL", 3: "RECENT"}
#: seconds without a block, below 100%, to call the transfer stalled. The
#: blocks came every ~4 s in the report; 60 s is many blocks missed.
STALL = 60
#: seconds between two requests to the phone while stalled
RETRY_EVERY = 90
#: requests per stall (about half an hour at RETRY_EVERY)
MAX_ATTEMPTS = 20
#: PeerDataOperationRequestType.HISTORY_SYNC_CHUNK_RETRY
CHUNK_RETRY = 10
#: HistorySyncChunkRetryResponseCode: the phone has nothing more to send
FINAL_CODES = {4: "SESSION_EXHAUSTED", 5: "CHUNK_EXHAUSTED"}
CODE_NAMES = {1: "GENERATION_ERROR", 2: "CHUNK_CONSUMED", 3: "TIMEOUT",
              6: "DUPLICATED_REQUEST", **FINAL_CODES}
#: seconds between two looks at the transfer while connected
WATCH_EVERY = 15


class Tracker:
    """The last numbered block the phone sent and the requests made since.
    The clock comes in as a parameter."""

    def __init__(self):
        self._lock = threading.Lock()
        self.kind = None            # TRANSFER_TYPES key of the last block
        self.chunk = None           # its chunkOrder
        self.percent = None         # its progress (0-100)
        self.last = None            # when it was stored
        self.attempts = 0           # requests since that block
        self.last_attempt = None
        self.refused = False        # the phone said it has nothing more

    def block(self, kind, chunk, percent, now):
        """A block of history was stored. Other types are ignored."""
        if kind not in TRANSFER_TYPES:
            return
        with self._lock:
            self.kind, self.chunk, self.percent, self.last = kind, chunk, percent, now
            self.attempts, self.last_attempt, self.refused = 0, None, False

    def _stalled(self, now):
        return (self.percent is not None and self.percent < 100
                and now - self.last >= STALL)

    def stalled(self, now):
        with self._lock:
            return self._stalled(now)

    def next_request(self, now):
        """(sync type, chunk order) to ask the phone for now, counting the
        attempt; None when nothing is to be asked."""
        with self._lock:
            if not self._stalled(now) or self.refused or self.attempts >= MAX_ATTEMPTS:
                return None
            if self.last_attempt is not None and now - self.last_attempt < RETRY_EVERY:
                return None
            self.attempts += 1
            self.last_attempt = now
            return self.kind, self.chunk + 1

    def answered(self, code, can_recover):
        """The phone answered a request: a final code, or canRecover=False,
        stops the asking until the next block."""
        if code in FINAL_CODES or can_recover is False:
            with self._lock:
                self.refused = True

    def status(self, now):
        """The `historyTransfer` of history-sync-status: None before the first
        numbered block."""
        with self._lock:
            if self.percent is None:
                return None
            return {"type": TRANSFER_TYPES[self.kind], "progress": self.percent,
                    "stalled": self._stalled(now), "resumeAttempts": self.attempts,
                    "resumeRefused": self.refused}


def request(kind, chunk):
    """The peer message asking the phone for block `chunk` of `kind` again."""
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message, ProtocolMessage
    message = Message()
    protocol = message.protocolMessage
    protocol.type = ProtocolMessage.PEER_DATA_OPERATION_REQUEST_MESSAGE
    operation = protocol.peerDataOperationRequestMessage
    operation.peerDataOperationRequestType = CHUNK_RETRY
    retry = operation.historySyncChunkRetryRequest
    retry.syncType = kind
    retry.chunkOrder = chunk
    retry.regenerateChunk = True
    return message


def answer_of(message):
    """(response code, canRecover, chunk order) of the phone's answer to a
    block request, each None when absent; None when `message` is not one."""
    if message is None or not message.HasField("protocolMessage"):
        return None
    protocol = message.protocolMessage
    if not protocol.HasField("peerDataOperationRequestResponseMessage"):
        return None
    response = protocol.peerDataOperationRequestResponseMessage
    if response.peerDataOperationRequestType != CHUNK_RETRY:
        return None
    for result in response.peerDataOperationResult:
        if result.HasField("historySyncChunkRetryResponse"):
            retry = result.historySyncChunkRetryResponse
            return (int(retry.responseCode) if retry.HasField("responseCode") else None,
                    bool(retry.canRecover) if retry.HasField("canRecover") else None,
                    int(retry.chunkOrder) if retry.HasField("chunkOrder") else None)
    return None, None, None


# ── The session side ─────────────────────────────────────────────────────────

def tracker_of(s):
    """The session's Tracker (created on first use)."""
    if getattr(s, "_history_resume", None) is None:
        s._history_resume = Tracker()
    return s._history_resume


def note_block(s, data, now):
    """Called for every stored block of the pairing's history."""
    tracker_of(s).block(int(data.syncType), int(data.chunkOrder), int(data.progress), now)


def status_of(s, now):
    """The fields history-sync-status adds."""
    return {"historyTransfer": tracker_of(s).status(now)}


def received(s, message, from_me):
    """Logs the phone's answer to a block request; True when `message` was one
    (it is not a chat message)."""
    if not from_me:
        return False
    answer = answer_of(message)
    if answer is None:
        return False
    code, can_recover, chunk = answer
    log.info("history resume answer: block %s, %s, canRecover=%s",
             chunk, CODE_NAMES.get(code, code), can_recover)
    tracker_of(s).answered(code, can_recover)
    return True


def ask_once(s, now):
    """Asks the phone for the next block when the transfer is stalled. True
    when a request went out."""
    tracker = tracker_of(s)
    wanted = tracker.next_request(now)
    client = getattr(s, "client", None)
    if wanted is None or client is None:
        return False
    kind, chunk = wanted
    try:
        client.send_peer_message(request(kind, chunk))
    except Exception as error:
        log.info("history resume request not sent: %s", type(error).__name__)
        return False
    log.info("history stalled at %s%%: asked the phone for %s block %d (attempt %d of %d)",
             tracker.percent, TRANSFER_TYPES[kind], chunk, tracker.attempts, MAX_ATTEMPTS)
    return True


def watch(s, sleep=time.sleep, clock=time.monotonic):
    """While the session is connected, asks for the next block whenever the
    transfer stalls. One loop per session, even across reconnections."""
    if getattr(s, "_history_resume_watch", False):
        return
    s._history_resume_watch = True
    try:
        while s.status == "CONNECTED":
            sleep(WATCH_EVERY)
            try:
                ask_once(s, clock())
            except Exception:
                log.exception("history resume")
    finally:
        s._history_resume_watch = False
