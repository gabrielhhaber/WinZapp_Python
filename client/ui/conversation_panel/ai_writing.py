"""Writing help on the composer's draft, with preview before replacement.

The writing window owns separate provider and Google web consent. It does
not send messages. Received-message translation stays in the existing
TranslationActionsMixin rather than adding another menu entry.
"""
from app_paths import global_dir
from app_settings import AppSettings
from core.ai_credentials import CredentialStore
from core.ai_media import config as ai_config
from core.ai_media import writing
from core.ai_media.config import MAX_SOURCE_BYTES, preferences, utf8_bytes
from core.ai_media.summary import build_unread_transcript
from core.ai_media.writing import REPLY_MESSAGES, REPLY_TRANSCRIPT_CHARS
from core.message_copy_format import format_copied_message
from ui.accessible import AccessibleWritingHelp
from ui.shortcut_bindings import popup_item_label


class AIWritingMixin:
    """Writing help on the draft. Received translation stays in TranslationActionsMixin."""

    def _create_writing_help_button(self, parent):
        """A native, named button immediately after Emojis in tab order."""
        import wx
        label = self.main_window.i18n.t("writing_menu")
        button = wx.Button(parent, label=label, name=label)
        button.SetAccessible(AccessibleWritingHelp())
        button.Bind(wx.EVT_BUTTON, self._on_writing_help)
        button.Enable(self._writing_help_offered())
        return button

    def _sync_writing_help_button(self):
        button = getattr(self, "_writing_help_btn", None)
        if button is not None:
            button.Enable(self._writing_help_offered())

    def _ai_prefs(self):
        mw = self.main_window
        app = getattr(mw, "_app_settings", None) or AppSettings(global_dir())
        return app, preferences(app)

    def _writing_help_offered(self, text=None):
        """The composer menu offers writing help on a non-empty draft short
        enough to send; translation works without any provider key."""
        if text is None:
            field = getattr(self, "message_field", None)
            if field is None:
                return False
            text = field.GetValue()
        field = getattr(self, "message_field", None)
        if field is not None:
            if not getattr(field, "IsEnabled", lambda: True)():
                return False
            if not getattr(field, "IsEditable", lambda: True)():
                return False
        text = text.strip()
        return bool(text) and len(utf8_bytes(text)) <= MAX_SOURCE_BYTES["writing"]

    def _writing_menu_label(self):
        """The menu item's text with the chord the person has set for it."""
        return popup_item_label(self, self.main_window.i18n.t("writing_menu"),
                                "messages.ID_WRITING_HELP")

    def _writing_has_providers(self):
        """Whether at least one AI provider/local model is ready for writing help."""
        _app, config = self._ai_prefs()
        saved = CredentialStore(global_dir()).saved()
        return bool(ai_config.chain(config, "writing", lambda p: p in saved))

    def _writing_quick_actions(self, text=None):
        """Fast writing actions for the composer context menu: [(action, i18n_key)]."""
        field = getattr(self, "message_field", None)
        if field is not None:
            if not getattr(field, "IsEnabled", lambda: True)():
                return []
            if not getattr(field, "IsEditable", lambda: True)():
                return []
            if text is None:
                text = field.GetValue()
        text = text or ""
        stripped = text.strip()
        has_draft = bool(stripped) and len(utf8_bytes(stripped)) <= MAX_SOURCE_BYTES["writing"]
        has_providers = self._writing_has_providers()
        has_recent = has_providers and bool(self._writing_recent_messages())
        actions = []
        if has_draft and has_providers:
            actions.append(("check", "writing_action_check"))
            actions.append(("professional", "writing_action_professional"))
            if has_recent:
                actions.append((writing.REPLY, "writing_action_reply"))
            actions.append(("translate", "writing_action_translate"))
        elif has_draft:
            actions.append(("translate", "writing_action_translate"))
        elif has_recent:
            actions.append((writing.REPLY, "writing_action_reply"))
        return actions

    def _on_writing_help_shortcut(self, event=None):
        """The shortcut: the same as the button, which is disabled (and so is
        this) while the draft cannot be helped."""
        if self._writing_help_offered():
            self._on_writing_help()

    def _writing_recent_messages(self):
        """The latest ``REPLY_MESSAGES`` messages of the open chat as one text
        (date, sender, words; "" when there are none), read only to offer a
        suggested reply: the window sends them after its own consent."""
        rows = [row for row in getattr(self, "_sorted_messages", ()) if not self._is_separator(row)]
        fmt = self.main_window.i18n.t("datetime_fmt")
        try:
            built = build_unread_transcript(
                rows,
                lambda row, text: format_copied_message(
                    self._extract_timestamp(row), self._summary_sender(row, None), text, fmt),
                lambda row: self._summary_text_of(row, {}),
                max_messages=REPLY_MESSAGES, max_chars=REPLY_TRANSCRIPT_CHARS)
        except Exception:
            return ""  # An odd row only hides the suggested reply.
        return built.text

    def _on_writing_help(self, event=None, initial_action=None, auto_run=False):
        """The composer menu item: open the writing-help window on the draft."""
        existing = getattr(self, "_writing_dialog", None)
        if existing is not None:
            existing.Raise()
            return  # No second window or duplicate request.
        draft = self.message_field.GetValue()
        recent = self._writing_recent_messages()
        if not draft.strip() and not (initial_action == writing.REPLY and recent):
            return
        mw = self.main_window
        app, config = self._ai_prefs()
        locked = mw.is_chat_locked((self.conversation or {}).get("remoteJid", ""))
        from ui.dialogs.ai_writing_dialog import WritingHelpDialog
        dialog = WritingHelpDialog(self, draft, locked, config, app, recent,
                                   initial_action=initial_action, auto_run=auto_run)
        self._writing_dialog = dialog
        try:
            dialog.ShowModal()
        finally:
            dialog._dispose()
            self._writing_dialog = None
            dialog.Destroy()
            if mw.IsShown() and not getattr(mw, "_shutting_down", False):
                self.message_field.SetFocus()
