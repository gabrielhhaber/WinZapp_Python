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

from core import connection_engine as engines
from core.api_client import api_post

#: the cap is 399 conversations (status and community containers included);
#: a list that short of it may already be everything the account has.
CAP_THRESHOLD = 390

#: conversations asked for in the one request
CONVERSATIONS = 2000
MESSAGES_PER_CONVERSATION = 10


def should_ask(engine, chat_count, already_asked):
    """Pure: whether this sync round sends the request."""
    if engine != engines.WPPCONNECT or already_asked:
        return False
    return chat_count >= CAP_THRESHOLD


def ask_once(main_window, post=api_post):
    """Sends the request when `should_ask`; returns True when it went out."""
    asked = getattr(main_window, "_wpp_recent_conversations_asked", False)
    chats = len(getattr(main_window, "chats", None) or ())
    if not should_ask(engines.loaded_engine(main_window), chats, asked):
        return False
    if not getattr(main_window, "_wa_connected", False):
        return False
    main_window._wpp_recent_conversations_asked = True
    url = (f"{main_window.wpp_server}:{main_window.wpp_port}"
           f"/api/{main_window.token}/request-recent-conversations")
    try:
        response = post(
            url,
            headers={"Authorization": f"Bearer {main_window.token}",
                     "Content-Type": "application/json"},
            json={"conversationCount": CONVERSATIONS,
                  "messagesPerConversation": MESSAGES_PER_CONVERSATION},
            timeout=60,
        )
        body = response.json() if response.status_code in (200, 201) else {}
        payload = body.get("response") if isinstance(body, dict) else None
        sent = bool(isinstance(payload, dict) and payload.get("requested"))
        logging.info("[recent-conversations] %d chats (cap %d): asked the phone "
                     "for the rest: status=%s sent=%s", chats, CAP_THRESHOLD,
                     response.status_code, sent)
        return sent
    except Exception as exc:
        logging.info("[recent-conversations] request failed: %s", exc)
        return False


def start(main_window):
    """`ask_once` off the sync thread (the route can take a few seconds)."""
    # No `args=`: sync tests swap threading.Thread for a fake that calls
    # target() bare, so the target is a closure.
    threading.Thread(target=lambda: ask_once(main_window), daemon=True,
                     name="wpp-recent-conversations").start()
