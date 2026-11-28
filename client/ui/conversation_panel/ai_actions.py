"""AIActionsMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Transcribe a voice message, describe a photo, sticker or video, or turn a PDF
into accessible text, with the AI providers the person set up in Settings.

The single entry point is ``_on_ai_action``: the context-menu item, the
Describe button and the Ctrl+Shift+I accelerator all end there. Which
provider answers, in which order and under which consent is core/ai_media's
business; this file only drives message -> bounded download -> window.

Methods run with ``self`` bound to the ConversationsPanel instance.
"""
import copy
import os

import wx
from cryptography.fernet import InvalidToken

from app_paths import active_account_id, global_dir
from app_settings import AppSettings
from core.ai_credentials import CredentialStore
from core.ai_media import config as ai_config
from core.ai_media import modes
from core.ai_media.config import MAX_SOURCE_BYTES, MENU_KEY, preferences
from core.ai_media.context import context_text
from core.ai_media.errors import DescriptionError
from core.ai_media.local_text import offline_ocr_kind
from core.ai_media.saved import saved_description
from core.utils import decrypt_bytes
from ui.ai_media_launch import close_open_dialog
from ui.conversation_panel.media_paths import cached_media_path


def encrypted_limit(limit):
    """Largest encrypted cache file that can hold ``limit`` bytes of media:
    Fernet expands the payload by a third plus its header."""
    return limit * 4 // 3 + 1024


class AIActionsMixin:
    """AI transcription and description of a message's media."""

    def _ai_settings(self):
        mw = self.main_window
        app = getattr(mw, "_app_settings", None) or AppSettings(global_dir())
        return app, preferences(app)

    def _ai_menu_label(self, msg, i18n, config=None):
        """Context-menu label for this message, or "" when no AI action is
        offered on it (feature off, nothing usable for its kind, view-once...).
        *config*: the preferences already read by the caller (one app.json
        read per focused row, not two)."""
        if config is None:
            _app, config = self._ai_settings()
        kind = ai_config.offered_kind(msg, config, CredentialStore(global_dir()).saved())
        if kind:
            return i18n.t(MENU_KEY[kind])
        if offline_ocr_kind(msg):
            return i18n.t("ai_read_text_offline")
        return ""

    def _append_ai_quick_menu_items(self, menu, msg, i18n, config=None):
        """Append fast media actions (Offline OCR, Understand full video, Quick analysis)
        next to the primary AI item when AI providers already handle this message."""
        if config is None:
            _app, config = self._ai_settings()
        offered = ai_config.offered_kind(msg, config, CredentialStore(global_dir()).saved())
        if offered is None:
            return
        if offline_ocr_kind(msg) is not None:
            ocr_item = menu.Append(wx.ID_ANY, i18n.t("ai_read_text_offline"))
            self.Bind(
                wx.EVT_MENU,
                lambda e, m=msg: self._on_ai_action(message=m, initial_action="offline_ocr"),
                ocr_item,
            )
        if offered == "video":
            full_item = menu.Append(wx.ID_ANY, i18n.t("ai_full_video"))
            self.Bind(
                wx.EVT_MENU,
                lambda e, m=msg: self._on_ai_action(message=m, initial_action="full_video"),
                full_item,
            )
        if modes.offered(offered):
            quick_menu = wx.Menu()
            for mode in ("text_only", "receipt", "table"):
                item = quick_menu.Append(wx.ID_ANY, i18n.t(modes.MODE_KEY[mode]))
                self.Bind(
                    wx.EVT_MENU,
                    lambda e, m=msg, md=mode: self._on_ai_action(message=m, initial_mode=md),
                    item,
                )
            menu.AppendSubMenu(quick_menu, i18n.t("ai_mode_label"))

    def _update_ai_describe_button(self, msg):
        """Show the Describe/Transcribe button under the same rule as the
        context-menu item: only on a message the feature offers an action for."""
        button = self._action_describe_btn
        # Settings can leave the button out; the menu item stays.
        _app, config = self._ai_settings()
        hidden = config.get("hide_action_button") is True
        label = "" if hidden else self._ai_menu_label(msg, self.main_window.i18n, config)
        if label:
            button.SetLabel(label)
            button.Show()
        else:
            button.Hide()

    def _on_ai_describe_button(self, event=None):
        """The button acts on the selected message (focus is on the button)."""
        self._on_ai_action(message=self._focused_message())

    def _focused_message(self):
        """The message under focus in the message list, or the selected one
        while the Describe button has focus; None anywhere else."""
        focus = wx.Window.FindFocus()
        button = getattr(self, "_action_describe_btn", None)
        if focus is not None and focus is button:
            index = self.messages_list.GetFirstSelected()
        elif focus is self.messages_list:
            index = self.messages_list.GetFocusedItem()
        else:
            return None
        if index < 0 or index >= len(self._sorted_messages):
            return None
        return self._sorted_messages[index]

    def _on_ai_action(self, event=None, message=None, parent=None, initial_action=None, initial_mode=None):
        """Menu item and Ctrl+Shift+I: act on ``message``, or on the focused one.
        ``parent`` is the modal window the action was started from (the media
        viewer); the result window opens over it and focus stays there."""
        if message is None:
            message = self._focused_message()
        kind = ai_config.eligible_kind(message)
        if kind is None:
            return
        mw = self.main_window
        app, config = self._ai_settings()
        offered = ai_config.offered_kind(message, config, CredentialStore(global_dir()).saved())
        offline_only = offered is None and offline_ocr_kind(message) is not None
        if offered is None and not offline_only:
            mw.output(mw.i18n.t("ai_disabled"))
            return
        chat = (message.get("key") or {}).get("remoteJid") or (self.conversation or {}).get("remoteJid", "")
        locked = mw.is_chat_locked(chat)
        if locked and not getattr(mw, "_chat_lock_unlocked", False):
            return
        identity = (active_account_id(), chat, message["key"]["id"])
        existing = getattr(self, "_ai_dialog", None)
        if existing is not None:
            existing.Raise()
            return  # No second window or duplicate billed request.
        snapshot = copy.deepcopy(message)
        msg_type = snapshot["messageType"]
        info = (snapshot.get("message") or {}).get(msg_type) or {}
        loader = self._ai_loader(snapshot, kind, cached_media_path(msg_type, identity[2]), mw.key)
        from ui.dialogs.ai_result_dialog import AIResultDialog
        # Only a video of this chat can be understood with its speech: the
        # window gets what it needs to transcribe it (ai_full_video.py).
        hooks = getattr(self, "_full_video_hooks", None) if kind == "video" and not offline_only else None
        context = "" if offline_only else (
            context_text(snapshot, config) if kind in ("image", "sticker", "video") else "")
        dialog = AIResultDialog(parent or self, identity, kind, locked, config, app, loader, str(info.get("mimetype") or ""),
                                saved_text=saved_description(snapshot, kind),
                                context=context,
                                **({"full_video": hooks(snapshot, chat)} if hooks else {}),
                                **({"offline_only": True} if offline_only else {}),
                                **({"initial_action": initial_action} if initial_action else {}),
                                **({"initial_mode": initial_mode} if initial_mode else {}))
        self._ai_dialog = dialog
        try:
            dialog.ShowModal()
        finally:
            dialog._dispose()
            self._ai_dialog = None
            dialog.Destroy()
            if parent is None:
                self._restore_focus_after_ai(identity, locked)

    def _ai_loader(self, snapshot, kind, path, key):
        """Callable that returns the decrypted original, never reading more than
        the kind's size limit even when the message's own metadata understates it."""
        mw = self.main_window
        msg_type = snapshot["messageType"]
        limit = MAX_SOURCE_BYTES[kind]

        def loader(token):
            info = (snapshot.get("message") or {}).get(msg_type) or {}
            try:
                size = int(info.get("fileLength", 0))
            except (TypeError, ValueError):
                size = 0
            if size > limit:
                raise DescriptionError("media_size")
            if not os.path.isfile(path):
                fetch = mw.handle_audio_message if msg_type == "audioMessage" else mw.handle_media_message
                fetch(snapshot, max_bytes=limit, cancel_check=token.check)
            token.check()
            if not os.path.isfile(path):
                raise DescriptionError("media")
            cap = encrypted_limit(limit)
            try:
                with open(path, "rb") as stream:
                    encrypted = stream.read(cap + 1)
                if len(encrypted) > cap:
                    raise DescriptionError("media_size")
                return decrypt_bytes(encrypted, key)
            except (OSError, InvalidToken, ValueError, TypeError):
                raise DescriptionError("media") from None
        return loader

    def _restore_focus_after_ai(self, identity, locked):
        """Put focus back on the message, found again by identity: incoming
        messages can reorder the rows while the window was open."""
        mw = self.main_window
        current_chat = (self.conversation or {}).get("remoteJid", "")
        if not (mw.IsShown() and not getattr(mw, "_shutting_down", False)
                and current_chat == identity[1]
                and (not locked or getattr(mw, "_chat_lock_unlocked", False))):
            return
        for index, msg in enumerate(self._sorted_messages):
            if (msg.get("key") or {}).get("id") == identity[2]:
                self.messages_list.Focus(index)
                self.messages_list.Select(index)
                break
        self.messages_list.SetFocus()

    def close_ai_media(self, *, locked_only=False, message_ids=None):
        """Close the open AI window (and so drop its media and answers): on
        leaving the chat, locking the vault, hiding or closing the app, or
        deleting its message."""
        if message_ids is None:
            # A summary's consent window waits for an answer before any voice
            # is transcribed; with the vault locked, only a locked chat's.
            self.close_summary_consent(locked_only=locked_only)
        if locked_only:
            self.cancel_summary_voice_locked()  # its progress window closes at once
        elif message_ids is None:
            close_open_dialog()  # one under the Status tab or its media viewer
            self.cancel_summary_voice()  # a summary's voice step of this chat
        dialog = getattr(self, "_ai_dialog", None)
        if dialog is None:
            return
        if locked_only and not (dialog.session.locked or self.main_window.is_chat_locked(dialog.session.identity[1])):
            return
        if message_ids is not None and dialog.session.identity[2] not in message_ids:
            return
        dialog._close()
