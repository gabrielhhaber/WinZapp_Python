"""Settings > Interface: the font size for low vision (core/font_scale.py).

A plain label and drop-down at the top of the Interface page. Default is 100 %,
which leaves every control as it always was; Apply gives the open windows the
new size at once (main_window/font_scale.py)."""

import wx

from core import font_scale


def font_scale_choice_labels(i18n):
    """The drop-down's rows, in font_scale.SCALES order."""
    return [i18n.t("ui_font_scale_default") if percent == font_scale.DEFAULT
            else f"{percent}%" for percent in font_scale.SCALES]


class FontScaleOptionsMixin:
    def _build_font_scale_options(self, parent, sizer):
        i18n = self.main_window.i18n
        self._font_scale_label = wx.StaticText(parent, label=i18n.t("ui_font_scale_label"))
        sizer.Add(self._font_scale_label, 0, wx.LEFT | wx.TOP | wx.RIGHT, 8)
        self._font_scale_choice = wx.Choice(parent, choices=font_scale_choice_labels(i18n))
        sizer.Add(self._font_scale_choice, 0, wx.ALL, 8)

    # The key is written out (not font_scale.SETTING) so
    # tests/test_settings_dialog_wiring.py's parse can follow it.

    def _load_font_scale_options(self):
        ui = self.main_window.settings.get("user_interface", {})
        percent = font_scale.normalize(ui.get("font_scale_percent", 100))
        self._font_scale_choice.SetSelection(font_scale.SCALES.index(percent))

    def _save_font_scale_options(self):
        index = self._font_scale_choice.GetSelection()
        scales = font_scale.SCALES
        percent = scales[index] if 0 <= index < len(scales) else font_scale.DEFAULT
        self.main_window.settings.setdefault("user_interface", {})["font_scale_percent"] = percent

    def _relabel_font_scale_options(self):
        i18n = self.main_window.i18n
        self._font_scale_label.SetLabel(i18n.t("ui_font_scale_label"))
        # SetString re-inserts the row on Windows and can drop the selection.
        selected = self._font_scale_choice.GetSelection()
        for index, text in enumerate(font_scale_choice_labels(i18n)):
            self._font_scale_choice.SetString(index, text)
        self._font_scale_choice.SetSelection(selected)

    def _apply_font_scale_live(self):
        """The saved size on every open window, the Settings window included."""
        apply = getattr(self.main_window, "apply_font_scale", None)
        if callable(apply):
            apply()
