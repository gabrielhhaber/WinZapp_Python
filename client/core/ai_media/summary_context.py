"""What a chat summary knows about a message beyond its bare text (pure, no wx).

A summary used to send "[Image]", "[Poll]" and no trace of what a reply was
answering. This module adds, from what is already on the machine: the AI
description saved with a picture, sticker or video, a poll's question, options
and (when known) votes, and a short excerpt of the message a reply quotes.
Nothing here reads a file or the network, and nothing logs: the result is the
content of a private conversation.
"""
from core.ai_media.saved import saved_description
from core.polls import poll_content, poll_options
from core.transcription import stored

#: Longest excerpt of a quoted message, and longest saved description, sent.
QUOTE_CHARS = 160
DESCRIPTION_CHARS = 600

_VISUAL = {"imageMessage": ("image", "Image"), "videoMessage": ("video", "Video"),
           "stickerMessage": ("sticker", "Sticker")}

_QUOTE_SUBS = ("extendedTextMessage", "audioMessage", "imageMessage", "videoMessage",
               "documentMessage", "stickerMessage", "locationMessage", "contactMessage")


def _clip(text, limit) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _context_info(msg):
    top = msg.get("contextInfo")
    if isinstance(top, dict) and isinstance(top.get("quotedMessage"), dict):
        return top
    body = msg.get("message")
    if not isinstance(body, dict):
        return None
    for key in _QUOTE_SUBS:
        sub = body.get(key)
        ctx = sub.get("contextInfo") if isinstance(sub, dict) else None
        if isinstance(ctx, dict) and isinstance(ctx.get("quotedMessage"), dict):
            return ctx
    return None


def _quoted_words(quoted) -> str:
    """The readable words of a quoted message body, or a bare type word."""
    text = quoted.get("conversation")
    if isinstance(text, str) and text.strip():
        return text
    for key in ("extendedTextMessage", "imageMessage", "videoMessage", "documentMessage"):
        sub = quoted.get(key)
        if isinstance(sub, dict):
            words = sub.get("text") or sub.get("caption")
            if isinstance(words, str) and words.strip():
                return words
    names = {"audioMessage": "voice message", "imageMessage": "image", "videoMessage": "video",
             "stickerMessage": "sticker", "documentMessage": "document",
             "locationMessage": "location", "contactMessage": "contact card",
             "pollCreationMessage": "poll"}
    for key, word in names.items():
        if key in quoted:
            return word
    return ""


def quote_prefix(msg) -> str:
    """"[in reply to: ...] " for a reply that quotes something readable, else ""."""
    if not isinstance(msg, dict):
        return ""
    ctx = _context_info(msg)
    if ctx is None:
        return ""
    words = _clip(_quoted_words(ctx["quotedMessage"]), QUOTE_CHARS)
    return f"[in reply to: {words}] " if words else ""


def quoted_excerpt(msg, limit=QUOTE_CHARS) -> str:
    """The readable words of the message ``msg`` replies to, clipped, or "";
    unlike ``quote_prefix`` a bare type word ("image") is not worth sending."""
    ctx = _context_info(msg) if isinstance(msg, dict) else None
    if ctx is None:
        return ""
    quoted = ctx["quotedMessage"]
    for key in ("conversation", "extendedTextMessage", "imageMessage", "videoMessage",
                "documentMessage"):
        sub = quoted.get(key)
        words = sub if isinstance(sub, str) else (
            (sub.get("text") or sub.get("caption")) if isinstance(sub, dict) else "")
        if isinstance(words, str) and words.strip():
            return _clip(words, limit)
    return ""


def poll_text(msg) -> str:
    """"[Poll] Question - options: A (3 votes), B (1 vote)" or "" when it is no poll.
    A vote count is only written when it is known (never an invented 0)."""
    poll = poll_content((msg or {}).get("message"))
    if not poll:
        return ""
    options = []
    for opt in poll_options(poll):
        votes = opt["votes"]
        if votes is None:
            options.append(opt["name"])
        else:
            options.append(f"{opt['name']} ({votes} vote{'' if votes == 1 else 's'})")
    question = _clip(poll.get("name"), QUOTE_CHARS * 2)
    text = f"[Poll] {question}".rstrip()
    return f"{text} - options: {'; '.join(options)}" if options else text


def visual_text(msg, base) -> str:
    """A picture, sticker or video line with its saved AI description, keeping
    the caption when there is one. ``base`` is the line without a description
    (the caption, or the bare "[Image]" marker); returned unchanged when the
    message has no saved description."""
    kind = _VISUAL.get(str((msg or {}).get("messageType") or ""))
    if kind is None:
        return base
    description = _clip(saved_description(msg, kind[0]), DESCRIPTION_CHARS)
    # What a video SAYS (a local transcription) is deliberately not sent: the
    # person never agreed to that text leaving the computer, unlike a voice
    # message, whose handling the summary asks about first.
    if not description:
        return base
    marker = f"[{kind[1]}: {description}]"
    return marker if base == f"[{kind[1]}]" else f"{marker} {base}"


def enrich_text(msg, base) -> str:
    """``base`` (the message's text or placeholder) with what a summary can add:
    poll content, the saved description of media and the quoted excerpt."""
    if not base or not isinstance(msg, dict):
        return base
    if base == "[Poll]":
        base = poll_text(msg) or base
    else:
        base = visual_text(msg, base)
    return quote_prefix(msg) + base
