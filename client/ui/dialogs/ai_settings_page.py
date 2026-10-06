"""The "Transcriptions and Descriptions" page of Settings, and the window that
sets up one provider.

Install-wide, shared by every account (``app.json``), with the API keys in the
separate encrypted store of core/ai_credentials.py: nothing here touches the
per-account settings, so a key can never reach a settings export.

Design notes that are easy to undo by accident:

* The provider list is a plain ListBox whose item text embeds the state
  ("Gemini, enabled, key saved"), not a CheckListBox: NVDA does not reliably
  announce the check state of a CheckListBox item. The position in the list IS
  the order in which the providers are tried.
* The key field is masked, with a button that reveals it in a read-only field,
  so the screen reader can read it back on request without it sitting in
  plain view.
* Keys, deletions and the reset are staged until Apply/OK, like every other
  setting in this dialog; Cancel discards them.
"""
import wx
from wx.lib.scrolledpanel import ScrolledPanel

from app_paths import global_dir
from app_settings import AppSettings
from core.ai_credentials import CredentialStore, CredentialError
from core.ai_media.config import KINDS, PROFILES, PROVIDERS, preferences, supports, valid_model
from core.ai_media.errors import DescriptionError
from core.ai_media.service import RequestToken, probe_connection, submit
from .ai_provider_models import ModelSelectionMixin

#: The per-kind switches: (kind, i18n key of its label).
KIND_TOGGLES = (
    ("image", "ai_describe_images_label"),
    ("sticker", "ai_describe_stickers_label"),
    ("video", "ai_describe_videos_label"),
    ("audio", "ai_transcribe_audio_label"),
    ("pdf", "ai_pdf_accessible_label"),
)
assert [kind for kind, _key in KIND_TOGGLES] == list(KINDS)
#: i18n key naming each kind, for the "handles" line of the provider window.
KIND_NAMES = (("image", "ai_kind_image"), ("sticker", "ai_kind_sticker"), ("video", "ai_kind_video"),
              ("audio", "ai_kind_audio"), ("pdf", "ai_kind_pdf"))


class AIProviderDialog(ModelSelectionMixin, wx.Dialog):
    """Key, model and on/off of one provider. Edits a draft: nothing is saved
    until the Settings dialog is applied."""

    def __init__(self, parent, main_window, provider, state, store, reset):
        self.main_window = main_window
        self._provider = provider
        self.store = store
        self._reset = reset
        self._deleted = state["deleted"]
        self._alive = True
        self._probe = None
        self._probe_generation = 0
        self._init_model_catalog()
        spec = PROVIDERS[provider]
        super().__init__(parent, title=self._t("ai_provider_button").format(provider=spec.name),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        body = ScrolledPanel(self)
        self.sizer = wx.BoxSizer(wx.VERTICAL)
        self._labels = []
        self.enabled = wx.CheckBox(body, label=self._t("ai_provider_enabled_checkbox"))
        self.enabled.SetValue(state["enabled"])
        self.sizer.Add(self.enabled, 0, wx.ALL, 8)
        kinds = ", ".join(self._t(key) for kind, key in KIND_NAMES if supports(provider, kind))
        self.sizer.Add(wx.StaticText(body, label=self._t("ai_provider_supports").format(kinds=kinds)),
                       0, wx.ALL, 8)
        self.key = self._text(body, "ai_api_key", style=wx.TE_PASSWORD)
        self.key.SetMaxLength(4096)
        self.key.ChangeValue(state["key"])
        self.key_state = wx.StaticText(body)
        self.sizer.Add(self.key_state, 0, wx.ALL, 8)
        self.revealed = self._text(body, "ai_key_readable", style=wx.TE_READONLY)
        self._hide_key()
        self._button(body, "ai_show_key", self._show_key)
        self._button(body, "ai_delete_key", self._delete_key)
        self._button(body, "ai_get_key", lambda e: wx.LaunchDefaultBrowser(spec.key_url))
        self.get_models = self._button(body, "ai_get_models", self._fetch_models)
        self.automatic = wx.CheckBox(body, label=self._t("ai_model_automatic"))
        self.automatic.SetValue(not state["model"])
        self.sizer.Add(self.automatic, 0, wx.ALL, 8)
        self.model_choice = self._choice(body, "ai_model_choice", [])
        self.model_choice.Enable(False)
        self.model = self._text(body, "ai_model")
        self.model.ChangeValue(state["model"] or spec.model)
        self._apply_automatic()
        self._button(body, "ai_test_connection", self._test)
        self._button(body, "ai_billing", lambda e: wx.LaunchDefaultBrowser(spec.billing_url))
        self._button(body, "ai_privacy_link", lambda e: wx.LaunchDefaultBrowser(spec.privacy_url))
        self._caption(body, "status")
        self.status = wx.TextCtrl(body, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 65),
                                  name=self._t("status"))
        self.sizer.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
        body.SetSizer(self.sizer)
        body.SetupScrolling(scroll_x=False, rate_y=15, scrollIntoView=True)
        self._body = body
        self.key.Bind(wx.EVT_TEXT, self._key_changed)
        self.automatic.Bind(wx.EVT_CHECKBOX, self._automatic_changed)
        self.model_choice.Bind(wx.EVT_CHOICE, self._select_model)
        self.model.Bind(wx.EVT_TEXT, self._manual_model_changed)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._destroyed)
        buttons = wx.StdDialogButtonSizer()
        ok = wx.Button(self, wx.ID_OK, label=self._plain("ok"))
        cancel = wx.Button(self, wx.ID_CANCEL, label=self._plain("cancel"))
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
        self._key_status()
        self.enabled.SetFocus()

    def _t(self, key):
        return self.main_window.i18n.t(key)

    def _plain(self, key):
        """A label without its mnemonic marker (see ai_result_dialog.plain)."""
        return self._t(key).replace("&", "")

    def _caption(self, body, key):
        label = wx.StaticText(body, label=self._t(key))
        self._labels.append((label, key))
        self.sizer.Add(label, 0, wx.LEFT | wx.TOP, 8)

    def _text(self, body, key, style=0):
        self._caption(body, key)
        control = wx.TextCtrl(body, style=style, name=self._t(key))
        self.sizer.Add(control, 0, wx.EXPAND | wx.ALL, 8)
        return control

    def _choice(self, body, key, choices):
        self._caption(body, key)
        control = wx.Choice(body, choices=choices, name=self._t(key))
        self.sizer.Add(control, 0, wx.EXPAND | wx.ALL, 8)
        return control

    def _button(self, body, key, handler):
        button = wx.Button(body, label=self._t(key))
        button.Bind(wx.EVT_BUTTON, handler)
        self.sizer.Add(button, 0, wx.ALL, 8)
        return button

    def _apply_automatic(self):
        """Automatic follows the model WinZapp recommends: a pinned model is
        edited only while it is off."""
        pinned = self._model_pinned()
        if not pinned:
            self.model.ChangeValue(PROVIDERS[self._provider].model)
        self.model.Enable(pinned)
        self.get_models.Enable(pinned)
        self.model_choice.Enable(pinned and bool(self._model_options))

    def _automatic_changed(self, event):
        self._cancel_model_list(clear=True)
        self._cancel_probe()
        self._apply_automatic()
        event.Skip()

    def _current_key(self):
        typed = self.key.GetValue().strip()
        if typed:
            return typed
        return "" if self._reset or self._deleted else self.store.get(self._provider)

    def _key_status(self):
        if self._deleted and not self.key.GetValue().strip():
            self.key_state.SetLabel(self._t("ai_key_removal_pending"))
            return
        try:
            saved = bool(self.key.GetValue().strip() or
                         (not self._reset and self.store.get(self._provider)))
            self.key_state.SetLabel(self._t("ai_key_saved" if saved else "ai_key_missing"))
        except CredentialError:
            self.key_state.SetLabel(self._t("ai_error_credentials"))

    def _show_key(self, event):
        try:
            if self.revealed.IsShown():
                self._hide_key()
            else:
                self.revealed.ChangeValue(self._current_key())
                self._reveal(True)
                self.revealed.SetFocus()
            self._body.Layout()
            self._body.FitInside()
        except CredentialError:
            self.status.ChangeValue(self._t("ai_error_credentials"))

    def _reveal(self, shown):
        self.revealed.Show(shown)
        for label, key in self._labels:
            if key == "ai_key_readable":
                label.Show(shown)

    def _hide_key(self):
        self.revealed.ChangeValue("")
        self._reveal(False)

    def _key_changed(self, event):
        self._cancel_model_list(clear=True)
        self._cancel_probe()
        self._hide_key()
        if self.key.GetValue().strip():
            self._deleted = False
        self._key_status()
        event.Skip()

    def _delete_key(self, event):
        self._cancel_model_list(clear=True)
        self._cancel_probe()
        self._deleted = True
        self.key.ChangeValue("")
        self._hide_key()
        self._key_status()
        self.status.ChangeValue(self._t("ai_key_removal_notice"))

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

    def values(self):
        """The draft as entered: a typed key (blank keeps the saved one), whether
        the saved one is to be removed, the model and the on/off switch."""
        return {"key": self.key.GetValue().strip(), "deleted": self._deleted,
                "model": "" if self.automatic.GetValue() else self.model.GetValue().strip(),
                "enabled": self.enabled.GetValue()}

    def _destroyed(self, event):
        if event.GetEventObject() is self:
            self._alive = False
            self._cancel_model_list()
            self._cancel_probe()
        event.Skip()


class AISettingsPage(ScrolledPanel):
    def __init__(self, parent, main_window, on_change):
        super().__init__(parent)
        self.main_window = main_window
        self._on_change = on_change
        self.app = getattr(main_window, "app_settings", None) or AppSettings(global_dir())
        self.store = CredentialStore(global_dir())
        self._alive = True
        self._labels = []
        config = preferences(self.app)
        self._order = list(config["order"])
        self._disabled = set(config["disabled"])
        self._models = dict(config["models"])
        self._auto = set(config["auto_models"])
        self._drafts = {}      # provider -> key typed in its window, applied with the dialog
        self._deleted = set()  # providers whose saved key is to be removed
        self._reset = False
        self._saved = self.store.saved()
        self.sizer = wx.BoxSizer(wx.VERTICAL)
        self.enabled = self._check("ai_accessibility_enabled_label", config["enabled"])
        self._label("ai_provider_list_label")
        self.providers = wx.ListBox(self, name=self._t("ai_provider_list_label"))
        self.providers.Bind(wx.EVT_LISTBOX_DCLICK, self._configure)
        self.sizer.Add(self.providers, 0, wx.EXPAND | wx.ALL, 8)
        self.configure_button = self._button("ai_provider_configure_button", self._configure)
        self.up_button = self._button("ai_provider_move_up_button", lambda e: self._move(-1))
        self.down_button = self._button("ai_provider_move_down_button", lambda e: self._move(1))
        self.toggles = {kind: self._check(label, config["kinds"][kind]) for kind, label in KIND_TOGGLES}
        self.profile = self._choice("ai_profile", self._profile_labels())
        self.profile.SetSelection(PROFILES.index(config["profile"]))
        self.read_answers = self._check("ai_read_answers", config["read_answers"])
        self._label("ai_settings_help")
        self.notice = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 155))
        self.sizer.Add(self.notice, 0, wx.EXPAND | wx.ALL, 8)
        self._button("ai_technical_info", self._technical_info)
        self._button("ai_reset_keys", self._reset_keys)
        self._label("status")
        self.status = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 65))
        self.sizer.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(self.sizer)
        self.SetupScrolling(scroll_x=False, rate_y=15, scrollIntoView=True)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._destroyed)
        self.refresh_labels()

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
        self.providers.SetName(self._t("ai_provider_list_label"))
        self.profile.SetName(self._t("ai_profile"))
        selection = self.profile.GetSelection()
        self.profile.SetItems(self._profile_labels())
        self.profile.SetSelection(selection)
        self.notice.SetName(self._t("ai_settings_help"))
        self.notice.ChangeValue(self._t("ai_settings_notice"))
        self.status.SetName(self._t("status"))
        self._refresh_list(self.providers.GetSelection())

    def _has_key(self, provider):
        if provider in self._drafts:
            return True
        return provider in self._saved and provider not in self._deleted and not self._reset

    def _row(self, provider):
        """Item text with the state embedded, in the active language."""
        on = provider not in self._disabled
        return ", ".join((PROVIDERS[provider].name,
                          self._t("ai_provider_state_enabled" if on else "ai_provider_state_disabled"),
                          self._t("ai_key_saved" if self._has_key(provider) else "ai_key_missing")))

    def _refresh_list(self, selection=wx.NOT_FOUND):
        self.providers.Freeze()
        try:
            self.providers.Set([self._row(p) for p in self._order])
            if 0 <= selection < self.providers.GetCount():
                self.providers.SetSelection(selection)
        finally:
            self.providers.Thaw()

    def _selected(self):
        index = self.providers.GetSelection()
        return (index, self._order[index]) if 0 <= index < len(self._order) else (wx.NOT_FOUND, None)

    def _move(self, direction):
        """Swap the selected provider with its neighbour: list order is the
        order they are tried."""
        index, provider = self._selected()
        target = index + direction
        if provider is None or not 0 <= target < len(self._order):
            return
        self._order[index], self._order[target] = self._order[target], self._order[index]
        self._refresh_list(target)
        self._on_change()

    def _configure(self, event):
        index, provider = self._selected()
        if provider is None:
            return
        state = {"key": self._drafts.get(provider, ""), "deleted": provider in self._deleted,
                 "model": "" if provider in self._auto else self._models[provider],
                 "enabled": provider not in self._disabled}
        dialog = AIProviderDialog(self.GetTopLevelParent(), self.main_window, provider, state,
                                  self.store, self._reset)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            draft = dialog.values()
        finally:
            dialog.Destroy()
        if draft["deleted"]:
            self._deleted.add(provider)
            self._drafts.pop(provider, None)
        elif draft["key"]:
            self._deleted.discard(provider)
            self._drafts[provider] = draft["key"]
        if draft["model"]:
            self._auto.discard(provider)
            self._models[provider] = draft["model"]
        else:
            self._auto.add(provider)
            self._models[provider] = PROVIDERS[provider].model
        (self._disabled.discard if draft["enabled"] else self._disabled.add)(provider)
        self._refresh_list(index)
        self._on_change()

    def _reset_keys(self, event):
        if wx.MessageBox(self._t("ai_reset_confirm"), self._t("ai_reset_keys"),
                         wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING, self) != wx.YES:
            return
        self._reset = True
        self._deleted = set(PROVIDERS)
        self._drafts.clear()
        self._refresh_list(self.providers.GetSelection())
        self._on_change()

    def _technical_info(self, event):
        dialog = wx.Dialog(self, title=self._t("ai_technical_info"), size=(560, 400),
                           style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        try:
            layout = wx.BoxSizer(wx.VERTICAL)
            text = wx.TextCtrl(dialog, value=self._t("ai_technical_notice"),
                               style=wx.TE_MULTILINE | wx.TE_READONLY,
                               name=self._t("ai_technical_info"))
            layout.Add(text, 1, wx.EXPAND | wx.ALL, 12)
            close = wx.Button(dialog, wx.ID_CANCEL, label=self._t("close").replace("&", ""))
            layout.Add(close, 0, wx.ALIGN_RIGHT | wx.ALL, 12)
            dialog.SetSizer(layout)
            dialog.SetMinSize((420, 260))
            text.SetFocus()
            dialog.ShowModal()
        finally:
            dialog.Destroy()

    def apply(self):
        """Save the keys and the preferences. False (and the reason in the
        status field) when something is invalid, so Settings stays open."""
        for provider, model in self._models.items():
            if not model.strip():
                self._models[provider] = PROVIDERS[provider].model
        if any(not valid_model(model) for model in self._models.values()):
            self.status.ChangeValue(self._t("ai_error_request"))
            self.providers.SetFocus()
            return False
        try:
            changes = {provider: None for provider in self._deleted}
            changes.update(self._drafts)
            if changes or self._reset:
                self.store.apply(changes, reset=self._reset)
            # Re-entering a key removes it from _deleted, but must not undo
            # the consent revocation requested by an install-wide reset.
            removed = set(PROVIDERS) if self._reset else set(self._deleted)

            def merge(old):
                value = dict(old) if isinstance(old, dict) else {}
                value.update(
                    enabled=self.enabled.GetValue(), order=list(self._order),
                    disabled=sorted(self._disabled),
                    models={name: "" if name in self._auto else model
                            for name, model in self._models.items()},
                    kinds={kind: check.GetValue() for kind, check in self.toggles.items()},
                    profile=PROFILES[self.profile.GetSelection()],
                    read_answers=self.read_answers.GetValue())
                consented = value.get("consented", [])
                value["consented"] = [p for p in consented if p not in removed] \
                    if isinstance(consented, list) else []
                return value
            self.app.update("ai_media", merge)
        except (CredentialError, OSError, RuntimeError):
            self.status.ChangeValue(self._t("ai_error_credentials"))
            return False
        self._drafts.clear()
        self._deleted.clear()
        self._reset = False
        self._saved = self.store.saved()
        self.status.ChangeValue("")
        self._refresh_list(self.providers.GetSelection())
        return True

    def _destroyed(self, event):
        if event.GetEventObject() is self:
            self._alive = False
            self._drafts.clear()
        event.Skip()
