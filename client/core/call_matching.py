"""Deciding whether a WhatsApp call event belongs to the call WinZapp is on.

Kept out of ``main.py`` because it *is* the bug it was written for, and that
bug is unreachable from a test while it lives on a ``wx.Frame``.

Two events describe one call and they do not agree on how to name the peer:
the offer arrives through ``call.incoming_call`` carrying whatever address
WhatsApp signalled with, while the page's own ``CallStore.activeCall`` poll
reports the form the store happens to hold. On a LID-addressed account those
are ``<digits>@lid`` and ``<phone>@s.whatsapp.net`` — the same person, two
strings. Compared raw, a call answered as ``@lid`` and reported ``ACTIVE`` as a
phone JID looked like a *different* call, so both the ACTIVE and the terminal
ENDED were discarded: nothing was announced, the call window stayed up, and
``CallAudioSession`` went on streaming the microphone for the rest of the
session (with every background sync pass standing down behind it).

So the peer comparison is injected rather than done here — ``main.py`` owns the
``@lid`` ↔ phone caches — and this module owns only the decision.
"""

from __future__ import annotations

from typing import Callable, Mapping, Optional

# An outgoing call has no WhatsApp call id until the offer comes back, so it is
# tracked under a synthetic one. Anything carrying this prefix is a placeholder
# and must never be compared against a real id.
OUTGOING_ID_PREFIX = "outgoing:"


def is_placeholder_call_id(call_id: str) -> bool:
    """Is this our own stand-in id rather than one WhatsApp minted?"""
    return bool(call_id) and str(call_id).startswith(OUTGOING_ID_PREFIX)


def call_event_matches_active(
    active: Optional[Mapping],
    call_id: str,
    peer_jid: str,
    same_peer: Optional[Callable[[str, str], bool]] = None,
) -> bool:
    """Whether a ``callstate`` event describes the call currently held.

    ``same_peer`` compares two peer JIDs across the ``@lid`` ↔ phone bridge;
    left out, it falls back to string equality, which is the behaviour this
    function was extracted to fix and is only good enough for a non-LID
    account.

    Once the held call has a *real* WhatsApp id, only that id matches: a
    delayed terminal event from an earlier call with the same person must not
    tear down the current one. Until then the peer is all there is to go on.
    """
    if not active:
        return False
    if same_peer is None:
        same_peer = lambda left, right: bool(left) and left == right

    call_id = str(call_id or "")
    peer_jid = str(peer_jid or "")
    active_call_id = str(active.get("call_id") or "")
    active_identity = str(active.get("identity") or "")
    active_peer = str(active.get("peer_jid") or "")

    has_bound_call_id = bool(active_call_id) and not is_placeholder_call_id(active_call_id)
    if call_id and has_bound_call_id:
        return call_id in {active_call_id, active_identity}
    if call_id and call_id == active_identity:
        return True
    return bool(peer_jid and active_peer and same_peer(peer_jid, active_peer))
