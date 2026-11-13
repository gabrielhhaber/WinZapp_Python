"""Settings and model discovery for translation of text messages."""
from dataclasses import replace

import wx
from wx.lib.scrolledpanel import ScrolledPanel

from core.ai_credentials import CredentialError
from core.ai_media.errors import DescriptionError
from core.ai_media.service import RequestToken, submit
from core.i18n import LANGUAGE_NAMES
from core.message_translation import (
    AGE_CHOICES_MINUTES, CUSTOM, DISPLAY_MODES, PROVIDER_NAMES, SCOPE_ALL, SCOPE_SELECTED,
    TranslationSettings, credential_id, default_model, list_models, model_test_identity,
    normalized_base, translate, translation_model_ready, translation_request,
)


LABELS = PROVIDER_NAMES | dict(zip(CUSTOM, ("OpenAI Compatible", "Anthropic Compatible")))
SAMPLE = "Hello! See you tomorrow."


class TranslationSettingsDialog(wx.Dialog):
    def __init__(self, parent, i18n, config, get_key, title):
        super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._body = ScrolledPanel(self)
        self._t, self._get_key = i18n.t, get_key
        self._initial = config
        self._tested_model = model_test_identity(config) if config.model_tested else None
        self._token = None
        self._alive = True
        self._sizer = wx.BoxSizer(wx.VERTICAL)
        self.provider = self._field("translation_provider", wx.Choice, choices=list(LABELS.values()))
        self.provider.SetSelection(list(LABELS).index(config.provider) if config.provider in LABELS else 0)
        self.url = self._field("translation_api_url", wx.TextCtrl, value=config.base_url)
        self.key = self._field("translation_key", wx.TextCtrl, style=wx.TE_PASSWORD)
        self.model = self._field("translation_model", wx.ComboBox, value=config.model or default_model(self._provider()), style=wx.CB_DROPDOWN)
        self.language = self._field("translation_language", wx.ComboBox, value=config.language,
                                    choices=list(LANGUAGE_NAMES.values()), style=wx.CB_DROPDOWN)
        self.display = self._field("translation_display", wx.Choice, choices=[
            self._t("translation_display_replace"), self._t("translation_display_append"),
            self._t("translation_display_separate"),
        ])
        self.display.SetSelection(DISPLAY_MODES.index(config.display))
        self.automatic = wx.CheckBox(self._body, label=self._t("translation_automatic"))
        self.automatic.SetValue(config.automatic)
        self._sizer.Add(self.automatic, 0, wx.ALL, 6)
        self.scope = self._field("translation_auto_scope", wx.Choice, choices=[
            self._t("ai_auto_scope_selected"), self._t("ai_auto_scope_all"),
        ])
        self.scope.SetSelection(int(config.scope == SCOPE_ALL))
        self.age = self._field("translation_auto_age", wx.Choice, choices=[
            self._t("ai_auto_age_hour") if minutes == 60
            else self._t("ai_auto_age_minutes").format(minutes=minutes)
            for minutes in AGE_CHOICES_MINUTES
        ])
        self.age.SetSelection(AGE_CHOICES_MINUTES.index(config.max_age_minutes))
        self.limit = self._field("translation_auto_limit", wx.SpinCtrl, min=0, max=10000,
                                 initial=config.daily_limit)
        self.get_models = self._button("translation_get_models", self._get_models)
        self.test = self._button("translation_test", self._test)
        self.status = self._field("translation_status", wx.TextCtrl,
                                  style=wx.TE_READONLY | wx.TE_MULTILINE)
        self._body.SetSizer(self._sizer)
        self._body.SetupScrolling(scroll_x=False, scrollIntoView=True)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(self._body, 1, wx.EXPAND)
        outer.Add(self.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, 6)
        self.SetSizer(outer)
        self.SetSize(self.FromDIP((600, 580)))
        self.provider.Bind(wx.EVT_CHOICE, self._change_provider)
        self.url.Bind(wx.EVT_TEXT, self._change_url)
        self.Bind(wx.EVT_BUTTON, self._accept, id=wx.ID_OK)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._destroyed)
        self._show_custom()

    def _field(self, key, control, **kwargs):
        label = self._t(key)
        self._sizer.Add(wx.StaticText(self._body, label=label), 0, wx.LEFT | wx.TOP, 6)
        widget = control(self._body, name=label.replace("&", ""), **kwargs)
        self._sizer.Add(widget, 0, wx.EXPAND | wx.ALL, 6)
        return widget

    def _button(self, key, handler):
        button = wx.Button(self._body, label=self._t(key))
        button.Bind(wx.EVT_BUTTON, handler)
        self._sizer.Add(button, 0, wx.ALL, 6)
        return button

    def _provider(self):
        return tuple(LABELS)[self.provider.GetSelection()]

    def _show_custom(self):
        self.url.Enable(self._provider() in CUSTOM)

    def _change_provider(self, event):
        self._show_custom()
        self._change_url(event)

    def _change_url(self, event):
        self._cancel()
        self.model.SetItems([])
        self.model.SetValue(default_model(self._provider()))
        self.key.ChangeValue("")
        self.status.ChangeValue("")

    def _values(self):
        provider = self._provider()
        base = normalized_base(self.url.GetValue()) if provider in CUSTOM else ""
        config = TranslationSettings(
            provider=provider, model=self.model.GetValue().strip() or default_model(provider),
            language=self.language.GetValue().strip(), base_url=base,
            automatic=self.automatic.GetValue(),
            scope=SCOPE_ALL if self.scope.GetSelection() == 1 else SCOPE_SELECTED,
            max_age_minutes=AGE_CHOICES_MINUTES[self.age.GetSelection()],
            daily_limit=self.limit.GetValue(),
            display=DISPLAY_MODES[self.display.GetSelection()],
        )
        draft = self.key.GetValue().strip()
        if len(draft) > 4096 or any(char.isspace() for char in draft):
            raise CredentialError()
        return config, draft

    def _refuse(self, key):
        """Why OK did nothing, where focus lands: a silent OK left the user
        in a dialog that seemed not to answer."""
        self.status.ChangeValue(self._t(key))
        self.status.SetFocus()

    def _accept(self, event):
        try:
            config, draft = self._values()
            if not config.model:
                self._refuse("translation_model_required")
                return
            translation_request(config, draft or self._get_key(credential_id(config)), SAMPLE)
        except (DescriptionError, CredentialError) as error:
            self._refuse(str(error))
            return
        config = replace(config, model_tested=self._tested_model == model_test_identity(config))
        if not translation_model_ready(config):
            self._refuse("translation_test_required")
            return
        if config.automatic and config.scope == SCOPE_ALL and (
                not self._initial.automatic or not self._initial.all_accounts_confirmed
                or self._initial.scope != SCOPE_ALL
                or credential_id(self._initial) != credential_id(config)):
            prompt = self._t("translation_auto_all_confirm").format(provider=LABELS[config.provider])
            if wx.MessageBox(prompt, self.GetTitle(), wx.YES_NO | wx.NO_DEFAULT, self) != wx.YES:
                return
        config = replace(config, all_accounts_confirmed=config.automatic and config.scope == SCOPE_ALL)
        self.result = config, draft
        self.EndModal(wx.ID_OK)

    def _get_models(self, event):
        self._run(lambda config, key, token: list_models(config, key, token), models=True)

    def _test(self, event):
        self._run(lambda config, key, token: translate(config, key, SAMPLE, token))

    def _run(self, work, *, models=False):
        self._cancel()
        token = RequestToken()
        try:
            config, draft = self._values()
            key = draft or self._get_key(credential_id(config))
            if not key and config.provider not in CUSTOM:
                raise CredentialError()
            self._token = token
            submit(lambda: work(config, key, token),
                   lambda value, error: wx.CallAfter(self._complete, token, config, value, error, models))
        except (DescriptionError, CredentialError) as error:
            self._cancel()
            self.status.ChangeValue(self._t(str(error)))
            return
        self.status.ChangeValue(self._t("ai_models_loading") if models else self._t("translation_waiting"))

    def _complete(self, token, config, value, error, models):
        if not self._alive or token is not self._token:
            return
        self._token = None
        try:
            current, _ = self._values()
        except (DescriptionError, CredentialError):
            return
        if credential_id(current) != credential_id(config) or (not models and current != config):
            return
        if error:
            self.status.ChangeValue(self._t(error))
        elif models:
            selected = self.model.GetValue()
            self.model.SetItems(list(value))
            self.model.SetValue(selected)
            self.status.ChangeValue(self._t("ai_models_ready")
                                    if value else self._t("ai_models_empty"))
        else:
            self._tested_model = model_test_identity(config)
            self.status.ChangeValue(value)

    def _cancel(self):
        if self._token is not None:
            self._token.cancel()
            self._token = None

    def _destroyed(self, event):
        if event.GetEventObject() is self:
            self._alive = False
            self._cancel()
        event.Skip()
