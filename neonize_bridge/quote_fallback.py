"""The quoted message as the client describes it, for a reply whose quote the
store does not hold.

The store only has what this device received and decrypted. Right after the
account switches to this engine the server delivers the queue it kept while
the device was away, which took about 4 minutes on 2026-11-13; a reply to a
message still in that queue was refused ("quoted message not found"). Group
messages this device cannot decrypt ("no sender key state") never reach the
store at all. The client sends `quotedMessage` (client/core/quote_fallback.py)
and the quote is built from it only when the store misses.
"""

TYPES = {"chat", "image", "video", "document", "sticker", "audio", "ptt"}
TEXT_FIELDS = ("body", "caption", "mimetype", "filename")
MAX_TEXT = 1024


def described(data) -> dict | None:
    """The client's description reduced to the WhatsApp Web fields
    msgmap.quoted / msgmap.quoted_participant read, or None if unusable."""
    if not isinstance(data, dict) or data.get("type") not in TYPES:
        return None
    out = {"type": data["type"], "fromMe": data.get("fromMe") is True}
    for field in TEXT_FIELDS:
        value = data.get(field)
        if isinstance(value, str) and value:
            out[field] = value[:MAX_TEXT]
    duration = data.get("duration")
    if isinstance(duration, int) and not isinstance(duration, bool) and 0 < duration < 86400:
        out["duration"] = duration
    author = data.get("author")
    if isinstance(author, str) and "@" in author and len(author) < 128:
        out["author"] = author
    if not out["fromMe"] and "author" not in out:
        return None             # a quote must name who wrote it
    return out
