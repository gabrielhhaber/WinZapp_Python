"""Pure message/unread/history rules shared by the sync and event mixins.

Everything here takes plain dicts and lists and is tested directly — the
shape to copy when logic is pulled off MainWindow. Moved verbatim out of
main.py; main.py re-exports every name.
"""

import logging
import threading
from core.call_log import (
    CALL_LOG_MESSAGE_TYPE,
    LEGACY_CALL_LOG_TYPE,
)
from core.incremental_sync import timestamp_seconds as _timestamp_seconds
from core.remote_deletions import message_timestamp_seconds


class MediaExpiredError(Exception):
    """CDN URL for this media has expired (HTTP 403 or 410 from WhatsApp)."""


# Hard cap on how many messages stay resident in a chat's in-memory records
# list. Without this, a chat that stays active across a long-running session
# (this is a tray app — restarts are rare) grows records forever since
# on_new_message()/on_historical_message() only ever append. The initial
# sync already bounds itself to messages_page_size (default 200) per chat,
# so this only kicks in for chats that keep receiving messages well past
# that after sync — trimming the oldest ones out of RAM (they're still on
# disk in SQLite, just not resident).
_MAX_RESIDENT_MESSAGES_PER_CHAT = 1000


# Message types that are WhatsApp/system-generated rather than something a
# person actually sent, even though they ARE worth showing as the chat-list
# preview text once you're already looking at the list (a revoke reads
# "Mensagem apagada"; a join/leave reads "Fulano entrou no grupo") — see
# MainWindow._PREVIEW_MESSAGE_TYPES/_counts_as_last_message(), the allowlist
# this function builds on. is_countable_message() is strictly narrower than
# that allowlist: these two must ALSO never bump the chat-list sort
# timestamp, inflate the unread badge, or fire a notification, purely
# because a group's metadata changed or someone's own revoke arrived weeks
# after everyone stopped talking in that chat.
# A call record (core/call_log.py) is on the list too: it decides the chat's
# preview and position like WhatsApp's own list, but never mints an unread badge
# or a "new message" toast -- the incoming-call alert already told the user.
_PREVIEW_ONLY_MESSAGE_TYPES = frozenset({
    "protocolMessage", "groupNotification", CALL_LOG_MESSAGE_TYPE, LEGACY_CALL_LOG_TYPE,
})


#: Bytes per second assumed when turning a media file's declared size into a
#: read timeout. Deliberately pessimistic: this is not a throughput estimate,
#: it is the answer to "how long may the server be SILENT before we conclude it
#: is never going to answer". WPPConnect downloads the whole file from
#: WhatsApp's CDN and decrypts it before writing a single byte back, so the
#: silence lasts as long as that takes, and a 200 MB document on a slow line
#: takes far longer than the flat 60s every media request used to get.
_MEDIA_ASSUMED_BYTES_PER_SECOND = 200 * 1024

#: Never wait longer than this for one media request, however large the file
#: claims to be. A declared size is attacker-controlled in principle and
#: wrong-by-accident in practice, and a request that hangs forever is a worker
#: thread that never comes back.
_MEDIA_FETCH_TIMEOUT_CEILING = 30 * 60


def media_fetch_timeout(msg: dict, base: int = 60) -> int:
    """How long to let one media download go quiet, given its declared size.

    A flat 60s is right for a photo and hopeless for a 200 MB document: the
    request is abandoned while the server is still fetching it, the user is
    told the download failed, and the server keeps working on a request nobody
    is reading any more — which is the memory that fills. Reported as "it says
    it is downloading, takes forever, downloads nothing, fills the RAM, and the
    button goes back to Download".

    Falls back to `base` whenever the size is missing or unparseable, so a
    message that declares nothing behaves exactly as before.
    """
    inner = (msg or {}).get("message")
    if not isinstance(inner, dict):
        return base
    size = None
    for value in inner.values():
        if isinstance(value, dict) and value.get("fileLength") is not None:
            size = value.get("fileLength")
            break
    try:
        size = int(size)
    except (TypeError, ValueError):
        return base
    if size <= 0:
        return base
    return int(min(_MEDIA_FETCH_TIMEOUT_CEILING,
                   max(base, base + size / _MEDIA_ASSUMED_BYTES_PER_SECOND)))


def is_countable_message(msg: dict) -> bool:
    """True for a message type that should count as real conversation
    activity (unread badge, chat-list sort order, notifications).

    Deliberately built ON TOP of MainWindow._counts_as_last_message()
    (a real-content ALLOWLIST, not a blocklist of known-bad types) rather
    than keeping a second, separately-maintained list: WPPConnect/Baileys
    keep surfacing new WhatsApp-internal system message types
    (e2e_notification, notification_template, "unknown", ...) that carry no
    real content, and two independently-maintained lists are exactly how
    one of them silently missed one — is_countable_message() used to keep
    its own short blocklist, which only excluded groupNotification/
    protocolMessage, so an e2e_notification arriving for a chat nobody had
    messaged in months still bumped it to the top of the list with a
    phantom "1 unread" and nothing to show when opened, and fired a toast
    reading "Nova mensagem de <raw @lid digits>: Mensagem incompatível" —
    a type format_notification_body() had no way to describe. Deriving from
    the same allowlist the preview/sort code already trusts means a type
    only has to be taught to one place to be handled correctly everywhere.
    """
    from main_window.chat_list import ChatListMixin
    if not ChatListMixin._counts_as_last_message(msg):
        return False
    return msg.get("messageType") not in _PREVIEW_ONLY_MESSAGE_TYPES


def _discount_non_countable_unread(records: list, unread_count: int) -> int:
    """Discount from a server-reported unread count the tail messages that
    must never count toward the badge: our own (fromMe) sends — WhatsApp Web
    sometimes counts those — and system events (groupNotification,
    protocolMessage, e2e_notification, ...) that is_countable_message()
    excludes.

    This is the mirror of the app's own counting rule: on_new_message()
    only ever increments the badge for countable messages, so a server that
    counts a promote/join/leave (or any other system event) toward unread
    would otherwise mint a phantom badge on a chat that has no real unread
    message in it — observed live with a group promote that appeared as "1
    não lida" while the conversation held nothing new. The tail inspected is
    the locally-stored record list, which keeps the exact shape the
    on_new_message() increment logic itself saw.

    Both halves of the predicate matter and neither can be dropped: a chat
    whose tail is [groupNotification, our own reply] has no unread message at
    all, and either rule alone still reports one.

    Reactions are left out of the tail before it is cut, not discounted in it.
    WinZapp stores each one as a record of its own (`_rxn_...`) to decorate the
    message it points at, but WhatsApp never counted it as unread -- so it
    cannot take the place of a message that was. Measured 2026-09-23: a group
    with 1 unread text received four reactions; the tail of 1 was a reaction,
    was discounted, and the badge went to 0 with the text still unread.
    """
    if unread_count <= 0 or not records:
        return unread_count
    records = [m for m in records
               if not (isinstance(m, dict) and m.get("messageType") == "reactionMessage")]
    if not records:
        return unread_count
    tail = records[-unread_count:] if unread_count <= len(records) else records
    discount = sum(
        1 for m in tail
        if (isinstance(m, dict) and (m.get("key") or {}).get("fromMe"))
        or not is_countable_message(m)
    )
    return max(0, unread_count - discount)



# Media the server could not produce because WhatsApp Web no longer holds that
# message, counted for the summary _report_media_fetch_failure() emits.
_media_not_in_store_lock = threading.Lock()
_media_not_in_store = 0
# How often the running total is repeated once the first one has been reported.
_MEDIA_MISSING_LOG_EVERY = 25


# How many consecutive rounds an incremental delta may come back empty for a
# chat whose activity marker moved before the marker is accepted anyway. See
# sync_chat_messages(): a bump with nothing fetchable behind it is a permanent
# state for some chats, so this has to terminate.
_MAX_EMPTY_DELTA_RETRIES = 3

# How many consecutive rounds the store may answer chat_not_found for a chat
# before that chat stops being queried every round. See sync_chat_messages():
# a JID that only ever appeared in an encryption housekeeping event has no
# chat behind it and never will, but a chat CAN come into existence later, so
# this is a small budget rather than either extreme.
_MAX_ABSENT_CHAT_RETRIES = 3


def _report_media_fetch_failure(msg_id: str, status_code: int, body: str) -> bool:
    """Log a "message not found" media failure as a tally. True when handled.

    This is not a WinZapp bug and not a transient error, which is exactly why
    it deserves different treatment from every other failure here: asking for
    the media of a message WhatsApp Web has unloaded from its Store cannot
    succeed. The Node side already exhausts a deep recovery chain before saying
    so — the original id, the id without its device-port suffix, the cleaned
    id, loadEarlierMessages(), getMessages(count: 100), and finally a LID/phone
    JID resolution (see getMediaByMessage in sessionController.ts).

    Measured on one live sync: 106 of 1,110 media requests ended here, and each
    wrote two lines, so a perfectly healthy session buried its log under 212
    warnings about something nobody can act on. That matters because log.log is
    what users send when something is actually wrong, and it is truncated on
    every launch — noise here costs real diagnosis later.

    The count still surfaces: the first one explains itself, and the running
    total is repeated every _MEDIA_MISSING_LOG_EVERY after that, so a session
    where suddenly EVERY message is missing still looks different from a normal
    one, which a silent counter would have hidden.
    """
    global _media_not_in_store
    if status_code != 400 or "not found" not in (body or "").lower():
        return False
    with _media_not_in_store_lock:
        _media_not_in_store += 1
        total = _media_not_in_store
    if total == 1:
        logging.warning(
            "[get_base64_from_media] %s: WhatsApp Web no longer holds this "
            "message, so its media cannot be fetched (the server already tried "
            "loading the chat's history). Further occurrences are counted, and "
            "the running total is logged every %d.",
            msg_id, _MEDIA_MISSING_LOG_EVERY,
        )
    elif total % _MEDIA_MISSING_LOG_EVERY == 0:
        logging.warning(
            "[get_base64_from_media] %d media unavailable so far this run "
            "(message no longer in WhatsApp Web's store); latest: %s",
            total, msg_id,
        )
    else:
        logging.info(
            "[get_base64_from_media] media unavailable for %s (#%d).",
            msg_id, total,
        )
    return True


def media_not_in_store_count() -> int:
    """How many media were unavailable this run. For tests and diagnostics."""
    with _media_not_in_store_lock:
        return _media_not_in_store


def reset_media_not_in_store_count() -> None:
    """Forget the tally. Exists for tests."""
    global _media_not_in_store
    with _media_not_in_store_lock:
        _media_not_in_store = 0


def describe_history_sync_health(status) -> str:
    """The half of /history-sync-status that says *why* nothing is arriving.

    The progress lines below already report how much has landed (messages,
    chats, unprocessed, recentCompleted, initialSyncComplete). All of those
    read zero/False both when history is merely slow and when WhatsApp Web
    cannot ingest history at all, so on their own they cannot tell a first
    pairing on a big account from a session that is wedged — which is exactly
    the ambiguity a captured log was read against and lost to.

    The endpoint has answered ``backendWorkerBridgeReady`` since it was written
    (see getHistorySyncStatus in api_patches/src/controller/deviceController.ts,
    where it is documented as *the* field that matters) and nothing on this
    side ever wrote it down. False there means the chunk decoder is not
    running and no amount of waiting will help; the page/version fields next to
    it are the newer hypotheses about why.

    Every field is optional on purpose: a WPPConnect Server that predates them
    simply omits them, and this must stay readable rather than raise.
    """
    if not isinstance(status, dict):
        return "no status"
    counts = status.get("storeCounts")
    if not isinstance(counts, dict):
        counts = {}
    sw = status.get("serviceWorker")
    if isinstance(sw, dict):
        sw = sw.get("state") or "controlling"
    return (
        "bridge={} stored_chunks={} chunk_status={} page={}/focus={} "
        "web={} sw={}".format(
            status.get("backendWorkerBridgeReady", "?"),
            counts.get("history-sync-notification", "?"),
            status.get("chunkStatus", "?"),
            status.get("pageVisibility", "?"),
            status.get("pageHasFocus", "?"),
            status.get("webVersion", "?"),
            sw if sw is not None else "none",
        )
    )


#: Set on a chat whose unreadCount is the server's raw number, stored while the
#: chat held no messages to discount it against. Absent means the stored count
#: has already been through _discount_non_countable_unread() (or was counted
#: locally, which only ever counts countable messages).
_UNREAD_UNDISCOUNTED = "_unread_undiscounted"


def note_unread_discount_state(chat: dict, discounted: bool) -> None:
    """Record whether the unreadCount just stored on *chat* was discounted.

    Whoever stores a server count calls this. *discounted* is False when the
    chat held no records for _discount_non_countable_unread() to look at: the
    count is raw and apply_history_sync_unread_correction() owes it exactly one
    discount later. True makes it final.
    """
    if discounted:
        chat.pop(_UNREAD_UNDISCOUNTED, None)
    else:
        chat[_UNREAD_UNDISCOUNTED] = True


def records_cover_snapshot(records, snapshot_t) -> bool:
    """True when *records* reach the chat-list snapshot's last activity.

    The list-chats merge discounts a server count against the chat's stored
    tail, and that is only the right tail when it holds what the server
    counted. On a warm start the records are what the database had at launch:
    a message that arrived while WinZapp was closed (an own voice note sent
    from the phone, a group promote) is in the server's count and not in the
    tail, so discounting there subtracts the wrong messages and, marked final,
    skipped the post-fetch correction that would have found the right ones.
    A tail is current when its newest record is at least as new as the
    snapshot's `t`; a snapshot without a usable `t` cannot say otherwise.
    """
    if not records:
        return False
    snapshot = _timestamp_seconds(snapshot_t or 0)
    if not snapshot:
        return True
    newest = max((message_timestamp_seconds(r) for r in records), default=0)
    return newest >= snapshot


def apply_history_sync_unread_correction(remote_jid: str, chat: dict) -> bool:
    """Re-discount a chat's badge now that its messages have been fetched.

    get_remote_chats() applies _discount_non_countable_unread() while merging
    the list-chats snapshot, but at that moment the chat has no messages: the
    snapshot is merged before any get-messages call runs (measured on a live
    sync — chats merged at 21:17:30, first messages answered at 21:17:36). The
    discount returns immediately on its own `not records` guard, so whatever
    the server counted survives intact, including the own (fromMe) sends and
    system events WhatsApp Web sometimes counts toward unread.

    Reported live: a conversation showing "1 mensagem não lida" whose newest
    line was the user's own "Eu: áudio 0:03, Entregue" — no incoming message
    behind the badge at all. No chats-update event was involved; the count came
    straight from the snapshot merge, which is exactly where the correction is
    blind.

    Running the same correction here, with the records finally in hand, closes
    that window. It can only ever subtract, and only tail entries that
    on_new_message() would never have counted itself, so a genuinely unread
    conversation keeps its badge untouched.

    It runs only on a count marked raw (see note_unread_discount_state()), and
    consumes the mark. The discount is not idempotent: it inspects the last N
    records, and on an already-discounted N the shorter window still holds the
    same system events, so it subtracts them again. This used to run on every
    sync of every chat, on top of the discount the list-chats merge and the
    live chats-update had already applied. Diagnosed from a real log.log, a
    group the user watched fall from 68 to 62 with nothing read:

        chats-update in: <group> unread=72 previous=71
        [unread] <group>: 61 -> 67 (previous=71, open=False, read_ack=None).
        [unread] <group>: 67 -> 62 after history sync (...)

    once a minute, the badge see-sawing between the two numbers.

    Returns True when the badge changed.
    """
    records = (chat.get("messages", {})
               .get("messages", {})
               .get("records", []))
    if not records:
        return False
    if not chat.pop(_UNREAD_UNDISCOUNTED, False):
        return False
    before = int(chat.get("unreadCount") or 0)
    corrected = _discount_non_countable_unread(records, before)
    if corrected == before:
        return False
    logging.info(
        "[unread] %s: %s -> %s after history sync (own sends / system events "
        "the chat-list snapshot counted).", remote_jid, before, corrected,
    )
    chat["unreadCount"] = corrected
    return True


def _message_ts(msg: dict) -> int:
    """The timestamp of a message record, whichever key it arrived under."""
    if not isinstance(msg, dict):
        return 0
    try:
        return int(msg.get("messageTimestamp") or msg.get("timestamp") or msg.get("t") or 0)
    except (TypeError, ValueError):
        return 0


def own_message_marks_chat_read(records: list, msg: dict) -> bool:
    """True when our own message *msg* proves the chat was read elsewhere.

    Sending a reply from the phone (or any other linked device) is the
    strongest possible evidence that the chat was read there — WhatsApp
    itself clears the unread count the moment you do it. WinZapp used to
    ignore this entirely: on_new_message() only ever touches the badge
    inside `if not from_me`, and returns outright a few lines later on
    `if from_me`. Reported live as a chat still showing "2 mensagens não
    lidas" while its own last line already read "Eu: áudio 0:06" — the
    reply had been sent from the phone seconds earlier, its echo had
    arrived, and the badge sat there untouched.

    The caller only reaches this for an echo that matched no pending
    virtual message, i.e. one this WinZapp process did not send (a local
    send returns much earlier, see on_new_message). The remaining check is
    recency: a re-delivered *old* message of ours must never clear a badge
    for messages that arrived after it, so this only answers True when our
    message is at least as new as the newest countable message we received.
    A chat with nothing countable received at all has no real unread to
    protect and answers True as well.
    """
    if not isinstance(msg, dict) or not (msg.get("key") or {}).get("fromMe"):
        return False
    own_ts = _message_ts(msg)
    if own_ts <= 0:
        # No usable timestamp — refuse rather than guess. The next chats-update
        # or the 60s list-chats poll still gets a chance to fix the count.
        return False
    own_id = (msg.get("key") or {}).get("id")
    for record in reversed(records or []):
        if not isinstance(record, dict):
            continue
        key = record.get("key") or {}
        if key.get("fromMe"):
            continue
        if own_id and key.get("id") == own_id:
            continue  # msg itself, already appended to records by the caller
        if not is_countable_message(record):
            continue  # system events never counted toward the badge either
        return own_ts >= _message_ts(record)
    return True


def unread_after_history_sync(
    server_unread: int,
    local_unread: int,
    fetched: list,
    local_records: list,
) -> int:
    """Reconcile unread state when history sync discovers offline arrivals.

    The remote chat summary can still report zero while get-messages already
    contains newer messages. Infer only genuinely new incoming content after the
    newest locally known message; never count old backfill or our own messages.
    """
    baseline = max(0, int(server_unread or 0), int(local_unread or 0))
    if not fetched or not local_records:
        return baseline

    newest_local_ts = max((_message_ts(message) for message in local_records), default=0)
    if newest_local_ts <= 0:
        return baseline

    local_ids = {
        (message.get("key") or {}).get("id")
        for message in local_records
        if isinstance(message, dict)
    }
    inferred = 0
    for message in fetched:
        if not isinstance(message, dict) or _message_ts(message) <= newest_local_ts:
            continue
        key = message.get("key") or {}
        message_id = key.get("id")
        if message_id and message_id in local_ids:
            continue
        if key.get("fromMe") or not is_countable_message(message):
            continue
        inferred += 1

    return max(baseline, max(0, int(local_unread or 0)) + inferred)


def reconcile_open_chat_unread(
    server_unread: int,
    local_new: int,
    remote_read_confirmed: bool = False,
) -> tuple:
    """Decide the unread count for a chat the user has OPEN in the panel.

    Returns ``(count, clear_new_since_read)``.

    "Open" is not the same as "read". on_new_message() increments both
    unreadCount and _new_since_read whenever a message lands in the open
    conversation while the window is hidden or minimized (its guard is
    ``if not (_open and _visible)``), and Alt+F4 only hides to tray — so an
    open chat routinely holds a legitimate backlog. Zeroing it blind wipes the
    badge, the window-title counter and the toast count, and the next arrival
    then announces "1 mensagem não lida" for a conversation holding several.

    Both surfaces that reconcile an open chat's count go through this one
    function: the live chats-update event (on_chat_unread_update) and the 60s
    list-chats resync (get_remote_chats). They used to each carry their own
    copy — and the resync's was a bare `= 0` under a comment claiming parity
    with the live one, which is exactly the shape that hides a bug from code
    review. Same reasoning as link_preview_text() and _status_content_label().

    Both callers also decide "is this chat open" the same way, and that test
    includes _unread_anchored_to_local_read(): this function answers for an
    open chat that is also READ, which is not the same thing once a read has
    been undone on screen (see the _open_now comment in
    on_chat_unread_update()). Whichever one of the two you are changing, the
    other has the same condition and has to move with it.

    ``remote_read_confirmed`` says a zero really is somebody reading the chat
    elsewhere rather than an uninformative one. The resync has no
    previousUnreadCount to derive it from, so it passes False — the
    conservative side, the same default _remote_read_confirmed() itself
    documents for an unknown. The cost is explicit: a read performed on the
    phone, for a chat left open here with the window hidden, keeps the badge
    lit until the user focuses the window (only mark_conversation_as_read
    clears _new_since_read).

    min(), never max(), when both are positive: for an OPEN chat the local
    read state is the authoritative one by definition. A server count above
    the local backlog is the lag of a /send-seen the server has not
    acknowledged yet, and taking it resurrects already-read messages into the
    badge and the unread separator — a symptom this file has already fixed
    once (see the read_at_t branch in on_chat_unread_update).
    """
    server_unread = max(0, int(server_unread or 0))
    local_new = max(0, int(local_new or 0))
    if not local_new:
        return 0, False
    if server_unread == 0:
        if remote_read_confirmed:
            # The messages we counted really have been read; the badge goes
            # with them, and so does the tracking entry behind it.
            return 0, True
        return local_new, False
    return min(server_unread, local_new), False


def _log_refused_read_receipt(jid: str, server_unread: int, local_unread: int,
                             incoming_timestamp, local_timestamp,
                             merged: int) -> None:
    """Say so when a snapshot reporting "read" was not allowed to clear a badge.

    The only symptom of this from outside is a conversation that stays unread
    forever and comes back only after F5, and until this line existed the logs
    could not tell whether WPPConnect had reported a stale count or WinZapp had
    refused a good one (issue #173 says exactly that: "the available
    diagnostics do not establish" which).

    Both timestamps go in raw, because the units are the thing under
    suspicion — see _unread_seconds().
    """
    if server_unread != 0 or local_unread <= 0 or merged == 0:
        return
    logging.info(
        "[unread] %s: snapshot says read (0) but kept %d — "
        "snapshot t=%s local t=%s",
        jid, merged, incoming_timestamp, local_timestamp,
    )


def _unread_seconds(value) -> int:
    """A timestamp in seconds, whatever unit it arrived in.

    Deliberately the same rule as core/incremental_sync.py's _seconds(), and
    deliberately a second copy rather than an import: main.py is the module
    incremental_sync is imported INTO, and reaching back the other way for four
    lines would make the dependency circular. The threshold is the one that
    cannot be ambiguous — 1e12 seconds is the year 33658, so anything above it
    is milliseconds.
    """
    try:
        ts = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return ts // 1000 if ts > 1_000_000_000_000 else ts


def reconcile_snapshot_unread(
    server_unread: int,
    local_unread: int,
    incoming_timestamp: int,
    local_timestamp: int,
    unsynced: bool = False,
) -> int:
    """Merge an authoritative chat snapshot without losing newer live arrivals.

    The timestamp guard below is what let a conversation read on the phone sit
    at 8 unread forever, curable only with F5 (issue #173) — and the reason is
    not the rule, it is that the two sides were being compared in different
    units. `t` arrives from WhatsApp Web in seconds, while several local paths
    write a millisecond value into the very same field (on_historical_message()
    is the one CLAUDE.md names). A millisecond local timestamp is a thousand
    times any second-based snapshot, so `incoming < local` became permanently
    true and NO later snapshot could ever lower the count again. F5 "fixed" it
    only by wiping self.chats, leaving nothing to preserve.

    core/incremental_sync.py learned this on its own side and its _seconds()
    docstring says it outright: comparing the two raw "makes the local side
    look impossibly newer, which is the direction that silently skips a chat".
    The same normalisation belongs here, on the comparison that decides whether
    a read is allowed to reach the badge.

    The guard itself stays exactly as it was, because it protects a real case:
    a snapshot a second older than a live arrival must not clear the count that
    arrival just raised.
    """
    server_unread = max(0, int(server_unread or 0))
    local_unread = max(0, int(local_unread or 0))
    if server_unread >= local_unread:
        return server_unread
    if unsynced or _unread_seconds(incoming_timestamp) < _unread_seconds(local_timestamp):
        return local_unread
    return server_unread


def history_gap_detected(fetched: list, local_records: list, page_size: int) -> bool:
    """True when a freshly fetched page cannot be joined onto stored history.

    sync_chat_messages() asks get-messages for the newest `page_size`
    messages and nothing else. Whenever a chat received more than that while
    WinZapp was closed, the window lands entirely *after* the newest message
    on disk and the messages in between are never requested by anything —
    reported live on an active group: 1391 messages stored with a 14.5-hour
    hole (08/08 01:56 -> 16:28) and exactly 201 messages on the newer side,
    i.e. one saturated 200-page. WhatsApp's own unread count still counted
    the missing ones, so the chat announced 433 unread and produced ~200.

    Three conditions, because each alone gives a false positive:

    * The page must be **full**. A short page is everything the store had, so
      nothing can be hiding behind it and a wider request would not help.
    * There must be stored messages **older** than the page. With nothing
      older there is no second block to be disjoint from — that is a first
      sync, not a hole.
    * The page must be **mostly new to us**. This is what separates a hole
      from the ordinary case of re-syncing a chat we already hold: a routine
      re-sync returns the same newest messages we already stored, so the
      overlap is near total, while a page on the far side of a hole shares
      almost nothing with what is on disk. Comparing timestamps instead
      cannot tell the two apart — a single live message arriving mid-sync
      drags the newest stored timestamp past the fetched window and masks
      the hole permanently.
    """
    if not fetched or not local_records or page_size <= 0:
        return False
    if len(fetched) < page_size:
        return False

    oldest_fetched = min(_message_ts(m) for m in fetched)
    if not oldest_fetched:
        return False
    if not any(0 < _message_ts(r) < oldest_fetched for r in local_records):
        return False

    fetched_ids = {m.get("key", {}).get("id") for m in fetched if isinstance(m, dict)}
    fetched_ids.discard(None)
    fetched_ids.discard("")
    known = sum(
        1 for r in local_records
        if isinstance(r, dict) and r.get("key", {}).get("id") in fetched_ids
    )
    # Half is deliberately loose: the point is to separate "we already had
    # this window" (overlap near 100%) from "this window is new" (near 0),
    # not to pin down a precise ratio.
    return known * 2 < len(fetched)


def history_gap_closed(fetched: list, local_records: list, hole_top_ts: int) -> bool:
    """True when a widened fetch has reached back into the history we held.

    Deliberately not "history_gap_detected() is now False". That ratio asks
    whether *most* of a page is new, and it misreads the widened case: `known`
    is counted over local_records — the 200 records get_chats() keeps in
    memory — while the widened page holds 800 or 2000. Whenever the widened
    window lands *inside* that snapshot instead of reaching past it, some
    stored records are still older than the page, and `known * 2 <
    len(fetched)` is then true however complete the overlap is — so a hole
    that was in fact reached reads as still open and burns another
    escalation.

    The right question is narrower and has an exact answer: did the widened
    window come back down far enough to touch a message we already had from
    *below* the hole? `hole_top_ts` is the oldest timestamp of the original
    narrow page — the hole's ceiling — so anything stored below it is history
    on the far side. Restricting the comparison that way also makes it immune
    to a live message landing mid-sync: those sit above the ceiling and are
    excluded from the reference set entirely.
    """
    if not fetched or not local_records or not hole_top_ts:
        return False
    below = {
        r.get("key", {}).get("id") for r in local_records
        if isinstance(r, dict) and 0 < _message_ts(r) < hole_top_ts
    }
    below.discard(None)
    below.discard("")
    if not below:
        # Nothing stored below the hole, so there is no far side to reach.
        return False
    got = {m.get("key", {}).get("id") for m in fetched if isinstance(m, dict)}
    return bool(below & got)
