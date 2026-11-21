"""The per-call consent and routing controls for Gemini Live Translate."""
import logging

import wx

from app_paths import global_dir
from app_settings import AppSettings
from core.ai_credentials import CredentialError, CredentialStore
from core.live_translation_languages import language_defaults
from ui.dialogs.live_translation import LiveTranslationDialog


def stop_call_translation(window):
    audio = getattr(window, "_call_audio_session", None)
    stop = getattr(audio, "stop_live_translation", None)
    return bool(stop()) if stop is not None else False


def _is_group_call(window, active):
    if not active:
        return False
    if active.get("is_group") or active.get("translation_group"):
        return True
    others = getattr(window, "_call_rosters", {}).get(active.get("call_id"), ())
    if len(others) < 2:
        return False
    same_peer = getattr(window, "_chat_jids_equivalent", lambda a, b: a == b)
    if any(not same_peer(others[0], other) for other in others[1:]):
        active["translation_group"] = True
        return True
    return False


class CallTranslationMixin:
    def _make_live_translation_button(self, panel):
        button = wx.Button(panel, label=self.i18n.t("call_translation_start"))
        button.Bind(wx.EVT_BUTTON, self._toggle_live_translation)
        button.Hide()
        return button

    def _sync_live_translation_button(self, active):
        button = self.voice_call_window_translation_button
        audio = getattr(self, "_call_audio_session", None)
        translating = bool(audio and audio.live_translation)
        group = _is_group_call(self, active)
        if translating and group:
            audio.stop_live_translation()
            self.output(self.i18n.t("call_translation_stopped"))
            translating = False
        visible = bool(active and not group and audio and audio.running
                       and (translating or self._voice_call_last_announced_state == "ACTIVE"))
        if not visible and button.IsShown() and button.HasFocus() and active:
            self.voice_call_window_mute_button.SetFocus()
        button.Show(visible)
        button.SetLabel(self.i18n.t("call_translation_stop") if translating
                        else self.i18n.t("call_translation_start"))

    def _live_translation_languages(self):
        app = getattr(self, "_app_settings", None) or AppSettings(global_dir())
        return app, language_defaults(app.get("call_translation"), self.i18n.language)

    def open_live_translation_settings(self, *, parent):
        app, languages = self._live_translation_languages()
        with LiveTranslationDialog(parent, self.i18n, languages, defaults=True) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            mine, peer, duck = dialog.result
        try:
            app.set("call_translation", {"mine": mine, "peer": peer, "duck": duck})
        except (OSError, ValueError, RuntimeError):
            self.output(self.i18n.t("call_translation_save_failed"), interrupt=True)

    def _toggle_live_translation(self, event):
        audio, call = self._call_audio_session, self._active_voice_call
        if not audio or not call:
            return
        if audio.stop_live_translation():
            self._sync_voice_call_bar()
            self.output(self.i18n.t("call_translation_stopped"))
            return
        if _is_group_call(self, call) or self._voice_call_last_announced_state != "ACTIVE":
            return
        try:
            key = CredentialStore(global_dir()).get("gemini")
        except CredentialError:
            self.output(self.i18n.t("ai_error_credentials"), interrupt=True)
            return
        if not key:
            self.output(self.i18n.t("call_translation_key_needed"), interrupt=True)
            return
        _, languages = self._live_translation_languages()
        with LiveTranslationDialog(self.voice_call_window, self.i18n, languages) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            mine, peer, duck = dialog.result
        if (self._active_voice_call is not call or self._call_audio_session is not audio
                or _is_group_call(self, call) or self._voice_call_last_announced_state != "ACTIVE"):
            return
        self._live_translation_states = {}
        self._live_translation_status = "connecting"
        try:
            started = audio.start_live_translation(
                key, mine, peer,
                lambda worker, state: wx.CallAfter(self._on_live_translation_state,
                                                  audio, call, worker, state),
                duck=duck)
        except RuntimeError:
            logging.warning("[call translation] could not start audio workers")
            self.output(self.i18n.t("call_translation_failed"), interrupt=True)
            return
        if started:
            self._sync_voice_call_bar()
            self.output(self.i18n.t("call_translation_connecting"))

    def _on_live_translation_state(self, audio, call, worker, state):
        if (self._call_audio_session is not audio or self._active_voice_call is not call
                or worker not in audio.live_translation):
            return
        if state == "failed":
            audio.stop_live_translation()
            self._sync_voice_call_bar()
            self.output(self.i18n.t("call_translation_failed"), interrupt=True)
            return
        self._live_translation_states[worker] = state
        if state == "reconnecting":
            status, text = "reconnecting", self.i18n.t("call_translation_reconnecting")
        elif state == "ready" and all(
                self._live_translation_states.get(direction) == "ready"
                or (index == 1 and audio.microphone_muted)
                for index, direction in enumerate(audio.live_translation)):
            status, text = "ready", self.i18n.t("call_translation_ready")
        else:
            return
        if status != self._live_translation_status:
            self._live_translation_status = status
            self.output(text)
