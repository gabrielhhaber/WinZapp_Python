"""Keep storage protections and transactions consistent with the older cleaner."""
from core import media_cleanup

from .references import chat_token
from .types import canonical


def execute(preview, roots, store, shared_store, media_index, cache_id, *, progress=None, cancel=lambda: False):
    # Same lock order as new storage operations, including index bookkeeping.
    with shared_store.lock(), store.lock():
        own = store.preferences(strict=True)
        shared = shared_store.preferences(strict=True)
        protected = own.protected | shared.protected
        categories = own.protected_categories | shared.protected_categories
        allowed = []
        for candidate in preview.candidates:
            category = "voice" if candidate.category in ("audios", "voice_messages") else "media"
            if (canonical(candidate.path) not in protected and category not in categories
                    and chat_token(candidate.chat_jid) not in own.protected_chats):
                allowed.append(candidate)
        if cancel():
            return media_cleanup.CleanupStats(cancelled=True)
        # Do not let the old UI race a new transaction's block rollback, or
        # announce a deletion before its automatic-download block is durable.
        added = media_index.update_durable([cache_id(c.message_id) for c in allowed])
        stats = media_cleanup.execute(media_cleanup.CleanupPreview(allowed), roots,
                                      progress_cb=progress, should_cancel=cancel)
        stats.files_protected = len(preview.candidates) - len(allowed)
        deleted = {cache_id(key) for key in stats.deleted_ids}
        if added - deleted:
            media_index.update_durable(added - deleted, remove=True)
        return stats
