"""AIResultDialog's quick analysis modes and opt-in context (picture, sticker,
video, PDF).

A mode only changes the request: the preset wording or the person's own
instruction (core/ai_media/modes.py). Its answer replaces the first one, like
"Run again": it is not a description, so it is never offered for "Put the
description in the line". The caption and the message a reply quotes travel
with the opening description only when their options are on, after a consent of
their own (core/ai_media/context.py).

Methods run with ``self`` bound to the AIResultDialog.
"""
import wx

from core.ai_media import modes
from core.ai_media.config import MAX_QUESTION_CHARS


class AIMediaModesMixin:
    def _build_mode_controls(self, layout):
        """The choice of mode and the field for the person's own instruction,
        between the result and the buttons. Plain controls, no mnemonics."""
        layout.Add(wx.StaticText(self, label=self.i18n.t("ai_mode_label")), 0, wx.LEFT, 8)
        labels = [self.i18n.t(modes.MODE_KEY[mode]) for mode in modes.MODES]
        self.mode = wx.Choice(self, choices=labels, name=self.i18n.t("ai_mode_label"))
        self.mode.SetSelection(0)
        self.mode.Bind(wx.EVT_CHOICE, self._mode_changed)
        layout.Add(self.mode, 0, wx.EXPAND | wx.ALL, 8)
        layout.Add(wx.StaticText(self, label=self.i18n.t("ai_mode_instruction_label")), 0, wx.LEFT, 8)
        self.mode_instruction = wx.TextCtrl(self, name=self.i18n.t("ai_mode_instruction_label"))
        self.mode_instruction.SetMaxLength(MAX_QUESTION_CHARS - 200)
        layout.Add(self.mode_instruction, 0, wx.EXPAND | wx.ALL, 8)
        self._mode_changed()

    def _mode_changed(self, event=None):
        self.mode_instruction.Enable(modes.MODES[self.mode.GetSelection()] == modes.CUSTOM)
        if event is not None:
            event.Skip()

    def _mode_enter(self, event):
        """Enter in the instruction field runs it (the field is one line)."""
        if (event.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER) and not event.ControlDown()
                and getattr(self, "mode_instruction", None) is not None
                and wx.Window.FindFocus() is self.mode_instruction):
            self._run_mode()
            return True
        return False

    def _run_mode(self, event=None):
        """Run the chosen mode on the media already loaded: the answer replaces
        the first one, and the provider chain and consent are the usual ones."""
        mode = modes.MODES[self.mode.GetSelection()]
        try:
            question = modes.mode_question(mode, self.mode_instruction.GetValue())
        except ValueError:
            self._show_error("ai_error_question")
            self.mode_instruction.SetFocus()
            return
        self._start(question, regenerate=True, task=True)

    def _consent_context(self, providers):
        """Consent to send the caption and/or the quoted message of this
        media, which is more than the picture. Own remembered list; a locked
        chat asks for every window and never remembers (ask_consent)."""
        from ui.dialogs.ai_result_dialog import ask_consent, needs_consent, provider_names
        from core.ai_media import config as ai_config
        if self._context_consented or not needs_consent(
                self.config, providers, "context", self.session.locked):
            self._context_consented = True
            return True
        providers = ai_config.outbound(self.config, providers)  # a local model sends nothing out
        text = self.i18n.t("ai_context_consent").format(providers=provider_names(providers))
        for key in ai_config.notice_keys(providers, self.config):
            text += "\n\n" + self.i18n.t(key)
        if self.session.locked:
            text += "\n\n" + self.i18n.t("ai_locked_consent")
        try:
            accepted = ask_consent(self, self.i18n, self.app, self.config, providers, "context",
                                   self.session.locked, valid=self._valid, text=text,
                                   track=lambda dialog: setattr(self, "_consent_dialog", dialog))
            self._context_consented = accepted
            return accepted
        finally:
            if self.session.closed and self:
                self._finish_close()
