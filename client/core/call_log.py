"""WhatsApp call-log records: how a call appears as a message.

WhatsApp Web keeps every call as a message of type ``call_log`` in the chat it
belongs to (measured 2026-09-24 over CDP: 58 of 592 models in MsgStore). The
fields that matter are ``callOutcome``, ``finalCallOutcome``, ``isVideoCall``,
``callDuration`` (seconds) and ``callParticipants``; the message id is the
call id. The outcome is WhatsApp's own enum ``WAWebCallLogMsgData.flow``
(``CallOutcome``), copied below verbatim.

``fromMe`` is the direction: true for a call this account placed.

The same record is **rewritten in place** when the call ends: a call placed or
answered here is first written as ``Ongoing`` and later updated to its final
outcome and duration (``updateVoipCallLogOutcomeImpl``), and that update never
reaches WPPConnect's onAnyMessage. `is_call_log_pending()` is how the app knows
it has to fetch the record again.

Everything here is pure so the conversation list and the planned Calls tab
build the same label from the same record.
"""

from __future__ import annotations

CALL_LOG_MESSAGE_TYPE = "callLogMessage"
# What builds before this module stored: the raw WhatsApp type with no call
# data at all (the normalizer had no branch for it). Rendered generically until
# the next sync of that chat replaces it.
LEGACY_CALL_LOG_TYPE = "call_log"

COMPLETED = "Completed"
MISSED = "Missed"
REJECTED = "Rejected"
CANCELED = "Canceled"
ACCEPTED_ELSEWHERE = "AcceptedElsewhere"
ONGOING = "Ongoing"
FAILED = "Failed"
UNKNOWN = "Unknown"

# Outcomes WhatsApp later overwrites with the real one.
PENDING_OUTCOMES = frozenset({ONGOING, UNKNOWN, ""})

# Statuses, from this account's point of view. Direction matters: a "Missed"
# incoming call is one the user missed, an outgoing one is one the other side
# did not answer.
STATUS_MISSED = "missed"                    # incoming, not answered
STATUS_DECLINED = "declined"                # incoming, rejected by the user
STATUS_ANSWERED = "answered"                # incoming, answered
STATUS_ANSWERED_ELSEWHERE = "answered_elsewhere"
STATUS_MADE = "made"                        # outgoing, answered
STATUS_UNANSWERED = "unanswered"            # outgoing, not answered
STATUS_DECLINED_BY_PEER = "declined_by_peer"  # outgoing, rejected by the peer
STATUS_FAILED = "failed"
STATUS_ONGOING = "ongoing"
STATUS_UNKNOWN = "unknown"

_DURATION_STATUSES = frozenset({STATUS_ANSWERED, STATUS_MADE, STATUS_ANSWERED_ELSEWHERE})

# (status, is_video) -> i18n key. Written out rather than assembled so every
# key the code can ask for is greppable.
_LABEL_KEYS = {
    (STATUS_MISSED, False): "call_log_missed_voice",
    (STATUS_MISSED, True): "call_log_missed_video",
    (STATUS_DECLINED, False): "call_log_declined_voice",
    (STATUS_DECLINED, True): "call_log_declined_video",
    (STATUS_ANSWERED, False): "call_log_answered_voice",
    (STATUS_ANSWERED, True): "call_log_answered_video",
    (STATUS_ANSWERED_ELSEWHERE, False): "call_log_answered_elsewhere_voice",
    (STATUS_ANSWERED_ELSEWHERE, True): "call_log_answered_elsewhere_video",
    (STATUS_MADE, False): "call_log_made_voice",
    (STATUS_MADE, True): "call_log_made_video",
    (STATUS_UNANSWERED, False): "call_log_unanswered_voice",
    (STATUS_UNANSWERED, True): "call_log_unanswered_video",
    (STATUS_DECLINED_BY_PEER, False): "call_log_declined_by_peer_voice",
    (STATUS_DECLINED_BY_PEER, True): "call_log_declined_by_peer_video",
    (STATUS_FAILED, False): "call_log_failed_voice",
    (STATUS_FAILED, True): "call_log_failed_video",
    (STATUS_ONGOING, False): "call_log_ongoing_voice",
    (STATUS_ONGOING, True): "call_log_ongoing_video",
    (STATUS_UNKNOWN, False): "call_log_unknown_voice",
    (STATUS_UNKNOWN, True): "call_log_unknown_video",
}


def _as_int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def call_log_payload(wpp_msg: dict) -> dict:
    """The ``callLogMessage`` content for a raw WPPConnect ``call_log`` message."""
    wpp_msg = wpp_msg if isinstance(wpp_msg, dict) else {}
    outcome = str(wpp_msg.get("callOutcome") or "")
    final = str(wpp_msg.get("finalCallOutcome") or "")
    # WhatsApp records the settled outcome here while callOutcome can still
    # read Ongoing (its own label code checks both the same way).
    if outcome in PENDING_OUTCOMES and final and final not in PENDING_OUTCOMES:
        outcome = final
    participants = wpp_msg.get("callParticipants")
    participant_count = len(participants) if isinstance(participants, list) else 0
    return {
        "outcome": outcome,
        "isVideo": bool(wpp_msg.get("isVideoCall")),
        "durationSeconds": _as_int(wpp_msg.get("callDuration")),
        # More than the two ends of a one-to-one call, or a call link.
        "isGroupCall": participant_count > 2 or bool(wpp_msg.get("isCallLink")),
    }


def is_call_log(msg) -> bool:
    return isinstance(msg, dict) and msg.get("messageType") in (
        CALL_LOG_MESSAGE_TYPE, LEGACY_CALL_LOG_TYPE)


def call_log_data(msg) -> dict:
    """The stored call data, or {} for a legacy record (or anything else)."""
    if not is_call_log(msg):
        return {}
    content = msg.get("message")
    data = content.get(CALL_LOG_MESSAGE_TYPE) if isinstance(content, dict) else None
    return data if isinstance(data, dict) else {}


def call_log_status(msg) -> str:
    """One of the STATUS_* values for a call-log record."""
    data = call_log_data(msg)
    outcome = str(data.get("outcome") or "")
    outgoing = bool((msg.get("key") or {}).get("fromMe")) if isinstance(msg, dict) else False
    if outcome == ONGOING:
        return STATUS_ONGOING
    if outcome == COMPLETED:
        return STATUS_MADE if outgoing else STATUS_ANSWERED
    if outcome == ACCEPTED_ELSEWHERE:
        return STATUS_ANSWERED_ELSEWHERE
    if outcome == FAILED:
        return STATUS_FAILED
    if outcome == REJECTED:
        return STATUS_DECLINED_BY_PEER if outgoing else STATUS_DECLINED
    # A caller who hangs up before the user answers is "Canceled"; WhatsApp
    # counts it as a missed call too (getIsMissedCallOrNotConnected).
    if outcome in (MISSED, CANCELED):
        return STATUS_UNANSWERED if outgoing else STATUS_MISSED
    return STATUS_UNKNOWN


def call_log_is_video(msg) -> bool:
    return bool(call_log_data(msg).get("isVideo"))


def call_log_label(msg, i18n, format_duration) -> str:
    """The sentence a call-log row reads, e.g. "Ligação de voz atendida,
    duração: 30 minutos e 59 segundos".

    *format_duration* turns seconds into words ("" when unknown) — the
    conversation panel passes the one voice messages already use.
    """
    if not is_call_log(msg):
        return ""
    data = call_log_data(msg)
    if not data:
        return i18n.t("call_log_generic")
    status = call_log_status(msg)
    label = i18n.t(_LABEL_KEYS[(status, bool(data.get("isVideo")))])
    seconds = _as_int(data.get("durationSeconds"))
    if status in _DURATION_STATUSES and seconds > 0:
        duration = format_duration(seconds)
        if duration:
            label = f"{label}, {i18n.t('duration')}: {duration}"
    return label


def is_returnable_missed_call(msg, chat_jid: str = "") -> bool:
    """True for an incoming one-to-one call the user missed.

    Only those get "Retornar ligação": answered and declined calls do not, and
    WinZapp cannot place a group call.
    """
    if call_log_status(msg) != STATUS_MISSED:
        return False
    if call_log_data(msg).get("isGroupCall"):
        return False
    jid = str(chat_jid or (msg.get("key") or {}).get("remoteJid") or "")
    return bool(jid) and not jid.endswith(("@g.us", "@broadcast", "@newsletter"))


def is_call_log_pending(msg) -> bool:
    """A call-log record whose outcome WhatsApp has not settled yet."""
    data = call_log_data(msg)
    return bool(data) and str(data.get("outcome") or "") in PENDING_OUTCOMES


def call_log_candidate_ids(call_id: str, outgoing: bool, peer_jids) -> list:
    """Serialized message ids the record of call *call_id* may be stored under.

    The record's id is the call id and its chat is the peer, but whether
    WhatsApp filed it under the peer's @lid or phone JID is its own choice, so
    every known form is tried (phone JIDs in WhatsApp Web's @c.us spelling).
    """
    call_id = str(call_id or "")
    if not call_id:
        return []
    prefix = "true" if outgoing else "false"
    out = []
    for jid in peer_jids or ():
        jid = str(jid or "")
        if not jid:
            continue
        if jid.endswith("@s.whatsapp.net"):
            jid = jid[: -len("@s.whatsapp.net")] + "@c.us"
        candidate = f"{prefix}_{jid}_{call_id}"
        if candidate not in out:
            out.append(candidate)
    return out


# Seconds between re-reads of a call record: soon after the call ends, when
# WhatsApp writes the outcome, then settling into one read every five minutes.
_REFRESH_STEPS = (3, 10, 30, 90)
_REFRESH_PERIOD = 300


def call_log_refresh_delays(window_seconds: int):
    """The waits of one watch, stopping before their sum exceeds the window."""
    elapsed = 0
    steps = iter(_REFRESH_STEPS)
    while True:
        delay = next(steps, _REFRESH_PERIOD)
        if elapsed + delay > window_seconds:
            return
        elapsed += delay
        yield delay


def call_log_supersedes(existing, incoming) -> bool:
    """Whether *incoming* is a newer state of the call record *existing*.

    Duplicates are normally discarded by id, but a call record changes after
    it is first stored (see the module docstring), and a legacy record has no
    call data at all.
    """
    if not (is_call_log(existing) and is_call_log(incoming)):
        return False
    new = call_log_data(incoming)
    if not new:
        return False
    old = call_log_data(existing)
    if not old:
        return True
    # Never step a settled outcome back to a pending one.
    if (str(new.get("outcome") or "") in PENDING_OUTCOMES
            and str(old.get("outcome") or "") not in PENDING_OUTCOMES):
        return False
    return new != old
