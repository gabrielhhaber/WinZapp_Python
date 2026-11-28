"""Asking the phone for the chats past the first batch (WPPConnect).

An iPhone caps the first history batch of a new link at 399 conversations.
WhatsApp Web, which WPPConnect drives, never learns of the rest, so a user
with 467 chats sees ~413 and no message older than the last weeks for most of
them (reported 2026-11-26, the same account on Neonize: neonize_bridge/
recent_conversations.py). WhatsApp Web has a request for exactly this, sent
to the phone as a peer data operation; the patched server exposes it as
POST /request-recent-conversations (deviceController.ts). The answer is a
history chunk WhatsApp Web stores on its own; the chat re-listing that already
follows a sync picks the new chats up.

Asked once per launch, only when the chat count shows the cap, so the phone
is not notified for nothing.
"""

import logging
import threading
import time

from core import connection_engine as engines
from core.api_client import api_post

#: the cap is 399 conversations (status and community containers included);
#: a list that short of it may already be everything the account has.
CAP_THRESHOLD = 390

#: (conversations, messages per conversation) of each attempt, in order. The
#: Neonize bridge found on 2026-11-27 that an iPhone never answered 2000 x 50;
#: WhatsApp Web itself sends 10 x 10, so that goes first. The next attempt is
#: sent only while WhatsApp Web's chat store did not grow after the last one.
VARIANTS = ((10, 10), (2000, 10), (2000, 50))
#: seconds to wait for the answer to an attempt before the next one
WAIT_EACH = 75
#: chats the store must gain after an attempt to count it as answered
GAIN_TO_STOP = 3


def should_ask(engine, chat_count, already_asked):
    """Pure: whether this sync round sends the request."""
    if engine != engines.WPPCONNECT or already_asked:
        return False
    return chat_count >= CAP_THRESHOLD


def store_chats(main_window):
    """Chats WhatsApp Web's store holds (history-sync-status), or None."""
    status = main_window.fetch_history_sync_status(timeout=10)
    counts = status.get("storeCounts") if isinstance(status, dict) else None
    value = counts.get("chat") if isinstance(counts, dict) else None
    return value if isinstance(value, int) else None


def gained(before, after):
    """Whether the store grew enough between two reads to count as an answer."""
    return before is not None and after is not None and after - before >= GAIN_TO_STOP


def ask_once(main_window, post=api_post, sleep=time.sleep, chats_in_store=store_chats):
    """Sends the requests when `should_ask`, one variant at a time while the
    store does not grow; returns how many went out."""
    asked = getattr(main_window, "_wpp_recent_conversations_asked", False)
    chats = len(getattr(main_window, "chats", None) or ())
    if not should_ask(engines.loaded_engine(main_window), chats, asked):
        return 0
    if not getattr(main_window, "_wa_connected", False):
        return 0
    main_window._wpp_recent_conversations_asked = True
    url = (f"{main_window.wpp_server}:{main_window.wpp_port}"
           f"/api/{main_window.token}/request-recent-conversations")
    sent = 0
    for number, (count, per_chat) in enumerate(VARIANTS, 1):
        before = chats_in_store(main_window)
        try:
            response = post(
                url,
                headers={"Authorization": f"Bearer {main_window.token}",
                         "Content-Type": "application/json"},
                json={"conversationCount": count, "messagesPerConversation": per_chat},
                timeout=60,
            )
            try:
                body = response.json()
            except Exception:
                body = {}
            payload = body.get("response") if isinstance(body, dict) else None
            went = bool(response.status_code in (200, 201) and isinstance(payload, dict)
                        and payload.get("requested"))
            logging.info("[recent-conversations] attempt %d of %d (%d chats x %d messages), "
                         "%d chats listed (cap %d): status=%s sent=%s phone_supports=%s answer=%s",
                         number, len(VARIANTS), count, per_chat, chats, CAP_THRESHOLD,
                         response.status_code, went,
                         payload.get("primarySupports") if isinstance(payload, dict) else None,
                         str(payload)[:200])
        except Exception as exc:
            logging.info("[recent-conversations] attempt %d failed: %s", number, exc)
            went = False
        if not went:
            break               # refused (the phone does not answer it) or unreachable
        sent += 1
        sleep(WAIT_EACH)
        after = chats_in_store(main_window)
        logging.info("[recent-conversations] store chats %s -> %s after attempt %d",
                     before, after, number)
        if gained(before, after):
            break
    return sent


def start(main_window):
    """`ask_once` off the sync thread (the route can take a few seconds)."""
    # No `args=`: sync tests swap threading.Thread for a fake that calls
    # target() bare, so the target is a closure.
    threading.Thread(target=lambda: ask_once(main_window), daemon=True,
                     name="wpp-recent-conversations").start()
