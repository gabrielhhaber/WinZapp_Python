"""Chats a sync round never asked about because the session was down.

``sync_chat_messages()`` does not query a chat while ``_wa_connected`` is
False: it keeps the cached messages and reports the chat failed, which is
right, since nothing was read. But a session coming back from suspend flaps
for a few seconds (Neonize, 2026-11-11: socket up, keep-alive timeout,
INITIALIZING, authenticated again 7 s later), and every chat the round
reached in that window failed without a request. The round then ended
"incomplete" and the health checker ran a second one about two and a half
minutes later, announcing "Atualizando conversas" out of nowhere.

So the chats skipped that way are noted, and at the end of the message phase
the round asks them once more, if the session is back. They stay genuine
failures (CLAUDE.md, "Sync"): a chat that fails again is still in
``message_failures`` and the health checker retries it as before. Only one
extra pass, and only for chats that were never asked, so a server that
really fails costs nothing more.
"""
import logging
import threading
import time

# How long the end of the message phase waits for the session to come back
# before leaving the skipped chats to the health checker.
RECONNECT_WAIT = 20.0

_LOCK = threading.Lock()


def note_skipped_while_disconnected(mw, jid):
    """*jid* (normalized) was not queried because the session was down."""
    if not jid:
        return
    with _LOCK:
        skipped = getattr(mw, "_skipped_while_disconnected", None)
        if not isinstance(skipped, set):
            skipped = mw._skipped_while_disconnected = set()
        skipped.add(jid)


def take_skipped_while_disconnected(mw) -> set:
    """The chats noted so far, emptying the note."""
    with _LOCK:
        skipped = getattr(mw, "_skipped_while_disconnected", None)
        mw._skipped_while_disconnected = set()
    return set(skipped) if isinstance(skipped, set) else set()


def split_retry_targets(full_targets, incremental_targets, jids, normalize):
    """The round's targets whose normalized JID is in *jids*, each kept in
    the pass (full or incremental) it was planned for."""
    def pick(targets):
        return [chat for chat in targets or ()
                if isinstance(chat, dict)
                and (normalize(chat.get("remoteJid", "")) or chat.get("remoteJid", "")) in jids]
    return pick(full_targets), pick(incremental_targets)


def wait_until_connected(mw, still_current, timeout=RECONNECT_WAIT,
                         clock=time.monotonic, sleep=time.sleep) -> bool:
    """True once ``_wa_connected``; False after *timeout* or when the round
    stops being current."""
    deadline = clock() + timeout
    while True:
        if not still_current():
            return False
        if getattr(mw, "_wa_connected", False) is True:
            return True
        if clock() >= deadline:
            return False
        sleep(0.5)


def retry_skipped_while_disconnected(mw, full_targets, incremental_targets,
                                     failures, run_id, still_current,
                                     wait=wait_until_connected) -> set:
    """*failures* after asking again, once, the failed chats the round skipped
    while disconnected. Returns the failures that remain."""
    skipped = take_skipped_while_disconnected(mw) & set(failures)
    if not skipped:
        return failures
    if not wait(mw, still_current):
        logging.info(
            "[sync-reconnect-retry] %d chat(s) skipped while disconnected; "
            "the session is not back, left to the health checker.", len(skipped))
        return failures
    normalize = getattr(mw, "_normalize_jid", None)
    if not callable(normalize):
        def normalize(jid):
            return jid
    full, incremental = split_retry_targets(
        full_targets, incremental_targets, skipped, normalize)
    if not full and not incremental:
        return failures
    logging.info(
        "[sync-reconnect-retry] asking again %d chat(s) skipped while "
        "disconnected (%d full, %d incremental).",
        len(full) + len(incremental), len(full), len(incremental))
    again = set()
    if full:
        again |= set(mw.sync_remote_chats(full, incremental=False,
                                          expected_run_id=run_id, quiet=True) or ())
    if incremental:
        again |= set(mw.sync_remote_chats(incremental, incremental=True,
                                          expected_run_id=run_id, quiet=True) or ())
    retried = {normalize(chat.get("remoteJid", "")) or chat.get("remoteJid", "")
               for chat in full + incremental}
    remaining = (set(failures) - retried) | again
    logging.info("[sync-reconnect-retry] %d of %d still failed.",
                 len(again), len(retried))
    return remaining
