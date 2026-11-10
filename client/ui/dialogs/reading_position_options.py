"""Settings > Conversations: where the messages list lands when a conversation
opens (core/reading_position.py), next to "focus_on_open", and bookmarks that
remove themselves after the first use (core/bookmark_settings.py)."""

import wx

from core import reading_position

_POSITION_KEYS = ("ui_message_list_open_position_unread_or_last",
                  "ui_message_list_open_position_last_focused")


class ReadingPositionOptionsMixin:
    def _build_open_position_options(self, parent, sizer):
        i18n = self.main_window.i18n
        # Options in reading_position.MODES order.
        self._open_position_radio = wx.RadioBox(
            parent,
            label=i18n.t("ui_message_list_open_position_label"),
            choices=[i18n.t(key) for key in _POSITION_KEYS],
            majorDimension=1,
            style=wx.RA_SPECIFY_COLS,
        )
        sizer.Add(self._open_position_radio, 0, wx.EXPAND | wx.ALL, 8)

    def _build_bookmark_removal_options(self, parent, sizer):
        i18n = self.main_window.i18n
        self._remove_bookmarks_after_use_cb = wx.CheckBox(
            parent, label=i18n.t("ui_remove_bookmarks_after_use"))
        sizer.Add(self._remove_bookmarks_after_use_cb, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self._remove_temp_bookmarks_after_use_cb = wx.CheckBox(
            parent, label=i18n.t("ui_remove_temp_bookmarks_after_use"))
        sizer.Add(self._remove_temp_bookmarks_after_use_cb, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

    # The keys are written out (not reading_position.SETTING etc.) so
    # tests/test_settings_dialog_wiring.py's parse can follow them; the
    # modules' tests pin the same names.

    def _load_open_position_options(self):
        ui = self.main_window.settings.get("user_interface", {})
        mode = ui.get("message_list_open_position", "unread_or_last")
        if mode not in reading_position.MODES:
            mode = reading_position.DEFAULT
        self._open_position_radio.SetSelection(reading_position.MODES.index(mode))

    def _load_bookmark_removal_options(self):
        ui = self.main_window.settings.get("user_interface", {})
        self._remove_bookmarks_after_use_cb.SetValue(
            bool(ui.get("remove_bookmarks_after_use", False)))
        self._remove_temp_bookmarks_after_use_cb.SetValue(
            bool(ui.get("remove_temp_bookmarks_after_use", False)))

    def _save_open_position_options(self):
        index = self._open_position_radio.GetSelection()
        modes = reading_position.MODES
        mode = modes[index] if 0 <= index < len(modes) else reading_position.DEFAULT
        self.main_window.settings.setdefault("user_interface", {})[
            "message_list_open_position"] = mode

    def _save_bookmark_removal_options(self):
        ui = self.main_window.settings.setdefault("user_interface", {})
        ui["remove_bookmarks_after_use"] = self._remove_bookmarks_after_use_cb.GetValue()
        ui["remove_temp_bookmarks_after_use"] = (
            self._remove_temp_bookmarks_after_use_cb.GetValue())

    def _relabel_open_position_options(self):
        i18n = self.main_window.i18n
        self._open_position_radio.SetLabel(i18n.t("ui_message_list_open_position_label"))
        for index, key in enumerate(_POSITION_KEYS):
            self._open_position_radio.SetItemLabel(index, i18n.t(key))

    def _relabel_bookmark_removal_options(self):
        i18n = self.main_window.i18n
        self._remove_bookmarks_after_use_cb.SetLabel(i18n.t("ui_remove_bookmarks_after_use"))
        self._remove_temp_bookmarks_after_use_cb.SetLabel(
            i18n.t("ui_remove_temp_bookmarks_after_use"))
