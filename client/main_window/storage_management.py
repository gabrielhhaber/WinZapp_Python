"""Bridge storage's pure service to existing paths, local history and jobs."""
from __future__ import annotations

import threading

import app_paths
from core.storage_manager.references import collect
from core.storage_manager.roots import current_roots
from core.storage_manager.service import StorageService
from core.storage_manager.store import StorageStore
from core.storage_manager.types import canonical


class StorageContext:
    def __init__(self, window):
        self.window = window
        self.account = app_paths.data_path()
        self.chats = {}
        self.chat_jids = {}
        self._labels_lock = threading.RLock()
        self._labels_epoch = 0

    def forget_labels(self):
        with self._labels_lock:
            self._labels_epoch += 1
            self.chats, self.chat_jids = {}, {}

    def can_display(self, entry):
        if not entry.chat_key:
            return not entry.display_name
        with self._labels_lock:
            jid = self.chat_jids.get(entry.chat_key)
            known = jid in self.chats if jid is not None else False
        return known and not self.locked(jid)

    def allowed_chats(self):
        with self._labels_lock:
            names = dict(self.chats)
        return {jid: name for jid, name in names.items() if not self.locked(jid)}

    def has_hidden_labels(self, cached_keys=()):
        with self._labels_lock:
            known = dict(self.chat_jids)
        return any(not known.get(key) or self.locked(known[key]) for key in set(known) | set(cached_keys))

    def valid(self):
        return (not getattr(self.window, "_shutting_down", False)
                and canonical(app_paths.data_path()) == canonical(self.account))

    def capture_chats(self):
        """Called on the UI thread; workers never enumerate wx controls."""
        epoch = self._labels_epoch
        result = {}
        for attr in ("conversations_panel", "archived_conversations_panel"):
            panel = getattr(self.window, attr, None)
            if panel is None:
                continue
            chats = getattr(panel, "_all_chats_list", getattr(panel, "chats_list", []))
            names = getattr(panel, "_all_chat_names", getattr(panel, "chat_names", []))
            for chat, name in zip(chats or [], names or []):
                jid = chat.get("remoteJid", "") if isinstance(chat, dict) else ""
                if not jid or self.locked(jid):
                    continue
                # A data/identity fallback must not become a spoken raw JID.
                result[jid] = str(name) if name and str(name) != jid and "@" not in str(name) else ""
        from core.storage_manager.references import chat_token
        with self._labels_lock:
            if epoch == self._labels_epoch:
                self.chats = result
                self.chat_jids = {chat_token(jid): jid for jid in result}

    def locked(self, jid):
        try:
            return (self.window.is_chat_locked(jid)
                    or self.window.is_chat_hidden_by_vault(jid))
        except Exception:
            return True

    def references(self, cancel, preferences=None):
        from ui.conversation_panel.media_paths import cached_media_path, media_cache_id
        from core.message_stars import is_local_star
        from core.utils import group_media_category
        db = getattr(self.window, "db", None)
        names = self.allowed_chats()
        preferences = preferences if preferences is not None else self.window._storage_service.preferences
        if preferences.match_chats is not None:
            from core.storage_manager.references import chat_token
            names = {jid: label for jid, label in names.items() if chat_token(jid) in preferences.match_chats}
        if db is None:
            from core.storage_manager.references import References
            return References({}, complete=False)
        return collect([jid for jid in names if not self.locked(jid)],
                       db.get_message_count, db.get_messages_asc, cached_media_path, media_cache_id,
                       cancel=cancel,
                       protected=lambda m: bool(m.get("starred") or is_local_star(m)),
                       media_type_for=lambda m: "voiceMessage" if group_media_category(m) == "voice_messages" else m.get("messageType", ""),
                       label_for=lambda jid, message: names.get(jid, ""))

    def busy(self, entry):
        if not self.valid():
            return True
        if entry.media_id:
            if not self.can_display(entry):
                return True
            from ui.conversation_panel.media_paths import media_cache_id
            # A job may already have buffered content and released its file
            # handle. Respect its owner as well as the native sharing check.
            for field in ("_auto_transcription_current", "_transcription_background_job", "_background_transcription"):
                if getattr(self.window, field, None) is not None:
                    return True
            jid = self.chat_jids.get(entry.chat_key)
            if jid is not None and self.locked(jid):
                return True
            for attr in ("conversations_panel", "archived_conversations_panel", "locked_conversations_panel"):
                panel = getattr(self.window, attr, None)
                if panel is None:
                    continue
                if getattr(panel, "_is_recording", False):
                    return True
                for field in ("_transcription_job", "_bulk_transcription", "_ai_media_job"):
                    job = getattr(panel, field, None)
                    if job is not None and getattr(job, "running", not getattr(job, "done", False)):
                        return True
                if getattr(panel, "_ai_dialog", None) is not None:
                    return True
                for field in ("_current_audio_id", "_current_video_msg_id"):
                    active = getattr(panel, field, None)
                    if active and media_cache_id(str(active)) == entry.media_id:
                        return True
        queue = getattr(self.window, "message_queue", None)
        lock = getattr(queue, "_lock", None)
        if lock is not None:
            if not lock.acquire(blocking=False):
                return True
            try:
                for pending in queue._pending.values():
                    for path in (pending.media_path, pending.audio_path):
                        if path and canonical(path) == entry.key:
                            return True
            finally:
                lock.release()
        return False


def service_for(window):
    current = getattr(window, "_storage_service", None)
    if current is not None:
        return current
    import wx
    context = StorageContext(window)
    service = StorageService(StorageStore(context.account), window._media_cleaned_index(),
                             lambda preferences: current_roots(window, preferences),
                             context.references, wx.CallAfter,
                             busy=context.busy, valid=context.valid)
    service.shared_store = StorageStore(app_paths.global_dir())
    service.scoped_references = context.references
    from core.storage_manager.roots import capture_other_accounts
    service.capture_accounts = capture_other_accounts
    service.shared_preferences = lambda: service.shared_store.preferences(strict=True)
    window._storage_context = context
    window._storage_service = service
    return service


def open_manager(parent, window):
    from ui.dialogs.storage_manager import StorageManagerDialog
    dialog = StorageManagerDialog(parent, window, service_for(window))
    window._storage_dialog_open = True
    service = window._storage_service
    service.register_view(dialog)
    try:
        if dialog.privacy_epoch != service.visibility_epoch:
            dialog.clear_private_state()
        dialog.ShowModal()
        return dialog.requested_owner
    finally:
        window._storage_dialog_open = False
        service.unregister_view(dialog)
        dialog.Destroy()
