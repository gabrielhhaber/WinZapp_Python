"""A missing quote is definite only with an explicit pre-send API verdict."""


def is_missing_quote_failure(message) -> bool:
    return isinstance(message, dict) and bool(
        message.get("_reply_quote_missing") and message.get("_send_failed")
    )


def quote_missing_before_send(response) -> bool:
    if response.status_code != 500:
        return False
    try:
        body = response.json()
    except (ValueError, TypeError):
        return False
    return (
        isinstance(body, dict)
        and body.get("status") == "error"
        and body.get("code") == "quoted_message_not_found"
        and body.get("stage") == "quote_resolution"
        and body.get("sendAttempted") is False
    )


def mark_missing_reply_quote(chats, local_id):
    """Keep the failed text in its own chat even after the panel moved away."""
    if not local_id:
        return None
    for jid, chat in chats.items():
        records = chat.get("messages", {}).get("messages", {}).get("records", [])
        for record in records:
            if record.get("_local_id") == local_id:
                record["_local_pending"] = False
                record["_send_failed"] = True
                record["_reply_quote_missing"] = True
                return jid, record
    return None


#: What the composer held when a quoted reply was queued, kept on the row only
#: so a pre-send "quote missing" refusal can give the text back.
REPLY_DRAFT_KEYS = ("_reply_draft_text", "_reply_draft_mentions", "_reply_draft_mention_names")


def clear_reply_draft(record) -> None:
    """Drop the recovery copy once the reply was sent.

    Nothing reads it after that (the restore needs a missing-quote failure),
    and left on the row it is saved to messages.db with every sent reply: a
    second copy of the text and a JID-to-name map, forever.
    """
    if isinstance(record, dict):
        for key in REPLY_DRAFT_KEYS:
            record.pop(key, None)
