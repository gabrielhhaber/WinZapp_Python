"""Which messages a batch translation covers (issue #793).

Automatic translation only reaches messages that arrive after it is turned on.
A batch translates the messages already in the chat, one request each, through
the same provider, model and display mode as "Translate message"
(core/message_translation.py). Pure logic: no wx, no network.
"""
from core.message_translation import MAX_TEXT_CHARS, message_identity, message_text

#: Most messages one batch sends, so a long chat never becomes hundreds of
#: paid requests at once. It also stays below the panel's result cache
#: (translation_actions._RESULT_LIMIT), so every translation stays on screen.
BATCH_LIMIT = 50
#: Progress is said every this many messages while a batch runs.
PROGRESS_EVERY = 10


def batch_candidates(messages, limit=BATCH_LIMIT):
    """``(rows, left_out)``: the messages worth translating, in list order.

    Only received text messages count: the person's own messages are already in
    their language, and a text too long for one request is never sent. The
    first ``limit`` are kept (the person reads from where they are downwards);
    ``left_out`` says how many more there were.
    """
    rows = []
    seen = set()
    for message in messages:
        if not isinstance(message, dict) or (message.get("key") or {}).get("fromMe"):
            continue
        text = message_text(message)
        identity = message_identity(message)
        if not text or len(text) > MAX_TEXT_CHARS or not identity or identity in seen:
            continue
        seen.add(identity)
        rows.append(message)
    return rows[:limit], max(len(rows) - limit, 0)


def says_progress(done, total, every=PROGRESS_EVERY):
    """Whether "translated: done of total" is said now: every ``every``
    messages, never on the last one (the end has its own message)."""
    return 0 < done < total and done % every == 0
