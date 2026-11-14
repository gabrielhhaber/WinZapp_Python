"""Build the transcript an unread-messages summary is made from.

Pure and wx-free: the conversation panel (ui/conversation_panel/ai_actions.py)
hands in the unread slice of its sorted messages and its own sender-label
helper, and gets back the plain-text transcript sent to the provider. The
wording of the placeholders is English on purpose, like the prompts: the
answer's language comes from the instructions, not from this input.
"""

#: Newest messages and total characters a summary is built from. The cap
#: keeps long backlogs inside every provider's context window; what does not
#: fit is dropped from the OLD end, so the newest unread always travel.
MAX_MESSAGES = 200
MAX_CHARS = 15_000

#: One placeholder per message type without wire text. Captions travel with
#: their media line.
_PLACEHOLDER = {
    "audioMessage": "[Voice message]",
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


def build_unread_transcript(messages, sender_label, max_messages=MAX_MESSAGES,
                            max_chars=MAX_CHARS) -> "tuple[str, int]":
    """``"Name: line"`` per usable message, oldest first, capped from the old
    end. *sender_label* is the panel's own name resolution (contact name,
    pushName, "you" for one's own messages). Returns (transcript, how many
    messages went in) — ("", 0) when nothing was usable."""
    if max_messages <= 0 or max_chars <= 0:
        return "", 0
    lines = []
    for msg in messages[-max_messages:]:
        text = message_text(msg)
        if not text:
            continue
        try:
            name = str(sender_label(msg) or "Someone").strip() or "Someone"
        except Exception:
            name = "Someone"
        line = f"{name}: {text}"
        if len(line) > max_chars:
            marker = " [truncated]"
            line = (line[:max_chars - len(marker)] + marker
                    if max_chars >= len(marker) else marker[:max_chars])
        lines.append(line)
    transcript = ""
    kept = 0
    for line in reversed(lines):
        candidate = (line + "\n" + transcript) if transcript else line
        if transcript and len(candidate) > max_chars:
            break
        transcript = candidate
        kept += 1
    return transcript, kept
