"""The on/off options at the end of the Local Transcription tab's language
block: keep the model loaded in memory, transcribe received voice messages on
their own, and the same choices for status updates (transcribe on arrival,
keep the result, Enter and Space transcribe) with the button that clears the
saved status transcriptions.

Kept out of transcription_tab.py, which is far past the size a mixin should
have. Each box mirrors one key of settings.json's "transcription" section, and
is written the way tests/test_settings_checkboxes_wiring.py can follow (a
literal section and key on both the load and the save side).
"""

import wx

from core.transcription import residency
from core.transcription import stored as stored_transcription


class TranscriptionOptionsMixin:
    """`keep_model_loaded` and `auto_transcribe_received` for `SettingsDialog`."""

    def _build_transcription_options(self, page, sizer) -> None:
        i18n = self.main_window.i18n
        self._transcription_keep_loaded_check = wx.CheckBox(
            page, label=i18n.t("transcription_keep_loaded_check")
        )
        sizer.Add(self._transcription_keep_loaded_check, 0, wx.ALL, 8)
        self._transcription_no_dialog_check = wx.CheckBox(
            page, label=i18n.t("transcription_no_dialog_check")
        )
        sizer.Add(self._transcription_no_dialog_check, 0, wx.ALL, 8)
        self._transcription_auto_check = wx.CheckBox(
            page, label=i18n.t("transcription_auto_transcribe_check")
        )
        sizer.Add(self._transcription_auto_check, 0, wx.ALL, 8)
        # Status updates: the conversation's options, mirrored. No Alt keys,
        # like the cache controls: the tab has no free letter left.
        self._transcription_status_auto_check = wx.CheckBox(
            page, label=i18n.t("transcription_status_auto_check")
        )
        sizer.Add(self._transcription_status_auto_check, 0, wx.ALL, 8)
        self._transcription_status_enter_check = wx.CheckBox(
            page, label=i18n.t("transcription_status_enter_check")
        )
        sizer.Add(self._transcription_status_enter_check, 0, wx.ALL, 8)
        self._transcription_status_cache_check = wx.CheckBox(
            page, label=i18n.t("transcription_status_cache_check")
        )
        sizer.Add(self._transcription_status_cache_check, 0, wx.ALL, 8)
        self._transcription_status_clear_btn = wx.Button(
            page, label=i18n.t("transcription_status_clear_btn")
        )
        self._transcription_status_clear_btn.Bind(
            wx.EVT_BUTTON, self._on_transcription_clear_status_cache
        )
        sizer.Add(self._transcription_status_clear_btn, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

    def _on_transcription_clear_status_cache(self, event) -> None:
        """Delete the saved transcription of every status, after asking."""
        if getattr(self, "_transcription_status_clearing", False):
            return
        i18n = self.main_window.i18n
        if wx.MessageBox(
                i18n.t("transcription_status_clear_confirm"), i18n.t("settings_title"),
                wx.YES_NO | wx.ICON_QUESTION, self
        ) != wx.YES:
            return
        # One clear at a time, and not by disabling the button: it holds the
        # focus, and a disabled control says nothing to a screen reader.
        self._transcription_status_clearing = True

        def _done(count):
            if self:
                self._transcription_status_clearing = False
            key = ("transcription_clear_all_cache_none" if count == 0
                   else stored_transcription.clear_cache_message_key(count))
            self.main_window.speak_output.output(i18n.t(key).format(count=count or 0),
                                                 interrupt=True)

        self.main_window.clear_status_transcriptions(_done)

    def _load_transcription_options(self) -> None:
        section = self.main_window.settings.get("transcription", {})
        self._transcription_status_auto_check.SetValue(
            bool(section.get("auto_transcribe_statuses", False))
        )
        self._transcription_status_cache_check.SetValue(
            bool(section.get("status_cache_enabled", True))
        )
        self._transcription_status_enter_check.SetValue(bool(
            self.main_window.settings.get("user_interface", {}).get(
                "enter_transcribes_statuses", False
            )
        ))
        self._transcription_keep_loaded_check.SetValue(
            bool(section.get("keep_model_loaded", False))
        )
        self._transcription_auto_check.SetValue(
            bool(section.get("auto_transcribe_received", False))
        )
        self._transcription_no_dialog_check.SetValue(
            section.get("run_without_dialog") is True
        )

    def _apply_transcription_options(self) -> None:
        section = self.main_window.settings.setdefault("transcription", {})
        section["keep_model_loaded"] = self._transcription_keep_loaded_check.GetValue()
        section["auto_transcribe_received"] = self._transcription_auto_check.GetValue()
        section["run_without_dialog"] = self._transcription_no_dialog_check.GetValue()
        section["auto_transcribe_statuses"] = self._transcription_status_auto_check.GetValue()
        section["status_cache_enabled"] = self._transcription_status_cache_check.GetValue()
        self.main_window.settings.setdefault("user_interface", {})[
            "enter_transcribes_statuses"
        ] = self._transcription_status_enter_check.GetValue()
        # Takes effect now, not at the next launch: turning it on cancels the
        # pending release, turning it off starts the idle clock.
        residency.residency.configure(residency.keep_loaded_from_settings(
            self.main_window.settings
        ))

    def _refresh_transcription_options_labels(self) -> None:
        i18n = self.main_window.i18n
        self._transcription_keep_loaded_check.SetLabel(
            i18n.t("transcription_keep_loaded_check")
        )
        self._transcription_auto_check.SetLabel(
            i18n.t("transcription_auto_transcribe_check")
        )
        self._transcription_no_dialog_check.SetLabel(
            i18n.t("transcription_no_dialog_check")
        )
        self._transcription_status_auto_check.SetLabel(
            i18n.t("transcription_status_auto_check")
        )
        self._transcription_status_enter_check.SetLabel(
            i18n.t("transcription_status_enter_check")
        )
        self._transcription_status_cache_check.SetLabel(
            i18n.t("transcription_status_cache_check")
        )
        self._transcription_status_clear_btn.SetLabel(
            i18n.t("transcription_status_clear_btn")
        )
