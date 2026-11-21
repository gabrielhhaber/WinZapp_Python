"""Native language selection for call translation and its saved defaults."""
import wx

from core.live_translation_languages import LANGUAGES


class LiveTranslationDialog(wx.Dialog):
    def __init__(self, parent, i18n, languages, *, defaults=False):
        title = i18n.t("call_translation_options").replace("&", "")
        super().__init__(parent, title=title)
        self.i18n = i18n
        self.result = None
        self.defaults = defaults
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.codes = tuple(sorted(LANGUAGES, key=lambda code: LANGUAGES[code].casefold()))
        items = [LANGUAGES[code] for code in self.codes]

        self.mode = None
        if not defaults:
            notice = wx.StaticText(self, label=i18n.t("call_translation_consent"))
            notice.Wrap(self.FromDIP(470))
            sizer.Add(notice, 0, wx.EXPAND | wx.ALL, 10)
            self.mode = self._field(sizer, i18n.t("call_translation_mode"), [
                i18n.t("call_translation_listen"), i18n.t("call_translation_both")])
            self.mode.SetSelection(0)
            self.mode.Bind(wx.EVT_CHOICE, self._mode_changed)

        self.mine = self._field(sizer, i18n.t("call_translation_my_language"), items)
        self.mine.SetSelection(self.codes.index(languages[0]))
        self.peer = self._field(sizer, i18n.t("call_translation_peer_language"),
                                [i18n.t("call_translation_ask_language"), *items])
        self.peer.SetSelection(self.codes.index(languages[1]) + 1 if languages[1] else 0)
        self.peer.Enable(defaults)
        self.duck = wx.CheckBox(self, label=i18n.t("call_translation_duck"))
        self.duck.SetValue(languages[2])
        sizer.Add(self.duck, 0, wx.ALL, 10)
        buttons = wx.StdDialogButtonSizer()
        okay = wx.Button(self, id=wx.ID_OK, label=i18n.t("ok"))
        cancel = wx.Button(self, id=wx.ID_CANCEL, label=i18n.t("cancel"))
        buttons.AddButton(okay)
        buttons.AddButton(cancel)
        buttons.Realize()
        okay.SetDefault()
        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 10)
        self.SetSizerAndFit(sizer)
        self.Bind(wx.EVT_BUTTON, self._accept, id=wx.ID_OK)
        (self.mode or self.mine).SetFocus()

    def _field(self, sizer, label, items):
        sizer.Add(wx.StaticText(self, label=label), 0, wx.LEFT | wx.TOP, 10)
        control = wx.Choice(self, choices=items, name=label.replace("&", ""))
        sizer.Add(control, 0, wx.EXPAND | wx.ALL, 10)
        return control

    def _mode_changed(self, event):
        self.peer.Enable(self.mode.GetSelection() == 1)

    def _accept(self, event):
        peer_index = self.peer.GetSelection()
        both = not self.defaults and self.mode.GetSelection() == 1
        if both and peer_index <= 0:
            wx.MessageBox(self.i18n.t("call_translation_choose_peer"),
                          self.GetTitle(), wx.OK | wx.ICON_INFORMATION, self)
            self.peer.SetFocus()
            return
        peer = self.codes[peer_index - 1] if peer_index > 0 and (both or self.defaults) else ""
        self.result = self.codes[self.mine.GetSelection()], peer, self.duck.GetValue()
        self.EndModal(wx.ID_OK)
