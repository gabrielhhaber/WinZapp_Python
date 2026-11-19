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
from ui.dialogs.settings_pages import dialog_owner


def any_age_box_shown(auto_on) -> bool:
    """Whether "however old they are" is on screen: only with the automatic
    transcription on, the only thing it changes."""
    return bool(auto_on)


class TranscriptionOptionsMixin:
    """`keep_model_loaded` and `auto_transcribe_received` for `SettingsDialog`."""

    def _build_transcription_advanced_options(self, page, sizer) -> None:
        """Keep the model loaded and transcribe without the progress window:
        in the tab's Advanced options window (transcription_windows.py)."""
        i18n = self.main_window.i18n
        self._transcription_keep_loaded_check = wx.CheckBox(
            page, label=i18n.t("transcription_keep_loaded_check")
        )
        sizer.Add(self._transcription_keep_loaded_check, 0, wx.ALL, 8)
        self._transcription_no_dialog_check = wx.CheckBox(
            page, label=i18n.t("transcription_no_dialog_check")
        )
        sizer.Add(self._transcription_no_dialog_check, 0, wx.ALL, 8)

    def _build_transcription_options(self, page, sizer) -> None:
        i18n = self.main_window.i18n
        self._transcription_auto_check = wx.CheckBox(
            page, label=i18n.t("transcription_auto_transcribe_check")
        )
        sizer.Add(self._transcription_auto_check, 0, wx.ALL, 8)
        # Automatic transcription with the online providers (Settings >
        # Transcriptions & Descriptions) and this one would write two texts for
        # the same message: turning either on turns the other off, after asking.
        self._transcription_auto_check.Bind(wx.EVT_CHECKBOX, self._on_local_auto_toggle)
        # However old the messages are: only while the box above is ticked
        # (shown and hidden with it, _sync_any_age_box).
        self._transcription_any_age_check = wx.CheckBox(
            page, label=i18n.t("transcription_auto_any_age_check")
        )
        sizer.Add(self._transcription_any_age_check, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self._transcription_options_page = page
        # Only an AI page already built: one built later connects itself.
        ai_page = vars(self).get("_ai_page")
        if ai_page is not None:
            ai_page.confirm_replaces_local = self._confirm_online_replaces_local

    def _build_transcription_status_options(self, page, sizer) -> None:
        """Status updates: the conversation's options, mirrored, in the tab's
        Statuses window (transcription_windows.py)."""
        i18n = self.main_window.i18n
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

    def _ask_replace_automatic(self, key) -> bool:
        i18n = self.main_window.i18n
        return wx.MessageBox(
            i18n.t(key), i18n.t("settings_title"), wx.YES_NO | wx.ICON_QUESTION,
            dialog_owner(self)
        ) == wx.YES

    def _on_local_auto_toggle(self, event) -> None:
        """Turning on the local automatic transcription while the online one is
        on: ask, then turn the online one off (or leave this one off)."""
        if self._transcription_auto_check.GetValue() and self._automatic_transcriptions()[1]:
            if not self._ask_replace_automatic("transcription_auto_replaces_online"):
                self._transcription_auto_check.SetValue(False)
                self._sync_any_age_box()
                return
            # The AI page saves that choice: built now if it never was.
            self._ai_page.auto_transcribe.SetValue(False)
        self._sync_any_age_box()
        event.Skip()

    def _sync_any_age_box(self) -> None:
        """"However old" means something only while the automatic
        transcription is on: shown with it, hidden without it."""
        box = vars(self).get("_transcription_any_age_check")
        if box is None:
            return
        shown = any_age_box_shown(self._transcription_auto_check.GetValue())
        if box.IsShown() != shown:
            box.Show(shown)
            page = vars(self).get("_transcription_options_page")
            if page is not None:
                page.Layout()

    def _confirm_online_replaces_local(self) -> bool:
        """The online automatic transcription is being turned on: True when the
        local one is off, or is turned off now because the user agreed."""
        if not self._transcription_auto_check.GetValue():
            return True
        if not self._ask_replace_automatic("transcription_online_auto_replaces_local"):
            return False
        self._transcription_auto_check.SetValue(False)
        self._sync_any_age_box()
        return True

    def _automatic_transcriptions(self) -> tuple:
        """(local on, online on): the box of each page that was built, and
        for a page never opened what is stored, which it could not change.
        Reading a box of a page not built would build it in the middle of OK."""
        built = vars(self)
        local_box = built.get("_transcription_auto_check")
        if local_box is not None:
            local = local_box.GetValue()
        else:
            local = bool(self.main_window.settings.get("transcription", {})
                         .get("auto_transcribe_received", False))
        ai_page = built.get("_ai_page")
        if ai_page is not None:
            online = ai_page.auto_transcribe.GetValue()
        else:
            from app_paths import global_dir
            from app_settings import AppSettings
            from core.ai_media.config import preferences
            app = getattr(self.main_window, "_app_settings", None) or AppSettings(global_dir())
            online = bool(preferences(app).get("auto_transcribe"))
        return local, online

    def _validate_automatic_transcription(self) -> bool:
        """Refuse to save both automatic transcriptions on: they would write
        two texts for the same message, and a conversation's data window
        offers one of them (or none) to follow. Turning either on asks to turn
        the other off, but a settings.json from before that question can hold
        both."""
        local, online = self._automatic_transcriptions()
        if not (local and online):
            return True
        i18n = self.main_window.i18n
        wx.MessageBox(
            i18n.t("transcription_auto_both_on"),
            i18n.t("error").format(app_name=self.main_window.app_name),
            wx.OK | wx.ICON_ERROR, self)
        self._show_page(self._transcription_page, self._transcription_auto_check)
        return False

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
        self._transcription_any_age_check.SetValue(
            bool(section.get("auto_any_age", False))
        )
        self._sync_any_age_box()
        self._transcription_no_dialog_check.SetValue(
            bool(section.get("run_without_dialog", False))
        )

    def _apply_transcription_options(self) -> None:
        section = self.main_window.settings.setdefault("transcription", {})
        section["keep_model_loaded"] = self._transcription_keep_loaded_check.GetValue()
        section["auto_transcribe_received"] = self._transcription_auto_check.GetValue()
        section["auto_any_age"] = self._transcription_any_age_check.GetValue()
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
        # A paused automatic queue tries again: this tab is where what paused
        # it is fixed (auto_transcription.transcription_settings_applied).
        applied = getattr(self.main_window, "transcription_settings_applied", None)
        if callable(applied):
            applied()

    def _refresh_transcription_options_labels(self) -> None:
        i18n = self.main_window.i18n
        self._transcription_keep_loaded_check.SetLabel(
            i18n.t("transcription_keep_loaded_check")
        )
        self._transcription_auto_check.SetLabel(
            i18n.t("transcription_auto_transcribe_check")
        )
        self._transcription_any_age_check.SetLabel(
            i18n.t("transcription_auto_any_age_check")
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
