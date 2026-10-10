"""Bounded post-wake refresh, independent of WhatsApp's chat-list markers."""

import logging

from core.incremental_sync import chat_sync_marker, timestamp_seconds

RESUME_RECHECK_PER_ROUND = 20


def promote_resume_rechecks(window, skipped_chats, incremental_targets, reasons):
    """Promote warm chats not actually queried since wake, active/recent first.

    The existing verification map supplies completion; I/O faults and empty
    deltas keep using the existing retry mechanisms. No phone history request
    or cache wipe is involved. A fresh wake replaces the cutoff, so a second
    suspension also rechecks chats that the first recovery already covered.
    """
    since = getattr(window, "_resume_message_sync_since", 0)
    if not isinstance(since, (int, float)) or since <= 0:
        return skipped_chats
    verified = getattr(window, "_chat_verified_at", None)
    verified = verified if isinstance(verified, dict) else {}
    panel = getattr(window, "conversations_panel", None)
    conversation = getattr(panel, "conversation", None)
    active = conversation.get("remoteJid", "") if isinstance(conversation, dict) else ""

    def forms(jid):
        try:
            return {jid, *(window._normalize_jid(f) for f in
                           window._jid_address_forms(jid) if f)}
        except Exception:
            return {jid}

    active_forms = forms(window._normalize_jid(active)) if active else set()

    def priority(item):
        jid, chat = item
        marker = chat_sync_marker(chat)
        recent = max(timestamp_seconds(marker["activity"]),
                     timestamp_seconds(marker["newest_local_ts"]))
        return (not bool(forms(jid) & active_forms), -recent, jid)

    due = []
    for jid, chat in skipped_chats:
        if not chat_sync_marker(chat)["record_count"]:
            continue  # Empty shells have no newer cached history to verify.
        checked = []
        for form in forms(jid):
            try:
                checked.append(float(verified.get(form, 0) or 0))
            except (TypeError, ValueError):
                checked.append(0)
        if max(checked, default=0) < since:
            due.append((jid, chat))
    due.sort(key=priority)
    chosen = due[:RESUME_RECHECK_PER_ROUND]
    chosen_jids = {jid for jid, _ in chosen}
    for jid, chat in chosen:
        incremental_targets.append(chat)
        reasons[jid] = "resume-recheck"
        logging.info("[resume-message-sync] %s: querying despite unchanged metadata", jid)
    if due:
        logging.info("[resume-message-sync] selected=%d deferred=%d budget=%d",
                     len(chosen), len(due) - len(chosen), RESUME_RECHECK_PER_ROUND)
    # Changed chats and retries already selected also respect active/recent
    # priority. The existing worker pool retains its concurrency limit.
    incremental_targets.sort(key=lambda chat: priority((
        window._normalize_jid(chat.get("remoteJid", "")), chat)))
    return [(jid, chat) for jid, chat in skipped_chats if jid not in chosen_jids]
