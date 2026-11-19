"""Pure logic of an AI summary of chat messages (kind "text").

Wx-free: the conversation panel (ui/conversation_panel/summary_actions.py)
decides WHICH messages to summarize (unread, from one onwards, selected) and
hands them in with its own line formatting; this module turns them into the
plain-text transcript sent to the provider and says what did not fit. The
wording of the placeholders is English on purpose, like the prompts: the
answer's language comes from the instructions, not from this input.

The transcript is the content of a private conversation: nothing here logs.
"""
from typing import NamedTuple

#: Newest messages and total characters a summary is built from. The cap
#: keeps long backlogs inside every provider's context window; what does not
#: fit is dropped from the OLD end, so the newest messages always travel.
MAX_MESSAGES = 200
MAX_CHARS = 15_000

#: Voice messages transcribed on the spot per summary (the newest ones). Each
#: one is a local Whisper run, so the wait is bounded; older ones stay markers.
MAX_VOICE_TRANSCRIPTIONS = 20

#: Already-read messages fetched above the unread ones, so the divider is
#: found where WhatsApp puts it; a corrupt unread count never pulls more than the cap.
UNREAD_FETCH_BUFFER = 50
UNREAD_FETCH_CAP = 2000

#: The "Summarize unread" button is only worth a Tab stop once a backlog is
#: long; below this the shortcut and the menus still work.
BUTTON_MIN_UNREAD = 10

VOICE_MARKER = "[Voice message]"

#: One placeholder per message type without wire text. Captions travel with
#: their media line.
_PLACEHOLDER = {
    "audioMessage": VOICE_MARKER,
    "imageMessage": "[Image]",
    "videoMessage": "[Video]",
    "stickerMessage": "[Sticker]",
    "documentMessage": "[Document]",
    "locationMessage": "[Location]",
    "liveLocationMessage": "[Live location]",
    "contactMessage": "[Contact card]",
    "contactsArrayMessage": "[Contact card]",
    "pollCreationMessage": "[Poll]",
    "call_log": "[Call]",
    "protocolMessage": None,  # edits/deletes: no content to summarize
    "reactionMessage": None,
}


class SummaryTranscript(NamedTuple):
    #: What goes to the provider ("" when nothing was usable).
    text: str
    #: Messages in ``text``.
    included: int
    #: Usable older messages that did not fit the caps.
    left_out: int
    #: Voice messages in ``text`` that are only the marker, with no words.
    voice_markers: int


def message_text(msg) -> "str | None":
    """The wire text of one message, a placeholder for media, or None when the
    message carries nothing a summary can use (reactions, protocol events,
    unknown types)."""
    if not isinstance(msg, dict):
        return None
    body = msg.get("message")
    body = body if isinstance(body, dict) else {}
    text = body.get("conversation") or ""
    if not text:
        ext = body.get("extendedTextMessage")
        if isinstance(ext, dict):
            text = ext.get("text") or ""
    msg_type = str(msg.get("messageType") or "")
    if not text:
        # Media captions say what a bare "[Image]" cannot.
        inner = body.get(msg_type)
        if isinstance(inner, dict):
            text = inner.get("caption") or ""
    if not text:
        return _PLACEHOLDER.get(msg_type)
    return " ".join(str(text).split())


def voice_text(spoken) -> str:
    """A voice message's line text: its transcription when there is one,
    else the bare marker."""
    words = " ".join(str(spoken or "").split())
    return f"{VOICE_MARKER} {words}" if words else VOICE_MARKER


def is_voice_message(msg) -> bool:
    return isinstance(msg, dict) and msg.get("messageType") == "audioMessage"


def build_unread_transcript(messages, format_line, text_of=message_text,
                            max_messages=MAX_MESSAGES, max_chars=MAX_CHARS) -> SummaryTranscript:
    """One ``format_line(msg, text)`` per usable message, oldest first, capped
    from the old end.

    *format_line* is the panel's own line (date and time, sender name,
    text); *text_of* gives a message's text (the panel's resolves @mentions
    and swaps in a voice message's transcription). ``left_out`` counts the
    usable messages that did not fit, so the window can say so."""
    if max_messages <= 0 or max_chars <= 0:
        return SummaryTranscript("", 0, 0, 0)
    usable = []
    for msg in messages:
        text = text_of(msg)
        if text:
            usable.append((msg, text))
    lines = []
    for msg, text in usable[-max_messages:]:
        try:
            line = format_line(msg, text)
        except Exception:
            line = text
        if len(line) > max_chars:
            marker = " [truncated]"
            line = (line[:max_chars - len(marker)] + marker
                    if max_chars >= len(marker) else marker[:max_chars])
        lines.append((line, text == VOICE_MARKER and is_voice_message(msg)))
    transcript = ""
    kept = 0
    markers = 0
    for line, marker in reversed(lines):
        candidate = (line + "\n" + transcript) if transcript else line
        if transcript and len(candidate) > max_chars:
            break
        transcript = candidate
        kept += 1
        markers += marker
    return SummaryTranscript(transcript, kept, len(usable) - kept, markers)


def unread_fetch_limit(unread_count) -> int:
    """How many of a chat's newest messages to read to reach its unread ones."""
    return min(max(int(unread_count or 0), 0) + UNREAD_FETCH_BUFFER, UNREAD_FETCH_CAP)


def unread_messages(messages, unread_count) -> list:
    """The unread part of ``messages`` (oldest first): from WhatsApp's unread
    divider (core/unread_types.py) to the newest message. Empty when nothing is unread.

    When ``messages`` holds fewer countable messages than ``unread_count`` the
    divider lies above everything that was read: right after pairing WhatsApp
    Web keeps about 15 messages per chat while the badge shows the server's
    count, and the fetch stops at UNREAD_FETCH_CAP. Every message read is then
    unread, so all of them are summarized (the window says how many it covers).
    """
    from core.unread_types import first_unread_index
    if not messages or int(unread_count or 0) <= 0:
        return []
    start = first_unread_index(messages, int(unread_count))
    return list(messages[max(start, 0):])


def voice_to_transcribe(messages, has_text, limit=MAX_VOICE_TRANSCRIPTIONS) -> list:
    """The voice messages of ``messages`` (oldest first) to transcribe now: the
    ones with no stored transcription (``has_text(msg)``), only the newest
    ``limit`` of them. The older ones stay markers, and the transcript counts
    them (``SummaryTranscript.voice_markers``)."""
    pending = [msg for msg in messages if is_voice_message(msg) and not has_text(msg)]
    return pending[max(len(pending) - limit, 0):] if limit > 0 else []


def button_offered(unread_count) -> bool:
    """Whether the summary button is shown for this many unread messages."""
    return unread_count >= BUTTON_MIN_UNREAD
