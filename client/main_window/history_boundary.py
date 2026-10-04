"""Where a linked device's history ends while the phone's goes on.

WhatsApp gives a linked device a bounded window of each chat. Past it there
are two different situations, and `primaryHasMoreMessagesReadyToLoad` — the
check the on-demand request was gated on — is true for both:

* the phone has older messages and this device may ask for a batch of them
  (`endOfHistoryTransferType` 0): WhatsApp Web shows "Click here to get older
  messages from your phone";
* the phone has older messages and this device may not ask
  (`COMPLETE_ON_DEMAND_SYNC_BUT_MORE_MSG_REMAIN_ON_PRIMARY`, 4): what a chat
  becomes once an on-demand sync has delivered all it will. WhatsApp Web shows
  only "Use WhatsApp on your phone to see older messages."

Asking in the second one is answered with silence, and the phone tells its
owner "Sync paused. Open WhatsApp to resume." (issue #220). The API now
refuses it before sending and reports `phoneOnly`; these functions carry that
verdict to the two callers that ask the phone (the backfill and the user
scrolling up) and to the conversation panel, which says so out loud.

Measured over CDP on a live session, 2026-10-04 (WhatsApp Web
2.3000.1049220475): 5 of 569 chats were in state 4, every one of them passing
the old gate.

Plain functions over the window rather than mixin methods: both mixins that
need them are exercised in tests through stubs carrying only what a method
touches.
"""

import logging

from core.api_client import api_get


def older_history_url(window, jid: str, route: str) -> str:
    """An older-history API *route* for an already normalized *jid*."""
    # Same @lid-preferred addressing sync_chat_messages() uses, so a chat the
    # store only knows under its @lid still resolves.
    lid = getattr(window, "_phone_to_lid", {}).get(jid, "")
    if lid:
        phone = lid
    elif jid.endswith("@s.whatsapp.net"):
        phone = jid.split("@")[0] + "@c.us"
    else:
        phone = jid
    return f"{window.wpp_server}:{window.wpp_port}/api/{window.token}/{route}/{phone}"


def note_verdict(window, jid: str, payload) -> bool:
    """Record whether the API just called *jid*'s older history phone-only.

    Kept per session and always the API's latest answer: the state moves, and
    an answer that does not carry the verdict (an error before it was reached,
    an API built before it existed) is "not known to be".
    """
    chats = getattr(window, "_phone_only_history_chats", None)
    if chats is None:
        chats = window._phone_only_history_chats = set()
    if isinstance(payload, dict) and payload.get("phoneOnly") is True:
        chats.add(jid)
        return True
    chats.discard(jid)
    return False


def is_only_on_phone(window, remote_jid: str) -> bool:
    """Whether the API last reported *remote_jid*'s older history as reachable
    only on the phone."""
    return window._normalize_jid(remote_jid) in getattr(
        window, "_phone_only_history_chats", ())


def probe(window, remote_jid: str, timeout: int = 15) -> bool:
    """Ask the API for the verdict without sending anything to the phone.

    For a chat that was already asked once: asking again would be a second
    notification on the phone, but the chat may since have become phone-only —
    which is exactly what a successful on-demand sync leaves behind.

    A route of its own, never a flag on the request: an API built before it
    existed answers 404, where a flag it does not know would send for real.
    Any failure is "not known to be", never True.
    """
    if not getattr(window, "_wa_connected", False):
        return False
    jid = window._normalize_jid(remote_jid)
    try:
        response = api_get(
            older_history_url(window, jid, "older-history-state"),
            headers={"Authorization": f"Bearer {window.token}",
                     "Content-Type": "application/json"},
            timeout=timeout,
        )
        body = response.json()
    except Exception as exc:
        logging.info("[history-sync] History-boundary probe failed for %s: %s", jid, exc)
        return False
    return note_verdict(window, jid, body.get("response") if isinstance(body, dict) else None)
