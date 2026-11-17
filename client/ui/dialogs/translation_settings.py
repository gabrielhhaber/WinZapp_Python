"""Settings and model discovery for translation of text messages.

The Provider list holds every engine: the offline model first (nothing leaves
the computer), then the translation services, then the AI providers. Only the
fields the chosen engine reads are shown: a hidden field is out of the Tab
order, where a disabled one would still be announced and do nothing.
"""
from dataclasses import replace

import wx
from wx.lib.scrolledpanel import ScrolledPanel

from core.ai_credentials import CredentialError
from core.ai_media.config import PROVIDERS
from core.ai_media.errors import DescriptionError
from core.ai_media.service import RequestToken, submit
from core.i18n import LANGUAGE_NAMES
from core.message_translation import (
    AGE_CHOICES_MINUTES, CUSTOM, DISPLAY_MODES, LOCAL, LOCAL_LLM_URLS, PROVIDER_NAMES, SCOPE_ALL,
    SCOPE_SELECTED, TranslationSettings, check_request, credential_id, default_model, default_url,
    is_ai, list_models, model_test_identity, needs_key, normalized_base, sends_text_out, translate,
    translation_model_ready, uses_key,
)
from core.text_translation import languages, services
from ui.dialogs.translation_local_models import TranslationLocalModelsMixin


#: Every engine's name but the offline model's, which is translated.
LABELS = PROVIDER_NAMES | {"custom_openai": "OpenAI Compatible",
                           "custom_anthropic": "Anthropic Compatible"}
#: The order of the Provider list.
ORDER = (LOCAL, *services.SERVICES, *PROVIDERS, "grok", *LOCAL_LLM_URLS,
         "custom_openai", "custom_anthropic")
SAMPLE = "Hello! See you tomorrow."


def provider_label(t, provider):
    """The name the user reads for an engine (the offline model's is a
    sentence in their language; the others are proper names)."""
    if provider == LOCAL:
        return t("translation_provider_local")
    return LABELS.get(provider, provider)


def takes_url(provider):
    return provider in CUSTOM or provider in services.WITH_URL


def language_items(provider, supported, ui_language):
    """The Target language list: the interface languages for an AI provider
    (any language can still be typed), the endonyms the engine knows otherwise."""
    if is_ai(provider):
        return list(LANGUAGE_NAMES.values())
    return [name for _code, name in languages.choices(supported, ui_language)]


class TranslationSettingsDialog(TranslationLocalModelsMixin, wx.Dialog):
    def __init__(self, parent, i18n, config, get_key, title, speak_output=None):
        super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._body = ScrolledPanel(self)
        self._i18n, self._t, self._get_key = i18n, i18n.t, get_key
        self._speak_output = speak_output
        self._initial = config
        self._tested_model = model_test_identity(config) if config.model_tested else None
        self._token = None
        self._alive = True
        self._labels = {}
        self._sizer = wx.BoxSizer(wx.VERTICAL)
        self.provider = self._field("translation_provider", wx.Choice,
                                    choices=[provider_label(self._t, p) for p in ORDER])
        self.provider.SetSelection(ORDER.index(config.provider) if config.provider in ORDER else 0)
        self.url = self._field("translation_api_url", wx.TextCtrl, value=config.base_url)
        self.key = self._field("translation_key", wx.TextCtrl, style=wx.TE_PASSWORD)
        self.region = self._field("translation_region", wx.TextCtrl, value=config.region)
        self.model = self._field("translation_model", wx.ComboBox,
                                 value=(config.model if is_ai(config.provider) else "")
                                 or default_model(self._provider()), style=wx.CB_DROPDOWN)
        self._build_local_section(config)
        self.language = self._field("translation_language", wx.ComboBox, value=config.language,
                                    style=wx.CB_DROPDOWN)
        self.source = self._field("translation_source_language", wx.Choice)
        self._source_codes = ()
        self._fill_languages(config.source_language)
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
        self.SetSize(self.FromDIP((600, 620)))
        self.provider.Bind(wx.EVT_CHOICE, self._change_provider)
        self.url.Bind(wx.EVT_TEXT, self._change_url)
        self.Bind(wx.EVT_BUTTON, self._accept, id=wx.ID_OK)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._destroyed)
        self._show_fields()

    def _field(self, key, control, **kwargs):
        label = self._t(key)
        text = wx.StaticText(self._body, label=label)
        self._sizer.Add(text, 0, wx.LEFT | wx.TOP, 6)
        widget = control(self._body, name=label.replace("&", ""), **kwargs)
        self._sizer.Add(widget, 0, wx.EXPAND | wx.ALL, 6)
        self._labels[widget] = text
        return widget

    def _button(self, key, handler):
        button = wx.Button(self._body, label=self._t(key))
        button.Bind(wx.EVT_BUTTON, handler)
        self._sizer.Add(button, 0, wx.ALL, 6)
        return button

    def _show(self, widget, shown):
        widget.Show(shown)
        label = self._labels.get(widget)
        if label is not None:
            label.Show(shown)

    def _provider(self):
        return ORDER[max(self.provider.GetSelection(), 0)]

    def _show_fields(self):
        provider = self._provider()
        ai = is_ai(provider)
        self._show(self.url, takes_url(provider))
        self._show(self.key, uses_key(provider))
        self._show(self.region, provider in services.WITH_REGION)
        self._show(self.model, ai)
        self._show_local_section(provider == LOCAL)
        self._show(self.source, not ai)
        self._show(self.get_models, ai)
        self._body.Layout()
        self._body.FitInside()

    def _supported_languages(self):
        """Which language codes the chosen engine can name; None for all."""
        if self._provider() == LOCAL:
            return self._local_supports
        return None

    def _fill_languages(self, source=None):
        """Refill Target and Source for the chosen engine, keeping the choice:
        a target typed for an AI provider becomes its endonym when the engine
        needs a code, and the other way round nothing is lost."""
        provider = self._provider()
        ui_language = getattr(self._i18n, "language", "")
        value = self.language.GetValue()
        code = languages.language_code(value, LANGUAGE_NAMES)
        self.language.SetItems(language_items(provider, self._supported_languages(), ui_language))
        if not is_ai(provider) and code in languages.ENDONYMS:
            value = languages.ENDONYMS[code]
        self.language.SetValue(value)
        if source is None:
            source = self._source_value()
        source_code = languages.language_code(source, LANGUAGE_NAMES) if source else None
        rows = languages.choices(self._supported_languages(), ui_language)
        self._source_codes = ("",) + tuple(row[0] for row in rows)
        self.source.SetItems([self._t("translation_source_auto")] + [row[1] for row in rows])
        self.source.SetSelection(self._source_codes.index(source_code)
                                 if source_code in self._source_codes else 0)

    def _source_value(self):
        index = self.source.GetSelection()
        if 0 < index < len(self._source_codes):
            return languages.ENDONYMS[self._source_codes[index]]
        return ""

    def _change_provider(self, event):
        provider = self._provider()
        defaults = {*LOCAL_LLM_URLS.values(), *services.DEFAULT_URLS.values(), ""}
        if takes_url(provider) and self.url.GetValue().strip() in defaults:
            self.url.ChangeValue(default_url(provider))
        self._show_fields()
        self._fill_languages()
        self._change_url(event)

    def _change_url(self, event):
        self._cancel()
        self.model.SetItems([])
        self.model.SetValue(default_model(self._provider()) if is_ai(self._provider()) else "")
        self.key.ChangeValue("")
        self.status.ChangeValue("")

    def _values(self):
        provider = self._provider()
        base = ""
        if provider in services.WITH_URL:
            base = services.service_base(self.url.GetValue())
        elif provider in CUSTOM:
            base = normalized_base(self.url.GetValue())
        if provider == LOCAL:
            model = self._local_model().id
        elif is_ai(provider):
            model = self.model.GetValue().strip() or default_model(provider)
        else:
            model = ""
        config = TranslationSettings(
            provider=provider, model=model,
            language=self.language.GetValue().strip(), base_url=base,
            source_language="" if is_ai(provider) else self._source_value(),
            device=self._local_device(),
            region=self.region.GetValue().strip().lower() if provider in services.WITH_REGION else "",
            automatic=self.automatic.GetValue(),
            scope=SCOPE_ALL if self.scope.GetSelection() == 1 else SCOPE_SELECTED,
            max_age_minutes=AGE_CHOICES_MINUTES[self.age.GetSelection()],
            daily_limit=self.limit.GetValue(),
            display=DISPLAY_MODES[self.display.GetSelection()],
        )
        draft = self.key.GetValue().strip() if uses_key(provider) else ""
        if len(draft) > 4096 or any(char.isspace() for char in draft):
            raise CredentialError()
        return config, draft

    def _saved_key(self, config):
        return self._get_key(credential_id(config)) if uses_key(config.provider) else ""

    def _refuse(self, key):
        """Why OK did nothing, where focus lands: a silent OK left the user
        in a dialog that seemed not to answer."""
        self.status.ChangeValue(self._t(key))
        self.status.SetFocus()

    def _accept(self, event):
        try:
            config, draft = self._values()
            if is_ai(config.provider) and not config.model:
                self._refuse("translation_model_required")
                return
            check_request(config, draft or self._saved_key(config), SAMPLE)
        except (DescriptionError, CredentialError) as error:
            self._refuse(str(error))
            return
        config = replace(config, model_tested=self._tested_model == model_test_identity(config))
        if not translation_model_ready(config):
            self._refuse("translation_local_download_required" if config.provider == LOCAL
                         else "translation_test_required")
            return
        if config.automatic and config.scope == SCOPE_ALL and sends_text_out(config.provider) and (
                not self._initial.automatic or not self._initial.all_accounts_confirmed
                or self._initial.scope != SCOPE_ALL
                or credential_id(self._initial) != credential_id(config)):
            prompt = self._t("translation_auto_all_confirm").format(
                provider=provider_label(self._t, config.provider))
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
            key = draft or self._saved_key(config)
            if needs_key(config.provider) and not key:
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
