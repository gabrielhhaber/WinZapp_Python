"""Settings > Interface > Messages: the reading profile of a message row
(ui/conversation_panel/row_reading.py) — default, content first, concise."""

import wx

from ui.conversation_panel import row_reading

_PROFILE_KEYS = ("ui_message_reading_profile_default",
                 "ui_message_reading_profile_content_first",
                 "ui_message_reading_profile_concise")


class MessageReadingOptionsMixin:
    def _build_message_reading_options(self, parent, sizer):
        i18n = self.main_window.i18n
        # Options in row_reading.PROFILES order.
        self._reading_profile_radio = wx.RadioBox(
            parent,
            label=i18n.t("ui_message_reading_profile_label"),
            choices=[i18n.t(key) for key in _PROFILE_KEYS],
            majorDimension=1,
            style=wx.RA_SPECIFY_COLS,
        )
        sizer.Add(self._reading_profile_radio, 0, wx.EXPAND | wx.ALL, 8)

    # The key is written out (not row_reading.SETTING) so
    # tests/test_settings_dialog_wiring.py's parse can follow it.

    def _load_message_reading_options(self):
        ui = self.main_window.settings.get("user_interface", {})
        profile = ui.get("message_reading_profile", "default")
        if profile not in row_reading.PROFILES:
            profile = row_reading.DEFAULT
        self._reading_profile_radio.SetSelection(row_reading.PROFILES.index(profile))

    def _save_message_reading_options(self) -> bool:
        """Store the choice; True when it differs from what was saved, so the
        open conversation's rows are written again."""
        index = self._reading_profile_radio.GetSelection()
        profiles = row_reading.PROFILES
        profile = profiles[index] if 0 <= index < len(profiles) else row_reading.DEFAULT
        ui = self.main_window.settings.setdefault("user_interface", {})
        changed = row_reading.reading_profile(self.main_window.settings) != profile
        ui["message_reading_profile"] = profile
        return changed

    def _relabel_message_reading_options(self):
        i18n = self.main_window.i18n
        self._reading_profile_radio.SetLabel(i18n.t("ui_message_reading_profile_label"))
        for index, key in enumerate(_PROFILE_KEYS):
            self._reading_profile_radio.SetItemLabel(index, i18n.t(key))
