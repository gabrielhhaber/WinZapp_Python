"""SummaryVoiceChoiceMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Before a summary transcribes anything, the person says how its voice messages
are handled: local Whisper, one online provider and model (apart from the
summary's own), or not at all (core/ai_media/summary_voice_choice.py). Online,
a second question names how many voice messages go to which provider and that
it may use quota or cost money; its box remembers the consent per provider.
Nothing is sent, transcribed or logged by opening, changing or closing these
windows.
"""
import wx

from app_paths import global_dir
from core.ai_credentials import CredentialStore
from core.ai_media import summary_voice_choice as svc


class SummaryVoiceChoiceMixin:
    """The voice step's questions, for ConversationsPanel."""

    def _summary_voice_providers(self, config):
        return svc.online_providers(config, CredentialStore(global_dir()).saved())

    def _summary_voice_saved(self, app):
        try:
            return svc.saved_choice(app.get("ai_media"))
        except Exception:
            return svc.saved_choice(None)

    def _summary_voice_remember(self, app, choice):
        try:
            app.update("ai_media", lambda old: svc.remember(old, choice))
        except (OSError, RuntimeError):
            pass  # asked again next time, starting from the default

    def _summary_voice_ask(self, count, providers, initial, jid):
        """The dialog; the VoiceChoice, or None when the person cancelled."""
        from ui.dialogs.summary_voice_choice import SummaryVoiceChoiceDialog
        mw = self.main_window
        dialog = SummaryVoiceChoiceDialog(self, mw.i18n, count, providers, initial)
        self._summary_consent_dialog = dialog  # the vault locking dismisses it
        self._summary_consent_jid = jid
        try:
            answered = dialog.ShowModal() == wx.ID_OK
            return dialog.choice if answered else None
        finally:
            self._summary_consent_dialog = None
            dialog.Destroy()

    def _summary_voice_decide(self, app, config, jid, count):
        """How this summary's ``count`` voice messages are handled: a
        VoiceChoice, or None when the person cancelled (or the chat was hidden
        or the app closed meanwhile)."""
        mw = self.main_window
        providers = self._summary_voice_providers(config)
        initial = svc.usable(self._summary_voice_saved(app), providers)
        choice = self._summary_voice_ask(count, providers, initial, jid)
        if choice is None or getattr(mw, "_shutting_down", False) or mw.is_chat_hidden_by_vault(jid):
            return None
        self._summary_voice_remember(app, choice)
        return choice

    def _summary_voice_online_consent(self, app, config, jid, choice, count) -> bool:
        """Ask to send ``count`` voice messages to the chosen provider (once
        and remembered for a common chat; every time for a locked one)."""
        from ui.dialogs.ai_result_dialog import ask_consent
        mw = self.main_window
        locked = mw.is_chat_locked(jid)

        def track(dialog):
            self._summary_consent_dialog = dialog
            self._summary_consent_jid = jid
        return ask_consent(
            self, mw.i18n, app, config, [choice.provider], "audio", locked,
            valid=lambda: not getattr(mw, "_shutting_down", False) and not mw.is_chat_hidden_by_vault(jid),
            track=track, text=svc.consent_text(mw.i18n, choice.provider, count, locked))
