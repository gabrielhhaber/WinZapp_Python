"""Asks how the voice messages of a summary are transcribed.

Shown before a summary that has voice messages without a saved transcription:
locally, online with a provider and model chosen apart from the summary's, or
not at all. Opening it, moving through it or closing it sends nothing; the
online consent is a separate question asked afterwards.
"""
import wx

from core.ai_media import summary_voice_choice as svc
from core.ai_media.config import PROVIDERS


class SummaryVoiceChoiceDialog(wx.Dialog):
    """The three ways, the online provider and its model. ``choice`` holds the
    answer after ShowModal() returned wx.ID_OK."""

    def __init__(self, parent, i18n, count, providers, initial):
        super().__init__(parent, title=i18n.t("ai_summary_voice_choice_title"))
        self._providers = list(providers)
        self._models = ()
        self._modes = [svc.MODE_LOCAL] + ([svc.MODE_ONLINE] if providers else []) + [svc.MODE_NONE]
        self.choice = svc.VoiceChoice(svc.MODE_LOCAL)
        layout = wx.BoxSizer(wx.VERTICAL)
        intro = wx.TextCtrl(self, value=i18n.t("ai_summary_voice_choice_intro").format(count=count),
                            style=wx.TE_MULTILINE | wx.TE_READONLY, size=(480, 70),
                            name=i18n.t("ai_summary_voice_choice_title"))
        layout.Add(intro, 0, wx.EXPAND | wx.ALL, 10)
        names = {svc.MODE_LOCAL: i18n.t("ai_summary_voice_mode_local"),
                 svc.MODE_ONLINE: i18n.t("ai_summary_voice_mode_online"),
                 svc.MODE_NONE: i18n.t("ai_summary_voice_mode_none")}
        labels = [names[mode] for mode in self._modes]
        self.mode = wx.RadioBox(self, label=i18n.t("ai_summary_voice_mode"), choices=labels,
                                majorDimension=1, style=wx.RA_SPECIFY_COLS)
        layout.Add(self.mode, 0, wx.EXPAND | wx.ALL, 10)
        self.provider_label = wx.StaticText(self, label=i18n.t("ai_summary_voice_provider"))
        layout.Add(self.provider_label, 0, wx.LEFT | wx.TOP, 10)
        self.provider = wx.Choice(self, choices=[PROVIDERS[p].name for p in self._providers],
                                  name=i18n.t("ai_summary_voice_provider"))
        layout.Add(self.provider, 0, wx.EXPAND | wx.ALL, 10)
        self.model_label = wx.StaticText(self, label=i18n.t("ai_summary_voice_model"))
        layout.Add(self.model_label, 0, wx.LEFT | wx.TOP, 10)
        self.model = wx.Choice(self, name=i18n.t("ai_summary_voice_model"))
        layout.Add(self.model, 0, wx.EXPAND | wx.ALL, 10)
        buttons = wx.WrapSizer(wx.HORIZONTAL)
        buttons.Add(wx.Button(self, wx.ID_OK, label=i18n.t("ai_summary_voice_continue")), 0, wx.ALL, 8)
        cancel = wx.Button(self, wx.ID_CANCEL, label=i18n.t("cancel"))
        buttons.Add(cancel, 0, wx.ALL, 8)
        layout.Add(buttons)
        self.SetSizerAndFit(layout)
        self._select(initial)
        self.mode.Bind(wx.EVT_RADIOBOX, lambda event: self._refresh())
        self.provider.Bind(wx.EVT_CHOICE, lambda event: self._load_models())
        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)
        cancel.SetDefault()
        self.mode.SetFocus()

    def _select(self, initial):
        initial = svc.usable(initial, self._providers)
        self.mode.SetSelection(self._modes.index(initial.mode))
        if self._providers:
            self.provider.SetSelection(self._providers.index(initial.provider)
                                       if initial.provider in self._providers else 0)
            self._load_models(initial.model)
        self._refresh()

    def _load_models(self, wanted=""):
        provider = self._providers[self.provider.GetSelection()] if self._providers else ""
        self._models = svc.model_choices(provider)
        self.model.SetItems([name for _id, name in self._models])
        ids = [model_id for model_id, _name in self._models]
        if ids:
            self.model.SetSelection(ids.index(wanted) if wanted in ids else 0)
        self._refresh()

    def _refresh(self):
        online = self._modes[self.mode.GetSelection()] == svc.MODE_ONLINE
        self.provider.Enable(online)
        self.provider_label.Enable(online)
        has_models = online and bool(self._models)
        self.model.Enable(has_models)
        self.model_label.Enable(has_models)

    def _on_ok(self, event):
        mode = self._modes[self.mode.GetSelection()]
        if mode != svc.MODE_ONLINE:
            self.choice = svc.VoiceChoice(mode)
        else:
            provider = self._providers[self.provider.GetSelection()]
            model = (self._models[self.model.GetSelection()][0]
                     if self._models and self.model.GetSelection() >= 0 else "")
            self.choice = svc.VoiceChoice(mode, provider, model)
        self.EndModal(wx.ID_OK)
