"""What a reply sends about the message it quotes, for an engine that may not
have that message stored.

The Neonize bridge builds a quote from its own store, which only holds what it
received itself: a message that arrived while the account was on WPPConnect is
not there, and every reply to it was refused ("the quoted message was not
found", reported 2026-11-13 right after switching engines). WinZapp has the
message, so the reply carries a short description of it (the WhatsApp Web
fields the bridge already turns into a quote bubble); the bridge uses it only
when its store misses. WPPConnect ignores the extra field.
"""

MAX_TEXT = 1024


def _text(value) -> str:
    return value[:MAX_TEXT] if isinstance(value, str) else ""


def _content(message: dict):
    """(type, fields) of a Baileys-style `message`, or None for what is not
    worth quoting from a description (reactions, protocol messages...)."""
    if not isinstance(message, dict):
        return None
    for wrapper in ("ephemeralMessage", "viewOnceMessage", "viewOnceMessageV2",
                    "documentWithCaptionMessage", "editedMessage"):
        inner = message.get(wrapper)
        if isinstance(inner, dict) and isinstance(inner.get("message"), dict):
            return _content(inner["message"])
    if isinstance(message.get("conversation"), str):
        return "chat", {"body": _text(message["conversation"])}
    extended = message.get("extendedTextMessage")
    if isinstance(extended, dict):
        return "chat", {"body": _text(extended.get("text"))}
    kinds = (("imageMessage", "image"), ("videoMessage", "video"),
             ("documentMessage", "document"), ("stickerMessage", "sticker"),
             ("audioMessage", "audio"))
    for field, kind in kinds:
        media = message.get(field)
        if not isinstance(media, dict):
            continue
        fields = {"caption": _text(media.get("caption")),
                  "mimetype": _text(media.get("mimetype"))}
        if kind == "document":
            fields["filename"] = _text(media.get("fileName"))
        if kind == "audio":
            if media.get("ptt"):
                kind = "ptt"
            seconds = media.get("seconds")
            if isinstance(seconds, int) and seconds > 0:
                fields["duration"] = seconds
        return kind, fields
    return None


def quote_fallback(quoted) -> dict | None:
    """{"type", "body"/"caption"/..., "fromMe", "author"} of the quoted record,
    or None when it cannot be described (the bridge then refuses as before)."""
    if not isinstance(quoted, dict):
        return None
    content = _content(quoted.get("message"))
    if content is None:
        return None
    kind, fields = content
    key = quoted.get("key") if isinstance(quoted.get("key"), dict) else {}
    author = key.get("participant") or key.get("remoteJid") or ""
    described = {"type": kind, **fields, "fromMe": bool(key.get("fromMe"))}
    if isinstance(author, str) and author:
        described["author"] = author
    return described
