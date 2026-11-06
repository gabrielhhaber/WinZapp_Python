"""Media cleanup dialog (Settings > Storage > Clean up downloaded media).

Two steps, as the engine (core/media_cleanup.py): choose conversations, media
types and a minimum size, read what that matches — files, megabytes — and only
then decide whether to delete. The messages stay; the downloaded copy goes.
"""

import logging
import threading

import wx

from app_paths import data_path, global_dir
from core import media_cleanup
from ui.dialogs.chat_picker import ChatCheckList

#: How much of a chat's history is read for the scan, as in the Media tab.
_HISTORY_LIMIT = 20000

_CATEGORY_LABEL_KEYS = {
    "photos": "cleanup_cat_photos",
    "videos": "cleanup_cat_videos",
    "audios": "cleanup_cat_audios",
    "voice_messages": "cleanup_cat_voice_messages",
    "documents": "cleanup_cat_documents",
}

_MB = 1024 * 1024


class MediaCleanupDialog(wx.Dialog):
    def __init__(self, parent, main_window):
        self.main_window = main_window
        i18n = main_window.i18n
        super().__init__(
            parent, title=i18n.t("cleanup_title"), size=(560, 700),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._i18n = i18n
        self._preview = None
        self._busy = False
        self._alive = True

        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        sizer.Add(wx.StaticText(panel, label=i18n.t("cleanup_chats_label")), 0, wx.ALL, 8)
        self._chat_picker = ChatCheckList(panel, i18n)
        sizer.Add(self._chat_picker, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        sizer.Add(wx.StaticText(panel, label=i18n.t("cleanup_types_label")), 0, wx.ALL, 8)
        self._category_checks = {}
        for category in media_cleanup.MEDIA_CATEGORIES:
            check = wx.CheckBox(panel, label=i18n.t(_CATEGORY_LABEL_KEYS[category]))
            check.SetValue(True)
            sizer.Add(check, 0, wx.LEFT | wx.RIGHT, 8)
            self._category_checks[category] = check

        sizer.Add(wx.StaticText(panel, label=i18n.t("cleanup_min_size_label")), 0, wx.ALL, 8)
        self._min_size_field = wx.TextCtrl(
            panel, value="0", style=wx.TE_DONTWRAP, name=i18n.t("cleanup_min_size_label"))
        sizer.Add(self._min_size_field, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self._scan_btn = wx.Button(panel, label=i18n.t("cleanup_scan_button"))
        self._scan_btn.Bind(wx.EVT_BUTTON, self._on_scan)
        sizer.Add(self._scan_btn, 0, wx.ALL, 8)

        sizer.Add(wx.StaticText(panel, label=i18n.t("cleanup_summary_label")), 0, wx.ALL, 8)
        self._summary = wx.TextCtrl(
            panel, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 130),
            name=i18n.t("cleanup_summary_label"))
        sizer.Add(self._summary, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        self._status = wx.StaticText(panel, label="")
        sizer.Add(self._status, 0, wx.ALL, 8)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self._proceed_btn = wx.Button(panel, label=i18n.t("cleanup_proceed_button"))
        self._proceed_btn.Bind(wx.EVT_BUTTON, self._on_proceed)
        self._proceed_btn.Disable()
        buttons.Add(self._proceed_btn, 0, wx.RIGHT, 8)
        close = wx.Button(panel, wx.ID_CLOSE, label=i18n.t("close").replace("&", ""))
        close.Bind(wx.EVT_BUTTON, lambda event: self.EndModal(wx.ID_CLOSE))
        buttons.Add(close, 0)
        sizer.Add(buttons, 0, wx.ALL | wx.ALIGN_RIGHT, 8)

        panel.SetSizer(sizer)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        self._load_chat_options()

    def _on_destroy(self, event):
        if event.GetEventObject() is self:
            self._alive = False
        event.Skip()

    def _say(self, text):
        if self._alive:
            self._status.SetLabel(text)

    # ── The conversations ───────────────────────────────────────────────────

    def _load_chat_options(self):
        """The two conversation lists are already in memory; read them here, on
        the UI thread (they belong to it) and hand the picker the options."""
        mw = self.main_window
        panels = []
        for name in ("conversations_panel", "archived_conversations_panel"):
            panel = getattr(mw, name, None)
            if panel is None:
                continue
            chats = getattr(panel, "_all_chats_list", None)
            names = getattr(panel, "_all_chat_names", None)
            if chats is None:
                chats = getattr(panel, "chats_list", [])
                names = getattr(panel, "chat_names", [])
            panels.append((list(chats or []), list(names or [])))

        def locked(jid):
            try:
                return bool(mw.is_chat_locked(jid)) or bool(mw.is_chat_hidden_by_vault(jid))
            except Exception:
                return True   # when it cannot tell, the chat is left out

        self._chat_picker.set_options(media_cleanup.chat_options(panels, locked), check_all=True)

    # ── Step 1: read what matches ────────────────────────────────────────────

    def _on_scan(self, event):
        if self._busy:
            return
        i18n = self._i18n
        jids = self._chat_picker.checked_jids()
        categories = [c for c, check in self._category_checks.items() if check.GetValue()]
        if not jids:
            wx.MessageBox(i18n.t("cleanup_no_chats_selected"), i18n.t("cleanup_title"),
                          wx.OK | wx.ICON_ERROR, self)
            return
        if not categories:
            wx.MessageBox(i18n.t("cleanup_no_categories_selected"), i18n.t("cleanup_title"),
                          wx.OK | wx.ICON_ERROR, self)
            return
        try:
            min_mb = float(self._min_size_field.GetValue().strip().replace(",", ".") or 0)
        except ValueError:
            min_mb = 0.0
        min_bytes = int(max(0.0, min_mb) * _MB)

        self._preview = None
        self._busy = True
        self._proceed_btn.Disable()
        self._scan_btn.Disable()
        self._summary.ChangeValue("")
        self._say(i18n.t("cleanup_scanning"))
        db = getattr(self.main_window, "db", None)

        def _work():
            records = {}
            for jid in jids:
                if not self._alive:
                    return
                try:
                    records[jid] = list(db.get_messages_asc(jid, limit=_HISTORY_LIMIT) or [])
                except Exception as exc:
                    logging.warning("[media-cleanup] reading a chat failed: %s",
                                    type(exc).__name__)
            preview = media_cleanup.scan(records, categories, min_bytes)
            wx.CallAfter(self._on_scanned, preview)

        threading.Thread(target=_work, daemon=True, name="winzapp-media-cleanup-scan").start()

    def _on_scanned(self, preview):
        if not self._alive:
            return
        i18n = self._i18n
        self._busy = False
        self._scan_btn.Enable()
        self._preview = preview
        self._say("")
        if preview.total_files == 0:
            self._summary.ChangeValue(i18n.t("cleanup_nothing_found"))
            self._proceed_btn.Disable()
            return
        lines = [i18n.t("cleanup_summary_header").format(
            chats=preview.chats_affected, files=preview.total_files,
            mb=preview.total_bytes / _MB), ""]
        for category, (count, total) in preview.by_category().items():
            lines.append(i18n.t("cleanup_summary_line").format(
                category=i18n.t(_CATEGORY_LABEL_KEYS.get(category, category)),
                files=count, mb=total / _MB))
        self._summary.ChangeValue("\n".join(lines))
        self._proceed_btn.Enable()
        self._summary.SetFocus()

    # ── Step 2: delete, after confirming ─────────────────────────────────────

    def _on_proceed(self, event):
        preview = self._preview
        if self._busy or preview is None or preview.total_files == 0:
            return
        i18n = self._i18n
        answer = wx.MessageBox(
            i18n.t("cleanup_confirm_msg").format(
                files=preview.total_files, mb=preview.total_bytes / _MB),
            i18n.t("cleanup_confirm_title"), wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING, self)
        if answer != wx.YES:
            return
        self._busy = True
        self._preview = None
        self._proceed_btn.Disable()
        self._scan_btn.Disable()
        self._say(i18n.t("cleanup_deleting"))
        roots = [data_path("media"), data_path("voice_messages")]
        from core.storage_manager.store import StorageStore
        from core.storage_manager.types import canonical
        account_store, shared_store = StorageStore(data_path()), StorageStore(global_dir())
        media_index = self.main_window._media_cleaned_index()

        def _progress(done, total):
            wx.CallAfter(self._say, i18n.t("cleanup_deleting_count").format(
                done=done, total=total))

        def _work():
            try:
                from core.storage_manager.legacy_cleanup import execute
                from ui.conversation_panel.media_paths import media_cache_id
                stats = execute(preview, roots, account_store, shared_store, media_index, media_cache_id,
                    progress=_progress, cancel=lambda: not self._alive
                    or getattr(self.main_window, "_shutting_down", False)
                    or canonical(data_path()) != canonical(account_store.directory))
            except Exception as exc:
                logging.warning("[media-cleanup] cleanup did not finish: %s",
                                type(exc).__name__)
                wx.CallAfter(self._storage_failed)
                return
            wx.CallAfter(self._on_done, stats)

        threading.Thread(target=_work, daemon=True, name="winzapp-media-cleanup-delete").start()

    def _storage_failed(self):
        if self._alive:
            self._busy = False
            self._scan_btn.Enable()
            self._say(self._i18n.t("storage_operation_failed"))

    def _on_done(self, stats):
        if not self._alive:
            return
        i18n = self._i18n
        self._busy = False
        self._scan_btn.Enable()
        self._say("")
        self._summary.ChangeValue("")
        message = i18n.t("cleanup_done").format(
            files=stats.files_deleted, mb=stats.bytes_freed / _MB)
        if stats.files_failed:
            message += "\n" + i18n.t("cleanup_done_failed").format(files=stats.files_failed)
        if getattr(stats, "files_protected", 0):
            message += "\n" + i18n.t("storage_legacy_skipped").format(count=stats.files_protected)
        self.main_window.output(message.replace("\n", " "))
        wx.MessageBox(message, i18n.t("cleanup_title"), wx.OK | wx.ICON_INFORMATION, self)
