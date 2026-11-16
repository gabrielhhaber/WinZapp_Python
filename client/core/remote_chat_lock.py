"""A phone lock updates the existing fingerprint index and closes its view."""
from core.remote_conversation_close import close_for_remote_change
from core.remote_message_state import chat_aliases


def apply_phone_lock(mw, jid, locked):
    chats = mw.chats
    aliases = chat_aliases(mw, jid)
    changed = mw._sync_phone_chat_lock({"isLocked": locked}, jid, chats)
    # The existing synchronizer knows the counterpart even without a chat row.
    if changed:
        mw._persist_phone_locked_chats()
    panel = getattr(mw, "conversations_panel", None)
    if locked:
        close_for_remote_change(mw, aliases, "remote_chat_locked_closed")
        if hasattr(mw, "_invalidate_star_sync_for_lock"):
            mw._invalidate_star_sync_for_lock()
        if panel is not None and hasattr(panel, "close_ai_media"):
            panel.close_ai_media(locked_only=True)
        if panel is not None and hasattr(panel, "drop_locked_translations"):
            panel.drop_locked_translations()
    mw._refresh_chat_lock_navigation()
    mw._schedule_set_chats()
