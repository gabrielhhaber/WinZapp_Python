"""Additional settings of the update dialog (issue #842): which bundled sounds
the installation keeps — all of WinZapp's, or only WhatsApp's own.

Only the choice is made here. The updater applies it when it installs (core/
sound_selection.stage_selection), so cancelling, or never installing, changes
nothing. Plain radio buttons and a read-only line stating the current choice,
so a screen reader reads both when the dialog opens.
"""
import wx

from core.sound_selection import MODE_ALL, MODE_WHATSAPP


def mode_label_key(mode: str) -> str:
    """The catalog key naming *mode*."""
    return "sound_selection_whatsapp" if mode == MODE_WHATSAPP else "sound_selection_all"


class UpdateSoundSettingsDialog(wx.Dialog):
    """Radio choice between the two sound sets; OK keeps it, Cancel drops it."""

    def __init__(self, parent, i18n, installed_mode: str, chosen_mode: str):
        super().__init__(parent, title=i18n.t("update_additional_settings_title"),
                         style=wx.DEFAULT_DIALOG_STYLE)
        sizer = wx.BoxSizer(wx.VERTICAL)
        current = i18n.t("sound_selection_current").format(
            selection=i18n.t(mode_label_key(installed_mode)))
        info = wx.StaticText(self, label=current)
        info.Wrap(420)
        sizer.Add(info, 0, wx.ALL, 12)
        self._all = wx.RadioButton(self, label=i18n.t("sound_selection_all"), style=wx.RB_GROUP)
        self._whatsapp = wx.RadioButton(self, label=i18n.t("sound_selection_whatsapp"))
        sizer.Add(self._all, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        sizer.Add(self._whatsapp, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        (self._whatsapp if chosen_mode == MODE_WHATSAPP else self._all).SetValue(True)
        note = wx.StaticText(self, label=i18n.t("sound_selection_note"))
        note.Wrap(420)
        sizer.Add(note, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        buttons = wx.StdDialogButtonSizer()
        ok = wx.Button(self, wx.ID_OK, label=i18n.t("ok"))
        cancel = wx.Button(self, wx.ID_CANCEL, label=i18n.t("cancel"))
        buttons.AddButton(ok)
        buttons.AddButton(cancel)
        buttons.Realize()
        sizer.Add(buttons, 0, wx.ALIGN_CENTER | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        ok.SetDefault()
        self.SetAffirmativeId(wx.ID_OK)
        self.SetEscapeId(wx.ID_CANCEL)
        self.SetSizerAndFit(sizer)
        self.CentreOnParent()
        (self._whatsapp if chosen_mode == MODE_WHATSAPP else self._all).SetFocus()

    def chosen_mode(self) -> str:
        return MODE_WHATSAPP if self._whatsapp.GetValue() else MODE_ALL
