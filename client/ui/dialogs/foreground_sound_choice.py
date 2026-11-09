"""The two "sound while WinZapp is in front" combo boxes of Settings > Sounds.

One for the conversation that is open, one for every other conversation. Each
chooses between the dedicated foreground sound and the background notification
sound. The dialog's EVT_COMBOBOX handler already marks the page dirty.
"""
import wx

from core.foreground_sound import (
    CHOICE_DEDICATED, CHOICE_NOTIFICATION, SCOPE_ACTIVE, SCOPE_OTHER,
    SETTINGS_KEY, foreground_sound_choice,
)

_CHOICES = (CHOICE_DEDICATED, CHOICE_NOTIFICATION)
_ROWS = (
    (SCOPE_ACTIVE, "foreground_sound_active_label"),
    (SCOPE_OTHER, "foreground_sound_other_label"),
)


class ForegroundSoundChoice:
    def __init__(self, dialog, page, sizer):
        self._i18n = dialog.main_window.i18n
        self._labels = {}
        self._combos = {}
        for scope, label_key in _ROWS:
            label = wx.StaticText(page, label=self._i18n.t(label_key))
            combo = wx.ComboBox(page, style=wx.CB_READONLY, choices=self._choice_labels())
            sizer.Add(label, 0, wx.LEFT | wx.TOP | wx.RIGHT, 8)
            sizer.Add(combo, 0, wx.EXPAND | wx.ALL, 8)
            self._labels[scope] = label
            self._combos[scope] = combo

    def _choice_labels(self):
        return [self._i18n.t("foreground_sound_dedicated"),
                self._i18n.t("foreground_sound_notification")]

    def load(self, settings: dict):
        for scope, combo in self._combos.items():
            combo.SetSelection(_CHOICES.index(foreground_sound_choice(settings, scope)))

    def apply(self, settings: dict):
        chosen = {}
        for scope, combo in self._combos.items():
            index = combo.GetSelection()
            chosen[scope] = _CHOICES[index] if 0 <= index < len(_CHOICES) else CHOICE_DEDICATED
        settings[SETTINGS_KEY] = chosen

    def relabel(self):
        for scope, label_key in _ROWS:
            self._labels[scope].SetLabel(self._i18n.t(label_key))
            combo = self._combos[scope]
            selection = combo.GetSelection()
            combo.Set(self._choice_labels())
            combo.SetSelection(selection if selection != wx.NOT_FOUND else 0)
