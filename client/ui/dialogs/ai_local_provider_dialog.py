"""The window that sets up one local model server (Ollama, LM Studio).

Unlike the provider window of an online service there is no key, no billing or
privacy page and no recommended model: the person gives the server's address
and picks a model they installed. "Get models" lists what the server reports
(``GET /models``) and "Test connection" asks for that list too; neither sends
a picture or a message. Edits a draft: nothing is saved until the Settings
dialog is applied.
"""
import wx
from wx.lib.scrolledpanel import ScrolledPanel

from core.ai_media import config as ai_config
from core.ai_media import local_llm
from core.ai_media.config import PROVIDERS, valid_model
from core.ai_media.errors import DescriptionError
from core.ai_media.local_url import is_loopback_url, normalize_url
from core.ai_media.service import RequestToken, probe_connection, submit
from .ai_provider_models import ModelSelectionMixin


def describe(t, config, provider):
    """What the window says about the draft: why the provider would not be
    used yet, and the warning that a server that is not this computer is sent
    the data. "" when there is nothing to warn about."""
    lines = []
    status = ai_config.local_status(config, provider)
    if status == "bad_url":
        lines.append(t("ai_error_local_url"))
    elif status == "no_model":
        lines.append(t("ai_local_no_model"))
    lines.extend(out_warnings(t, config, provider, status))
    return "\n".join(lines)


def _remote_address(config, provider):
    url = (config.get("local_urls") or {}).get(provider, "")
    return url if url and not is_loopback_url(url) else ""


def out_warnings(t, config, provider, status=None):
    """The warnings that what is sent leaves this computer: the address is not
    this computer's, and/or the model runs in Ollama's cloud."""
    status = status or ai_config.local_status(config, provider)
    lines = []
    if status != "bad_url" and _remote_address(config, provider):
        lines.append(t("ai_local_remote_warning"))
    if ai_config.cloud_model(config, provider):
        lines.append(t("ai_local_cloud_model_warning"))
    return lines


def spoken_warning(t, initial, draft, provider):
    """What pressing OK must say aloud: the warning of a draft that sends out,
    when it does so in a way the setup the window opened with did not (a new
    server, another address, a model that became a cloud one). The window only
    wrote it in a read-only field, which a screen reader user may never have
    visited. "" when there is nothing new to say."""
    if not ai_config.sends_out(draft, provider):
        return ""

    def route(config):
        return (_remote_address(config, provider), ai_config.cloud_model(config, provider))

    if ai_config.sends_out(initial, provider) and route(initial) == route(draft):
        return ""
    return "\n".join(out_warnings(t, draft, provider))


class AILocalProviderDialog(ModelSelectionMixin, wx.Dialog):
    #: The mixin's "no model" sentence for a server that lists none installed.
    _models_empty_key = "ai_local_models_empty"

    def __init__(self, parent, main_window, provider, state):
        self.main_window = main_window
        self._provider = provider
        # What the window opened with: pressing OK speaks only what is new.
        self._initial = {"models": {provider: state["model"]},
                         "local_urls": {provider: state["url"]}}
        self._alive = True
        self._probe = None
        self._probe_generation = 0
        self._result = None
        self._init_model_catalog()
        spec = PROVIDERS[provider]
        super().__init__(parent, title=self._t("ai_provider_button").format(provider=spec.name),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        body = ScrolledPanel(self)
        self.sizer = wx.BoxSizer(wx.VERTICAL)
        self.enabled = wx.CheckBox(body, label=self._t("ai_provider_enabled_checkbox"))
        self.enabled.SetValue(state["enabled"])
        self.sizer.Add(self.enabled, 0, wx.ALL, 8)
        # Read-only field, not a StaticText: Tab skips a StaticText, so a screen
        # reader user moving on to the address would never hear it.
        note = wx.TextCtrl(body, value=self._t("ai_local_provider_note"),
                           style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 90))
        self.sizer.Add(note, 0, wx.EXPAND | wx.ALL, 8)
        self.url = self._text(body, "ai_local_url")
        self.url.SetMaxLength(200)
        self.url.ChangeValue(state["url"])
        self.get_models = self._button(body, "ai_get_models", self._fetch_models)
        self.model_choice = self._choice(body, "ai_model_choice", [])
        self.model_choice.Enable(False)
        self.model = self._text(body, "ai_model")
        self.model.SetMaxLength(100)
        self.model.ChangeValue(state["model"])
        self._button(body, "ai_test_connection", self._test)
        self._caption(body, "ai_status_label")
        self.status = wx.TextCtrl(body, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 80),
                                  name=self._plain("ai_status_label"))
        self.sizer.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
        body.SetSizer(self.sizer)
        body.SetupScrolling(scroll_x=False, rate_y=15, scrollIntoView=True)
        self._body = body
        self.url.Bind(wx.EVT_TEXT, self._url_changed)
        self.model_choice.Bind(wx.EVT_CHOICE, self._select_model)
        self.model.Bind(wx.EVT_TEXT, self._manual_model_changed)
        self.Bind(wx.EVT_BUTTON, self._ok, id=wx.ID_OK)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._destroyed)
        buttons = wx.StdDialogButtonSizer()
        ok = wx.Button(self, wx.ID_OK, label=self._t("ok"))
        cancel = wx.Button(self, wx.ID_CANCEL, label=self._t("cancel"))
        buttons.AddButton(ok)
        buttons.AddButton(cancel)
        buttons.Realize()
        ok.SetDefault()
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(body, 1, wx.EXPAND)
        outer.Add(buttons, 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(outer)
        self.SetSize((560, 600))
        self.SetMinSize((420, 360))
        self._describe()
        self.enabled.SetFocus()

    def _t(self, key):
        return self.main_window.i18n.t(key)

    def _plain(self, key):
        """A label without its mnemonic marker, for a control's name."""
        return self._t(key).replace("&", "")

    def _caption(self, body, key):
        label = wx.StaticText(body, label=self._t(key))
        self.sizer.Add(label, 0, wx.LEFT | wx.TOP, 8)

    def _text(self, body, key, style=0):
        self._caption(body, key)
        control = wx.TextCtrl(body, style=style, name=self._plain(key))
        self.sizer.Add(control, 0, wx.EXPAND | wx.ALL, 8)
        return control

    def _choice(self, body, key, choices):
        self._caption(body, key)
        control = wx.Choice(body, choices=choices, name=self._plain(key))
        self.sizer.Add(control, 0, wx.EXPAND | wx.ALL, 8)
        return control

    def _button(self, body, key, handler):
        button = wx.Button(body, label=self._t(key))
        button.Bind(wx.EVT_BUTTON, handler)
        self.sizer.Add(button, 0, wx.ALL, 8)
        return button

    def _draft_config(self):
        """The draft as the settings read it, to say what would happen."""
        return {"models": {self._provider: self.model.GetValue().strip()},
                "local_urls": {self._provider: self._address()}}

    def _address(self):
        """The normalized address typed, "" when it is not usable. Blank is
        the server's usual address."""
        typed = self.url.GetValue().strip() or PROVIDERS[self._provider].default_url
        try:
            return normalize_url(typed)
        except DescriptionError:
            return ""

    def _describe(self):
        self.status.ChangeValue(describe(self._t, self._draft_config(), self._provider))

    def _url_changed(self, event):
        self._cancel_model_list(clear=True)
        self._cancel_probe()
        self._describe()
        event.Skip()

    def _manual_model_changed(self, event):
        super()._manual_model_changed(event)
        self._describe()

    def _select_model(self, event):
        super()._select_model(event)
        self._describe()

    def _model_source(self):
        address = self._address()
        if not address:
            raise DescriptionError("local_url")
        return lambda token: local_llm.fetch_models(address, token)

    def _test(self, event):
        self._cancel_model_list()
        try:
            address = self._address()
            if not address:
                raise DescriptionError("local_url")
            if self._probe:
                self._probe.cancel()
            provider = self._provider
            self._probe = token = RequestToken()
            self._probe_generation += 1
            generation = self._probe_generation
            self.status.ChangeValue(self._t("ai_connection_loading"))
            submit(lambda: probe_connection(provider, "", "", token, base_url=address),
                   lambda result, error: wx.CallAfter(self._tested, generation, error))
        except DescriptionError as exc:
            self.status.ChangeValue(self._t(str(exc)))

    def _tested(self, generation, error):
        if self._alive and generation == self._probe_generation:
            text = self._t(error or "ai_local_connection_ok")
            self.status.ChangeValue(text)
            self.main_window.output(text)

    def _cancel_probe(self):
        self._probe_generation += 1
        if self._probe:
            self._probe.cancel()
        self._probe = None

    def _ok(self, event):
        """Keep the window open, saying why, while the address or the model
        cannot be used. A blank model is allowed: the provider is then simply
        not used, which the status says."""
        model = self.model.GetValue().strip()
        address = self._address()
        problem, field = None, None
        if not address:
            problem, field = "ai_error_local_url", self.url
        elif model and not valid_model(model):
            problem, field = "ai_error_request", self.model
        if problem:
            text = self._t(problem)
            self.status.ChangeValue(text)
            self.main_window.output(text)
            field.SetFocus()
            return
        warning = spoken_warning(self._t, self._initial, self._draft_config(), self._provider)
        if warning:
            self.main_window.output(warning)
        self._result = {"url": address, "model": model, "enabled": self.enabled.GetValue()}
        event.Skip()

    def values(self):
        """The draft: the normalized address, the model ("" when none was
        chosen) and the on/off switch."""
        return self._result

    def _destroyed(self, event):
        if event.GetEventObject() is self:
            self._alive = False
            self._cancel_model_list()
            self._cancel_probe()
        event.Skip()
