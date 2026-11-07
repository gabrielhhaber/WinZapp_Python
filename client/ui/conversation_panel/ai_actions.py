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
from core.ai_media.config import MAX_SOURCE_BYTES, MENU_KEY, preferences
from core.ai_media.errors import DescriptionError
from core.ai_media.summary import build_unread_transcript
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
        return i18n.t(MENU_KEY[kind]) if kind else ""

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

    def _on_ai_action(self, event=None, message=None, parent=None):
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
        if ai_config.offered_kind(message, config, CredentialStore(global_dir()).saved()) is None:
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
        dialog = AIResultDialog(parent or self, identity, kind, locked, config, app, loader, str(info.get("mimetype") or ""))
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
        if not locked_only and message_ids is None:
            close_open_dialog()  # one under the Status tab or its media viewer
        dialog = getattr(self, "_ai_dialog", None)
        if dialog is None:
            return
        if locked_only and not (dialog.session.locked or self.main_window.is_chat_locked(dialog.session.identity[1])):
            return
        if message_ids is not None and dialog.session.identity[2] not in message_ids:
            return
        dialog._close()

    # ── Unread-messages summary (kind "text") ────────────────────────────────
    def _unread_slice(self) -> list:
        """The unread tail of the open conversation, oldest first.

        Not the conversation dict's unreadCount: mark_conversation_as_read()
        zeroes it as soon as the chat opens, so by the time anyone could ask
        for a summary the count was always 0 (reported live: a group with 93
        unread heard "not enough unread messages"). The panel's unread
        separator keeps the real answer: it is placed from the count
        snapshotted at open, summed and moved as messages arrive live, and
        dismissed only once focus passes it — when the user actually reached
        what sits below. The summary covers exactly the messages that
        separator announces.
        """
        if not self.conversation:
            return []
        messages = getattr(self, "_sorted_messages", None) or []
        sep_idx = getattr(self, "_unread_sep_idx", -1)
        if not (0 <= sep_idx < len(messages)) or not self._is_separator(messages[sep_idx]):
            # No separator row at the recorded index (mid-rebuild, or the
            # anchor pair is all that survived): fall back to the anchor id.
            sep_idx = -1
            anchor = getattr(self, "_first_unread_msg_id", None)
            if anchor:
                for idx, msg in enumerate(messages):
                    if not isinstance(msg, dict) or self._is_separator(msg):
                        continue
                    if (msg.get("key") or {}).get("id") == anchor:
                        sep_idx = idx - 1
                        break
            if sep_idx < 0:
                return []
        return [m for m in messages[sep_idx + 1:]
                if isinstance(m, dict) and not self._is_separator(m)]

    def _summary_offered(self, config=None) -> bool:
        """The button and Alt+Shift+U share this rule: feature on, at least one
        provider with a key that takes text, a conversation open and unlocked,
        and unread messages at or over the configured minimum (0 = never)."""
        if config is None:
            _app, config = self._ai_settings()
        if not config.get("enabled"):
            return False
        minimum = config.get("summary_min_unread", 10)
        if minimum <= 0 or not self.conversation:
            return False
        mw = self.main_window
        chat = self.conversation.get("remoteJid", "")
        if mw.is_chat_locked(chat) and not getattr(mw, "_chat_lock_unlocked", False):
            return False
        saved = CredentialStore(global_dir()).saved()
        if not ai_config.chain(config, "text", lambda p: p in saved):
            return False
        return len(self._unread_slice()) >= minimum

    def _update_summary_button(self):
        button = getattr(self, "_summary_btn", None)
        if button is None:
            return
        try:
            offered = self._summary_offered()
        except Exception:
            offered = False
        if offered:
            # Kept in sync here (not only at construction) so a language
            # change with a chat open renames the button too.
            button.SetLabel(self.main_window.i18n.t("ai_summary_button"))
            button.Show()
        else:
            button.Hide()
        # The slot's visibility is decided by _sync_media_action_slot_visibility()
        # (the summary button is one of its controls); a bare Layout() here
        # left the slot hidden when the summary was the only action offered.
        sync = getattr(self, "_sync_media_action_slot_visibility", None)
        if sync is not None:
            sync()

    def _on_summary_action(self, event=None):
        """Button and Alt+Shift+U: summarize the unread tail of this chat with
        the configured AI providers, in the same accessible result window a
        description uses (kind "text")."""
        mw = self.main_window
        app, config = self._ai_settings()
        if not self._summary_offered(config):
            mw.output(mw.i18n.t("ai_summary_not_available"), interrupt=True)
            return
        transcript, included = build_unread_transcript(
            self._unread_slice(), self._sender_label)
        if not transcript:
            mw.output(mw.i18n.t("ai_summary_not_available"), interrupt=True)
            return
        chat = self.conversation.get("remoteJid", "")
        locked = mw.is_chat_locked(chat)
        existing = getattr(self, "_ai_dialog", None)
        if existing is not None:
            existing.Raise()
            return  # No second window or duplicate billed request.
        data = transcript.encode("utf-8")
        loader = lambda operation: data
        identity = (active_account_id(), chat, "unread-summary")
        from ui.dialogs.ai_result_dialog import AIResultDialog
        dialog = AIResultDialog(self, identity, "text", locked, config, app,
                                loader, "text/plain")
        self._ai_dialog = dialog
        try:
            dialog.ShowModal()
        finally:
            dialog._dispose()
