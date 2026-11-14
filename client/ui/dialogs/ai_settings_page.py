"""The "Online AI: Transcriptions and Descriptions" page of Settings, and the window that
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
from core.ai_credentials import (
    CredentialStore, CredentialError, apply_with_translation, store_for_translation,
)
from core.message_translation import credential_id, preferences as translation_preferences
from core.message_translation import settings_from as settings_from_translation
from core import conversation_auto_ai
from core.ai_media import auto as ai_auto
from core.transcription import auto_queue
from core.ai_media import config as ai_config
from core.ai_media.config import PROFILES, PROVIDERS, preferences, supports, valid_model
from core.ai_media.errors import DescriptionError
from core.ai_media.service import RequestToken, probe_connection, submit
from .ai_provider_models import ModelSelectionMixin
from .ai_auto_options import AIAutoOptionsPanel, window_title
from .ai_kinds_options import AIKindsPanel
from .settings_pages import SubPageDialog

#: i18n key naming each kind, for the "handles" line of the provider window.
KIND_NAMES = (("image", "ai_kind_image"), ("sticker", "ai_kind_sticker"), ("video", "ai_kind_video"),
              ("audio", "ai_kind_audio"), ("pdf", "ai_kind_pdf"))


#: Captions of the manual model fields, hidden while "automatic" is on.
MANUAL_MODEL_CAPTIONS = ("ai_model_choice", "ai_model")


def key_for_dialog(store, provider, drafts, deleted, reset):
    """The key the provider window opens with: the one staged in this Settings
    session, else the saved one, so the field shows what is configured. Blank
    once it is staged for removal or reset, or when the store cannot be read
    (the window then reports the unreadable store itself)."""
    if provider in drafts:
        return drafts[provider]
    if provider in deleted or reset:
        return ""
    try:
        return store.get(provider) or ""
    except CredentialError:
        return ""


def voice_model_labels(t, provider):
    """The items of the voice-message model list, in the catalogue's order. A
    Public Preview model says so: it may change or be withdrawn unannounced."""
    return [t("ai_model_preview").format(name=choice.name) if choice.preview else choice.name
            for choice in PROVIDERS[provider].transcribe_choices]


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
        self.model_note = None
        if spec.model_note:
            # Read-only field, not a StaticText: Tab skips a StaticText, so a
            # screen reader user moving to the model field would never hear it.
            self.model_note = wx.TextCtrl(body, value=self._t(spec.model_note),
                                          style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 50))
            self.sizer.Add(self.model_note, 0, wx.EXPAND | wx.ALL, 8)
        self.voice_model = None
        if spec.transcribe_choices:
            self.voice_model = self._choice(body, "ai_voice_model",
                                            voice_model_labels(self._t, provider))
            ids = [choice.id for choice in spec.transcribe_choices]
            self.voice_model.SetSelection(ids.index(ai_config.transcribe_model(
                provider, state.get("transcribe_model"))))
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
        self._caption(body, "ai_status_label")
        self.status = wx.TextCtrl(body, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 65),
                                  name=self._plain("ai_status_label"))
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
        self._key_status()
        self.enabled.SetFocus()

    def _t(self, key):
        return self.main_window.i18n.t(key)

    def _plain(self, key):
        """A label without its mnemonic marker, for a control's name."""
        return self._t(key).replace("&", "")

    def _caption(self, body, key):
        label = wx.StaticText(body, label=self._t(key))
        self._labels.append((label, key))
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

    def _apply_automatic(self):
        """Automatic follows the model WinZapp recommends: a pinned model is
        edited only while it is off."""
        pinned = self._model_pinned()
        if not pinned:
            self.model.ChangeValue(PROVIDERS[self._provider].model)
        self.get_models.Enable(pinned)
        self.model_choice.Enable(pinned and bool(self._model_options))
        self._show_manual_model_fields(pinned)

    def _show_manual_model_fields(self, shown):
        """The manual model fields exist only while "automatic" is off. A
        disabled edit field is announced as unavailable or read-only by screen
        readers, which misdescribes a field that is editable once unlocked."""
        for control in (self.model_choice, self.model, self.get_models):
            control.Show(shown)
        for label, key in self._labels:
            if key in MANUAL_MODEL_CAPTIONS:
                label.Show(shown)
        body = getattr(self, "_body", None)
        if body is not None:
            body.Layout()
            body.FitInside()

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
        the saved one is to be removed, the model, the voice-message model (for
        a provider that offers a choice) and the on/off switch."""
        values = {"key": self.key.GetValue().strip(), "deleted": self._deleted,
                  "model": "" if self.automatic.GetValue() else self.model.GetValue().strip(),
                  "enabled": self.enabled.GetValue()}
        if self.voice_model is not None:
            choices = PROVIDERS[self._provider].transcribe_choices
            values["transcribe_model"] = choices[max(self.voice_model.GetSelection(), 0)].id
        return values

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
        self.app = getattr(main_window, "_app_settings", None) or AppSettings(global_dir())
        self.store = CredentialStore(global_dir())
        self._alive = True
        self._labels = []
        config = preferences(self.app)
        self._order = list(config["order"])
        self._disabled = set(config["disabled"])
        self._models = dict(config["models"])
        self._auto = set(config["auto_models"])
        self._voice_models = dict(config["transcribe_models"])
        self._drafts = {}      # provider -> key typed in its window, applied with the dialog
        self._deleted = set()  # providers whose saved key is to be removed
        self._reset = False
        self._translation = None
        self._saved = self.store.saved()
        self.sizer = wx.BoxSizer(wx.VERTICAL)
        self.enabled = self._check("ai_accessibility_enabled_label", config["enabled"])
        self._label("ai_provider_list_label")
        self.providers = wx.ListBox(self, name=self._plain("ai_provider_list_label"))
        self.providers.Bind(wx.EVT_LISTBOX_DCLICK, self._configure)
        self.sizer.Add(self.providers, 0, wx.EXPAND | wx.ALL, 8)
        self.configure_button = self._button("ai_provider_configure_button", self._configure)
        self.up_button = self._button("ai_provider_move_up_button", lambda e: self._move(-1))
        self.down_button = self._button("ai_provider_move_down_button", lambda e: self._move(1))
        # The kinds of media, in a window of their own (ai_kinds_options.py):
        # on the page they left no letter for the translation button.
        self.kinds_button = self._button("ai_kinds_button", lambda event: self.open_kinds())
        self.kinds_dialog = SubPageDialog(
            self.GetTopLevelParent(), window_title(self._t("ai_kinds_button")), self._t("ok"),
            on_change=lambda event: self._on_change())
        self.kinds_panel = AIKindsPanel(self.kinds_dialog, self, config["kinds"])
        self.kinds_dialog.set_body(self.kinds_panel)
        self.toggles = self.kinds_panel.toggles
        self.profile = self._choice("ai_profile", self._profile_labels())
        self.profile.SetSelection(PROFILES.index(config["profile"]))
        self.read_answers = self._check("ai_read_answers", config["read_answers"])
        self.hide_action_button = self._check("ai_hide_action_button", config["hide_action_button"])
        self._label("ai_summary_min_unread_label")
        self.summary_min_unread = wx.SpinCtrl(
            self, min=0, max=10000, initial=config["summary_min_unread"],
            name=self._plain("ai_summary_min_unread_label"))
        self.summary_min_unread.Bind(
            wx.EVT_SPINCTRL, lambda event: (self._on_change(), event.Skip()))
        self.sizer.Add(self.summary_min_unread, 0, wx.ALL, 8)
        # The automatic options, in a window of their own (ai_auto_options.py).
        # Its parent is the settings dialog, not this page: _show_options()
        # shows and hides every child of the page.
        self.auto_button = self._button("ai_auto_button", lambda event: self.open_auto())
        self.auto_dialog = SubPageDialog(
            self.GetTopLevelParent(), window_title(self._t("ai_auto_button")), self._t("ok"),
            on_change=lambda event: self._on_change())
        self.auto_panel = AIAutoOptionsPanel(self.auto_dialog, self, config)
        self.auto_dialog.set_body(self.auto_panel)
        self.auto_transcribe = self.auto_panel.transcribe
        self.auto_describe = self.auto_panel.describe
        self.auto_scope = self.auto_panel.scope
        self.auto_max_age = self.auto_panel.max_age
        self.auto_limit = self.auto_panel.limit
        self.auto_used = self.auto_panel.used
        self.auto_transcribe.Bind(wx.EVT_CHECKBOX, self._on_auto_toggle)
        self.auto_describe.Bind(wx.EVT_CHECKBOX, self._on_auto_toggle)
        self.auto_limit.Bind(wx.EVT_SPINCTRL, lambda event: (self._on_change(), event.Skip()))
        #: Set by SettingsDialog: asks before this replaces the local
        #: automatic transcription; True when it may go ahead.
        self.confirm_replaces_local = None
        self._auto_consent = set()  # providers consented to by turning an option on
        self._label("ai_settings_help")
        self.notice = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 155))
        self.sizer.Add(self.notice, 0, wx.EXPAND | wx.ALL, 8)
        self._button("ai_technical_info", self._technical_info)
        self.translation_button = self._button("translation_settings", self._configure_translation)
        self.reset_keys_button = self._button("ai_reset_keys", self._reset_keys)
        self._label("ai_status_label")
        self.status = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 65))
        self.sizer.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(self.sizer)
        self.SetupScrolling(scroll_x=False, rate_y=15, scrollIntoView=True)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._destroyed)
        self.refresh_labels()
        # Translation has its own preferences and works independently of
        # transcription and description.
        self.enabled.Bind(wx.EVT_CHECKBOX, self._on_enabled_toggle)
        self._show_options(self.enabled.GetValue())

    def open_kinds(self, control=None):
        """The window of the kinds of media, on *control* when given."""
        self.kinds_dialog.open(control)

    def open_auto(self, control=None):
        """The window of the automatic options, on *control* when given."""
        self.auto_dialog.open(control)

    def _show_options(self, shown):
        for child in self.GetChildren():
            if child not in (self.enabled, self.translation_button, self.reset_keys_button):
                child.Show(shown)
        # The automatic options' window too: the settings search reads its
        # controls (add_subpage), and must not offer them with AI turned off.
        for panel in (self.auto_panel, self.kinds_panel):
            for child in panel.GetChildren():
                child.Show(shown)
            panel.Layout()
        self.Layout()
        self.SetupScrolling(scroll_x=False, rate_y=15, scrollIntoView=True)

    def _on_enabled_toggle(self, event):
        self._show_options(self.enabled.GetValue())
        event.Skip()

    def DoGetBestSize(self):
        # The page has many occasional-use actions. Its virtual content may
        # be tall; it must not make SettingsDialog.Fit() exceed the screen.
        return wx.Size(420, 450)

    def _t(self, key):
        return self.main_window.i18n.t(key)

    def _plain(self, key):
        """A label without its mnemonic marker, for a control's name."""
        return self._t(key).replace("&", "")

    def _label(self, key):
        label = wx.StaticText(self, label=self._t(key))
        self._labels.append((label, key))
        self.sizer.Add(label, 0, wx.LEFT | wx.TOP, 8)

    def _choice(self, key, choices):
        self._label(key)
        control = wx.Choice(self, choices=choices, name=self._plain(key))
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
        self.providers.SetName(self._plain("ai_provider_list_label"))
        self.profile.SetName(self._plain("ai_profile"))
        selection = self.profile.GetSelection()
        self.profile.SetItems(self._profile_labels())
        self.profile.SetSelection(selection)
        self.auto_panel.refresh_labels()
        self.kinds_panel.refresh_labels()
        self.kinds_dialog.SetTitle(window_title(self._t("ai_kinds_button")))
        self.auto_dialog.SetTitle(window_title(self._t("ai_auto_button")))
        self.notice.SetName(self._plain("ai_settings_help"))
        self.notice.ChangeValue(self._t("ai_settings_notice"))
        self.status.SetName(self._plain("ai_status_label"))
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
        """Rebuild the rows. Without a valid selection the first provider is
        selected, so the list lands on it when focused instead of on nothing."""
        self.providers.Freeze()
        try:
            self.providers.Set([self._row(p) for p in self._order])
            count = self.providers.GetCount()
            if not 0 <= selection < count:
                selection = 0
            if count:
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
        state = {"key": key_for_dialog(self.store, provider, self._drafts, self._deleted, self._reset),
                 "deleted": provider in self._deleted,
                 "model": "" if provider in self._auto else self._models[provider],
                 "transcribe_model": self._voice_models.get(provider, ""),
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
        elif draft["key"] and draft["key"] != state["key"]:
            # Only a key that differs from the one the window opened with is
            # staged; an unchanged prefill needs no re-save.
            self._deleted.discard(provider)
            self._drafts[provider] = draft["key"]
        if draft["model"]:
            self._auto.discard(provider)
            self._models[provider] = draft["model"]
        else:
            self._auto.add(provider)
            self._models[provider] = PROVIDERS[provider].model
        if draft.get("transcribe_model"):
            self._voice_models[provider] = draft["transcribe_model"]
        (self._disabled.discard if draft["enabled"] else self._disabled.add)(provider)
        self._refresh_list(index)
        self._on_change()

    def _configure_translation(self, event):
        from ui.dialogs.translation_settings import TranslationSettingsDialog

        dialog = TranslationSettingsDialog(
            self.GetTopLevelParent(), self.main_window.i18n,
            self._translation or translation_preferences(self.app),
            lambda provider: key_for_dialog(
                store_for_translation(self.store, provider), provider,
                self._drafts, self._deleted, self._reset),
            window_title(self._t("translation_settings")))
        try:
            if dialog.ShowModal() == wx.ID_OK:
                self._translation, key = dialog.result
                if key:
                    provider = credential_id(self._translation)
                    self._drafts[provider] = key
                    self._deleted.discard(provider)
                self._on_change()
        finally:
            dialog.Destroy()

    def _reset_keys(self, event):
        if wx.MessageBox(self._t("ai_reset_confirm"), self._t("ai_reset_keys"),
                         wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING, self) != wx.YES:
            return
        self._reset = True
        self._deleted = set(PROVIDERS)
        self._drafts.clear()
        self._refresh_list(self.providers.GetSelection())
        self._on_change()

    def _age_labels(self):
        labels = []
        for minutes in auto_queue.AGE_CHOICES_MINUTES:
            if minutes == 60:
                labels.append(self._t("ai_auto_age_hour"))
            else:
                labels.append(self._t("ai_auto_age_minutes").format(minutes=minutes))
        return labels

    def _scope_labels(self):
        return [self._t("ai_auto_scope_all"), self._t("ai_auto_scope_selected")]

    def _on_auto_toggle(self, event):
        """Turning an automatic option on: replace the local automatic
        transcription (asked first) and consent to the providers that will
        receive every message of the kind. Turning it off asks nothing."""
        control = event.GetEventObject()
        if control.GetValue():
            if (control is self.auto_transcribe and callable(self.confirm_replaces_local)
                    and not self.confirm_replaces_local()):
                control.SetValue(False)
                return
            kinds = ("audio",) if control is self.auto_transcribe else ("image", "sticker", "video")
            order = {"order": self._order, "disabled": sorted(self._disabled)}
            providers = []
            for kind in kinds:
                for provider in ai_config.chain(order, kind, self._has_key):
                    if provider not in providers:
                        providers.append(provider)
            consented = set(preferences(self.app)["consented"]) | self._auto_consent
            needed = [p for p in providers if p not in consented]
            if needed:
                text = self._t("ai_auto_consent").format(
                    providers=", ".join(PROVIDERS[p].name for p in needed))
                # The same provider notices as the consent before one request.
                text = "\n\n".join([text, *(self._t(key) for key in ai_config.notice_keys(needed))])
                answer = wx.MessageBox(text, self._plain("ai_auto_consent_title"),
                                       wx.YES_NO | wx.ICON_WARNING, self.auto_dialog)
                if answer != wx.YES:
                    control.SetValue(False)
                    return
                self._auto_consent.update(needed)
        self._on_change()
        event.Skip()

    def _technical_info(self, event):
        dialog = wx.Dialog(self, title=self._t("ai_technical_info").replace("&", ""), size=(560, 400),
                           style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        try:
            layout = wx.BoxSizer(wx.VERTICAL)
            text = wx.TextCtrl(dialog, value=self._t("ai_technical_notice"),
                               style=wx.TE_MULTILINE | wx.TE_READONLY,
                               name=self._t("ai_technical_info").replace("&", ""))
            layout.Add(text, 1, wx.EXPAND | wx.ALL, 12)
            close = wx.Button(dialog, wx.ID_CANCEL, label=self._t("close"))
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
            apply_with_translation(self.store, changes, reset=self._reset)
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
                    # Only a choice that differs from the default: like an
                    # automatic vision model, the default may move later.
                    transcribe_models={name: model for name, model in self._voice_models.items()
                                       if model != PROVIDERS[name].transcribe_model},
                    kinds={kind: check.GetValue() for kind, check in self.toggles.items()},
                    profile=PROFILES[self.profile.GetSelection()],
                    read_answers=self.read_answers.GetValue(),
                    hide_action_button=self.hide_action_button.GetValue(),
                    summary_min_unread=int(self.summary_min_unread.GetValue()),
                    auto_transcribe=self.auto_transcribe.GetValue(),
                    auto_describe=self.auto_describe.GetValue(),
                    auto_daily_limit=int(self.auto_limit.GetValue()))
                consented = value.get("consented", [])
                if self.auto_transcribe.GetValue() or self.auto_describe.GetValue():
                    value["consented"] = sorted(
                        (set(consented) if isinstance(consented, list) else set())
                        | self._auto_consent)
                    consented = value["consented"]
                value["consented"] = [p for p in consented if p not in removed] \
                    if isinstance(consented, list) else []
                return value
            self.app.update("ai_media", merge)
            if self._translation is not None or self._reset:
                def merge_translation(old):
                    updated = dict(old) if isinstance(old, dict) else {}
                    if self._translation is not None:
                        updated.update(self._translation.saved())
                    if self._reset:
                        updated.update(automatic=False, all_accounts_confirmed=False)
                    return updated

                saved_translation = self.app.update("message_translation", merge_translation)
                translation = settings_from_translation(saved_translation)
                panel = getattr(self.main_window, "conversations_panel", None)
                update_view = getattr(panel, "_set_translation_view", None)
                if callable(update_view):
                    update_view(translation)
                    wx.CallAfter(panel._rerender_messages_list_rows)
                focused = getattr(panel, "_translate_focused_recent", None)
                if translation.automatic and callable(focused):
                    wx.CallAfter(focused)
            # Any apply may have fixed what paused automatic translation: a
            # key re-entered in a provider's window is the usual one.
            resume = getattr(getattr(self.main_window, "conversations_panel", None),
                             "resume_automatic_translation", None)
            if callable(resume):
                resume()
            self.main_window.settings[conversation_auto_ai.SCOPE_SETTING] = (
                conversation_auto_ai.SCOPE_SELECTED if self.auto_scope.GetSelection() == 1
                else conversation_auto_ai.SCOPE_ALL)
            index = max(self.auto_max_age.GetSelection(), 0)
            self.main_window.settings.setdefault("transcription", {})[
                auto_queue.SETTING_MAX_AGE_MINUTES] = auto_queue.AGE_CHOICES_MINUTES[index]
            resume = getattr(self.main_window, "resume_auto_ai_media", None)
            if callable(resume):
                resume()
        except (CredentialError, OSError, RuntimeError):
            self.status.ChangeValue(self._t("ai_error_credentials"))
            return False
        self._drafts.clear()
        self._deleted.clear()
        self._reset = False
        self._translation = None
        self._saved = self.store.saved()
        self.status.ChangeValue("")
        self._refresh_list(self.providers.GetSelection())
        return True

    def _destroyed(self, event):
        if event.GetEventObject() is self:
            self._alive = False
            self._drafts.clear()
        event.Skip()
