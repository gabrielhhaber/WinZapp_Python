"""Recover an undecrypted message's text from a reply that quotes it.

WhatsApp Web keeps a message it could not decrypt as a `ciphertext`
placeholder ("Aguardando mensagem"), sometimes for good -- measured 2026-09-23:
every message from one group member who had changed phone, for hours. Yet the
people who answer it read it fine on their phones, and a reply carries what it
quotes: ``contextInfo`` holds the original's id (``stanzaId``), its author
(``participant``) and its content (``quotedMessage``). So when a reply to a
stored placeholder arrives, the placeholder can show the real text.

Limits, each deliberate:
- Text only. A quoted photo or audio carries no usable media: media is fetched
  by message id, and WhatsApp Web still holds that id as a ciphertext.
- A stored reply keeps at most ``QUOTED_TEXT_CAP`` characters of its quote. A
  quote exactly that long may have been cut, and showing a cut text as the
  whole message would be worse than "Aguardando mensagem".
- The quote is the replier's claim, not the author's, so it must name the
  placeholder's own author, and the record is marked ``_recovered_from_quote``:
  the real decrypted copy replaces it whenever it arrives (never read as an
  edit), and a sync copy that is still a ciphertext does not wipe it.
"""

from core.utils import QUOTED_TEXT_CAP, _slim_quoted_message, quoted_message_text

#: WhatsApp Web's "arrived, not decrypted yet" placeholder types.
UNDECRYPTED_PLACEHOLDER_TYPES = frozenset({"ciphertext"})

#: Marks a placeholder whose content came from a reply's quote.
RECOVERED_FROM_QUOTE = "_recovered_from_quote"


def is_undecrypted_placeholder(msg) -> bool:
    """Whether this record is a placeholder rather than a message."""
    return (((msg or {}).get("messageType") or "")
            in UNDECRYPTED_PLACEHOLDER_TYPES)


def awaits_real_copy(msg) -> bool:
    """A placeholder, or one filled from a quote: the real copy replaces it."""
    return is_undecrypted_placeholder(msg) or bool((msg or {}).get(RECOVERED_FROM_QUOTE))


def reply_context(msg):
    """The ``contextInfo`` of a reply (one naming a ``stanzaId``), or None."""
    if not isinstance(msg, dict):
        return None
    content = msg.get("message")
    if isinstance(content, dict):
        for sub in content.values():
            if isinstance(sub, dict):
                ctx = sub.get("contextInfo")
                if isinstance(ctx, dict) and ctx.get("stanzaId"):
                    return ctx
    ctx = msg.get("contextInfo")
    if isinstance(ctx, dict) and ctx.get("stanzaId"):
        return ctx
    return None


def recovered_message(quoted):
    """``(messageType, message)`` rebuilt from a quote, or None."""
    slim = _slim_quoted_message(quoted)
    if not isinstance(slim, dict) or "conversation" not in slim:
        return None  # media, or no usable text
    text = quoted_message_text(quoted)
    if not text or len(text) == QUOTED_TEXT_CAP:
        return None
    mentioned = slim.get("mentionedJid")
    if mentioned:
        return "extendedTextMessage", {
            "extendedTextMessage": {"text": text,
                                    "contextInfo": {"mentionedJid": list(mentioned)}},
        }
    return "conversation", {"conversation": text}


def _author(record):
    key = record.get("key") or {}
    if key.get("participant"):
        return key["participant"]
    # A 1:1 chat's incoming message is written by the chat itself. An own
    # message would need the account's JID; none has been seen as a
    # placeholder, so it is left alone.
    if key.get("fromMe"):
        return ""
    return key.get("remoteJid") or ""


def fill_placeholders_from_replies(records, same_person, replies=None) -> list:
    """Fill each placeholder in *records* that a reply quotes. Returns them.

    *replies* are the messages whose quotes are read (default: *records*
    itself). *same_person(a, b)* compares two JIDs across their address
    forms -- the quote may name the author as ``@lid`` while the placeholder
    holds a phone JID, or the reverse.
    """
    targets = {}
    for record in records or ():
        if isinstance(record, dict) and is_undecrypted_placeholder(record):
            record_id = (record.get("key") or {}).get("id")
            if record_id:
                targets[record_id] = record
    if not targets:
        return []
    filled = []
    for reply in (records if replies is None else replies) or ():
        ctx = reply_context(reply)
        if ctx is None:
            continue
        target = targets.get(ctx.get("stanzaId"))
        if target is None:
            continue
        author, quoted_author = _author(target), ctx.get("participant") or ""
        if not author or not quoted_author or not same_person(author, quoted_author):
            continue
        rebuilt = recovered_message(ctx.get("quotedMessage"))
        if rebuilt is None:
            continue
        target["messageType"], target["message"] = rebuilt
        target[RECOVERED_FROM_QUOTE] = True
        del targets[ctx["stanzaId"]]
        filled.append(target)
    return filled


def carry_over_recovered_quotes(new_msgs, old_msgs) -> int:
    """Keep recovered text when a sync copy of the same id is still a ciphertext.

    Same shape as carry_over_edited_marker(): the sync replaces a chat's
    records with the server's copies and writes them to the database, so
    without this the next get-messages turned the row back into "Aguardando
    mensagem". Returns how many were carried.
    """
    recovered = {
        (m.get("key") or {}).get("id"): m
        for m in (old_msgs or ())
        if isinstance(m, dict) and m.get(RECOVERED_FROM_QUOTE)
        and not is_undecrypted_placeholder(m)
    }
    recovered.pop(None, None)
    recovered.pop("", None)
    if not recovered:
        return 0
    carried = 0
    for m in new_msgs or ():
        if not isinstance(m, dict) or not is_undecrypted_placeholder(m):
            continue
        source = recovered.get((m.get("key") or {}).get("id"))
        if source is None:
            continue
        m["messageType"] = source["messageType"]
        m["message"] = source["message"]
        m[RECOVERED_FROM_QUOTE] = True
        carried += 1
    return carried
