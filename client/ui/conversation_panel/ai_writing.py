"""Writing help on the composer's draft, with preview before replacement.

The writing window owns separate provider and Google web consent. It does
not send messages. Received-message translation stays in the existing
TranslationActionsMixin rather than adding another menu entry.
"""
from app_paths import global_dir
from app_settings import AppSettings
from core.ai_media.config import MAX_SOURCE_BYTES, preferences, utf8_bytes
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

    def _on_writing_help_shortcut(self, event=None):
        """The shortcut: the same as the button, which is disabled (and so is
        this) while the draft cannot be helped."""
        if self._writing_help_offered():
            self._on_writing_help()

    def _on_writing_help(self, event=None):
        """The composer menu item: open the writing-help window on the draft."""
        existing = getattr(self, "_writing_dialog", None)
        if existing is not None:
            existing.Raise()
            return  # No second window or duplicate request.
        draft = self.message_field.GetValue()
        if not draft.strip():
            return
        mw = self.main_window
        app, config = self._ai_prefs()
        locked = mw.is_chat_locked((self.conversation or {}).get("remoteJid", ""))
        from ui.dialogs.ai_writing_dialog import WritingHelpDialog
        dialog = WritingHelpDialog(self, draft, locked, config, app)
        self._writing_dialog = dialog
        try:
            dialog.ShowModal()
        finally:
            dialog._dispose()
            self._writing_dialog = None
            dialog.Destroy()
            if mw.IsShown() and not getattr(mw, "_shutting_down", False):
                self.message_field.SetFocus()
