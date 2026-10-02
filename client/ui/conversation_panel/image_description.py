"""Single entry point for the photo menu and keyboard accelerator."""
import copy
import os
import wx
from cryptography.fernet import InvalidToken

from app_paths import active_account_id, global_dir
from app_settings import AppSettings
from core.image_description.config import MAX_SOURCE_BYTES, eligible_photo, preferences
from core.image_description.errors import DescriptionError
from core.utils import decrypt_bytes
from ui.conversation_panel.media_paths import cached_media_path


class ImageDescriptionMixin:
    def _on_describe_photo(self, event=None, message=None):
        if message is None:
            if wx.Window.FindFocus() is not self.messages_list:
                return
            index = self.messages_list.GetFocusedItem()
            if index < 0 or index >= len(self._sorted_messages):
                return
            message = self._sorted_messages[index]
        if not eligible_photo(message):
            return
        mw = self.main_window
        app = getattr(mw, "app_settings", None) or AppSettings(global_dir())
        config = preferences(app)
        if not config["enabled"]:
            mw.output(mw.i18n.t("ai_disabled"))
            return
        chat = (message.get("key") or {}).get("remoteJid") or (self.conversation or {}).get("remoteJid", "")
        locked = mw.is_chat_locked(chat)
        if locked and not getattr(mw, "_chat_lock_unlocked", False):
            return
        identity = (active_account_id(), chat, message["key"]["id"])
        existing = getattr(self, "_image_description_dialog", None)
        if existing is not None:
            existing.Raise()
            return  # No second dialog or duplicate billed request.
        snapshot = copy.deepcopy(message)
        path = cached_media_path("imageMessage", identity[2])
        key = mw.key
        def loader(token):
            info = snapshot["message"]["imageMessage"]
            try:
                size = int(info.get("fileLength", 0))
            except (TypeError, ValueError):
                size = 0
            if size > MAX_SOURCE_BYTES:
                raise DescriptionError("image_size")
            if not os.path.isfile(path):
                mw.handle_media_message(snapshot, timeout=15, max_bytes=MAX_SOURCE_BYTES,
                                        cancel_check=token.check)
            token.check()
            if not os.path.isfile(path):
                raise DescriptionError("media")
            # Fernet expands the encrypted payload; read at most the cap plus
            # one byte even if its metadata understates the size.
            limit = MAX_SOURCE_BYTES * 4 // 3 + 1024
            try:
                with open(path, "rb") as stream:
                    encrypted = stream.read(limit + 1)
                if len(encrypted) > limit:
                    raise DescriptionError("image_size")
                return decrypt_bytes(encrypted, key)
            except (OSError, InvalidToken, ValueError, TypeError):
                raise DescriptionError("media") from None
        from ui.dialogs.image_description_dialog import ImageDescriptionDialog
        dialog = ImageDescriptionDialog(self, identity, locked, config, app, loader)
        self._image_description_dialog = dialog
        try:
            dialog.ShowModal()
        finally:
            dialog._dispose()
            self._image_description_dialog = None
            dialog.Destroy()
            # Re-resolve by identity; incoming messages can reorder the rows.
            current_chat = (self.conversation or {}).get("remoteJid", "")
            if (mw.IsShown() and not getattr(mw, "_shutting_down", False)
                    and current_chat == chat
                    and (not locked or getattr(mw, "_chat_lock_unlocked", False))):
                for index, msg in enumerate(self._sorted_messages):
                    if (msg.get("key") or {}).get("id") == identity[2]:
                        self.messages_list.Focus(index)
                        self.messages_list.Select(index)
                        break
                self.messages_list.SetFocus()

    def close_image_description(self, *, locked_only=False, message_ids=None):
        dialog = getattr(self, "_image_description_dialog", None)
        if dialog is None:
            return
        if locked_only and not (dialog.session.locked or self.main_window.is_chat_locked(dialog.session.identity[1])):
            return
        if message_ids is not None and dialog.session.identity[2] not in message_ids:
            return
        dialog._close()
