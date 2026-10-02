"""Install-wide photo-description settings, isolated from WhatsApp API keys."""
import wx
from wx.lib.scrolledpanel import ScrolledPanel

from app_paths import global_dir
from app_settings import AppSettings
from core.ai_credentials import CredentialStore, CredentialError
from core.image_description.config import PROVIDERS, PROFILES, preferences, valid_model
from core.image_description.errors import DescriptionError
from core.image_description.service import RequestToken, probe_connection, submit
from .image_description_models import ModelSelectionMixin


class ImageDescriptionSettingsPage(ModelSelectionMixin, ScrolledPanel):
    def __init__(self, parent, main_window):
        super().__init__(parent)
        self.main_window = main_window
        self.app = getattr(main_window, "app_settings", None) or AppSettings(global_dir())
        self.store = CredentialStore(global_dir())
        self._labels = []
        self._alive = True
        self._init_model_catalog()
        self._probe = None
        self._probe_generation = 0
        self._drafts = {}
        self._deleted = set()
        self._reset = False
        raw = self.app.get("image_description")
        raw = raw if isinstance(raw, dict) else {}
        self._models = dict(raw.get("models", {})) if isinstance(raw.get("models"), dict) else {}
        config = preferences(self.app)
        self._provider = config["provider"]
        self.sizer = wx.BoxSizer(wx.VERTICAL)
        self.enabled = self._check("ai_enabled", config["enabled"])
        self.provider = self._choice("ai_provider", [p.name for p in PROVIDERS.values()])
        self.provider.SetSelection(list(PROVIDERS).index(self._provider))
        self.key = self._text("ai_api_key", style=wx.TE_PASSWORD)
        self.key.SetMaxLength(4096)
        self.key_state = wx.StaticText(self)
        self.sizer.Add(self.key_state, 0, wx.ALL, 8)
        self.revealed = self._text("ai_key_readable", style=wx.TE_READONLY)
        self.revealed.Hide()
        self._labels[-1][0].Hide()
        self.show_key = self._button("ai_show_key", self._show_key)
        self._button("ai_delete_key", self._delete_key)
        self._button("ai_reset_keys", self._reset_keys)
        self._button("ai_get_key", lambda e: wx.LaunchDefaultBrowser(PROVIDERS[self._provider].key_url))
        self.get_models = self._button("ai_get_models", self._fetch_models)
        self.model_choice = self._choice("ai_model_choice", [])
        self.model_choice.Enable(False)
        self.model = self._text("ai_model")
        self.model.ChangeValue(config["model"])
        self.profile = self._choice("ai_profile", self._profile_labels())
        self.profile.SetSelection(PROFILES.index(config["profile"]))
        self.read_answers = self._check("ai_read_answers", config["read_answers"])
        self._button("ai_test_connection", self._test)
        self._button("ai_billing", lambda e: wx.LaunchDefaultBrowser(PROVIDERS[self._provider].billing_url))
        self._button("ai_privacy_link", lambda e: wx.LaunchDefaultBrowser(PROVIDERS[self._provider].privacy_url))
        self._label("ai_settings_help")
        self.notice = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 155))
        self.sizer.Add(self.notice, 0, wx.EXPAND | wx.ALL, 8)
        self._button("ai_technical_info", self._technical_info)
        self._label("ai_status")
        self.status = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 65))
        self.sizer.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(self.sizer)
        self.SetupScrolling(scroll_x=False, rate_y=15, scrollIntoView=True)
        self.provider.Bind(wx.EVT_CHOICE, self._change_provider)
        self.key.Bind(wx.EVT_TEXT, self._key_changed)
        self.model_choice.Bind(wx.EVT_CHOICE, self._select_model)
        self.model.Bind(wx.EVT_TEXT, self._manual_model_changed)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._destroyed)
        self.refresh_labels()
        self._key_status()

    def DoGetBestSize(self):
        # The page has many occasional-use actions. Its virtual content may
        # be tall; it must not make SettingsDialog.Fit() exceed the screen.
        return wx.Size(420, 450)

    def _t(self, key):
        return self.main_window.i18n.t(key)

    def _label(self, key):
        label = wx.StaticText(self, label=self._t(key))
        self._labels.append((label, key))
        self.sizer.Add(label, 0, wx.LEFT | wx.TOP, 8)

    def _text(self, key, style=0):
        self._label(key)
        control = wx.TextCtrl(self, style=style, name=self._t(key))
        self.sizer.Add(control, 0, wx.EXPAND | wx.ALL, 8)
        return control

    def _choice(self, key, choices):
        self._label(key)
        control = wx.Choice(self, choices=choices, name=self._t(key))
        self.sizer.Add(control, 0, wx.EXPAND | wx.ALL, 8)
        return control

    def _check(self, key, value):
        control = wx.CheckBox(self, label=self._t(key))
        self._labels.append((control, key))
        control.SetValue(value)
        self.sizer.Add(control, 0, wx.ALL, 8)
        return control

    def _button(self, key, handler):
        button = wx.Button(self, label=self._t(key))
        self._labels.append((button, key))
        button.Bind(wx.EVT_BUTTON, handler)
        self.sizer.Add(button, 0, wx.ALL, 8)
        return button

    def _profile_labels(self):
        return [self._t("ai_fast"), self._t("ai_balanced"), self._t("ai_detailed")]

    def refresh_labels(self):
        for control, key in self._labels:
            control.SetLabel(self._t(key))
        for control, key in ((self.key, "ai_api_key"), (self.revealed, "ai_key_readable"),
                             (self.provider, "ai_provider"), (self.profile, "ai_profile"),
                             (self.model, "ai_model"), (self.model_choice, "ai_model_choice")):
            control.SetName(self._t(key))
        selection = self.profile.GetSelection()
        self.profile.SetItems(self._profile_labels())
        self.profile.SetSelection(selection)
        self.notice.SetName(self._t("ai_settings_help"))
        self.notice.ChangeValue(self._t("ai_settings_notice"))
        self.status.SetName(self._t("ai_status"))

    def _key_status(self):
        if self._provider in self._deleted:
            self.key_state.SetLabel(self._t("ai_key_removal_pending"))
            return
        try:
            saved = bool(self.store.get(self._provider))
            self.key_state.SetLabel(self._t("ai_key_saved" if saved else "ai_key_missing"))
        except CredentialError:
            self.key_state.SetLabel(self._t("ai_error_credentials"))

    def _current_key(self):
        return self.key.GetValue().strip() or ("" if self._reset or self._provider in self._deleted else
                                               self.store.get(self._provider))

    def _reset_keys(self, event):
        if wx.MessageBox(self._t("ai_reset_confirm"), self._t("ai_reset_keys"),
                         wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING, self) != wx.YES:
            return
        self._reset = True
        self._cancel_model_list(clear=True)
        self._cancel_probe()
        self._deleted = set(PROVIDERS)
        self._drafts.clear()
        self.key.SetValue("")
        self._hide_key()
        self._key_status()
        self.GetParent().GetParent()._mark_dirty()

    def _show_key(self, event):
        try:
            if self.revealed.IsShown():
                self.revealed.ChangeValue("")
                self.revealed.Hide()
            else:
                self.revealed.ChangeValue(self._current_key())
                self.revealed.Show()
                self.revealed.SetFocus()
            # Its label directly precedes the read-only control.
            for label, key in self._labels:
                if key == "ai_key_readable":
                    label.Show(self.revealed.IsShown())
            self.Layout()
            self.FitInside()
        except CredentialError:
            self.status.ChangeValue(self._t("ai_error_credentials"))

    def _hide_key(self):
        self.revealed.ChangeValue("")
        self.revealed.Hide()
        for label, key in self._labels:
            if key == "ai_key_readable":
                label.Hide()

    def _key_changed(self, event):
        self._cancel_model_list(clear=True)
        self._cancel_probe()
        self._hide_key()
        event.Skip()

    def _delete_key(self, event):
        self._cancel_model_list(clear=True)
        self._cancel_probe()
        self._deleted.add(self._provider)
        self.key.SetValue("")
        self._drafts.pop(self._provider, None)
        self._hide_key()
        self._key_status()
        self.status.ChangeValue(self._t("ai_key_removal_notice"))
        # Deletion is staged until Apply/OK, including dirty-button visibility.
        dialog = self.GetParent().GetParent()
        dialog._mark_dirty()

    def _technical_info(self, event):
        dialog = wx.Dialog(self, title=self._t("ai_technical_info"), size=(560, 400),
                           style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        try:
            layout = wx.BoxSizer(wx.VERTICAL)
            text = wx.TextCtrl(dialog, value=self._t("ai_technical_notice"),
                               style=wx.TE_MULTILINE | wx.TE_READONLY,
                               name=self._t("ai_technical_info"))
            layout.Add(text, 1, wx.EXPAND | wx.ALL, 12)
            close = wx.Button(dialog, wx.ID_CANCEL, label=self._t("close"))
            layout.Add(close, 0, wx.ALIGN_RIGHT | wx.ALL, 12)
            dialog.SetSizer(layout)
            dialog.SetMinSize((420, 260))
            text.SetFocus()
            dialog.ShowModal()
        finally:
            dialog.Destroy()

    def _change_provider(self, event):
        self._cancel_model_list(clear=True)
        self._drafts[self._provider] = self.key.GetValue().strip()
        self._models[self._provider] = self.model.GetValue().strip()
        self._provider = list(PROVIDERS)[self.provider.GetSelection()]
        self.key.ChangeValue(self._drafts.get(self._provider, ""))
        self.model.ChangeValue(self._models.get(self._provider, PROVIDERS[self._provider].model))
        self._hide_key()
        self._cancel_probe()
        self.status.ChangeValue("")
        self._key_status()
        event.Skip()

    def _test(self, event):
        self._cancel_model_list()
        try:
            if self._probe:
                self._probe.cancel()
            provider, model, key = self._provider, self.model.GetValue().strip(), self._current_key()
            self._probe = token = RequestToken()
            self._probe_generation += 1
            generation = self._probe_generation
            self.status.ChangeValue(self._t("ai_connection_loading"))
            submit(lambda: probe_connection(provider, model, key, token),
                   lambda result, error: wx.CallAfter(self._tested, generation, error))
        except (DescriptionError, CredentialError) as exc:
            self.status.ChangeValue(self._t(str(exc)))

    def _tested(self, generation, error):
        if self._alive and generation == self._probe_generation:
            self.status.ChangeValue(self._t(error or "ai_connection_ok"))

    def _cancel_probe(self):
        self._probe_generation += 1
        if self._probe:
            self._probe.cancel()
        self._probe = None

    def apply(self):
        self._drafts[self._provider] = self.key.GetValue().strip()
        self._models[self._provider] = self.model.GetValue().strip()
        if any(not valid_model(model) for model in self._models.values()):
            self.status.ChangeValue(self._t("ai_error_request"))
            self.model.SetFocus()
            return False
        try:
            changes = {provider: None for provider in self._deleted}
            changes.update({provider: key for provider, key in self._drafts.items() if key})
            keys_changed = bool(changes) or self._reset
            self.store.apply(changes, reset=self._reset)
            def merge(old):
                value = dict(old) if isinstance(old, dict) else {}
                value.update(enabled=self.enabled.GetValue(), provider=self._provider,
                             models=self._models, profile=PROFILES[self.profile.GetSelection()],
                             read_answers=self.read_answers.GetValue())
                if self._deleted:
                    value["consented"] = [p for p in value.get("consented", []) if p not in self._deleted]
                return value
            self.app.update("image_description", merge)
            self._drafts.clear()
            self._deleted.clear()
            self._reset = False
            self.key.ChangeValue("")
            if keys_changed:
                self._cancel_model_list(clear=True)
                self._cancel_probe()
            self._hide_key()
            self._key_status()
            if keys_changed:
                self.status.ChangeValue("")
            return True
        except (CredentialError, OSError, RuntimeError):
            self.status.ChangeValue(self._t("ai_error_credentials"))
            return False

    def _destroyed(self, event):
        if event.GetEventObject() is self:
            self._alive = False
            self._cancel_model_list()
            self._drafts.clear()
            self._cancel_probe()
        event.Skip()
