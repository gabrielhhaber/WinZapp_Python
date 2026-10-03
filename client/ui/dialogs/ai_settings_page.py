"""The "Transcriptions and Descriptions" page of Settings (AI accessibility).

Lives in its own module so settings_dialog.py only has to build the page, load
it, read it back and relabel it. The page owns everything specific to this
feature: the ordered list of providers, the per-provider key/model window and
the confirmation asked when the feature is turned on.

Design notes that are easy to undo by accident:

* The provider list is a plain ListBox whose item text embeds the state
  ("Gemini, On" / "Gemini, Off"), not a CheckListBox: NVDA does not reliably
  announce the check state of a CheckListBox item. The position in the list IS
  the order in which core/ai_providers.py tries the providers.
* API key fields are plain, unmasked text controls on purpose. Masking guards
  against shoulder-surfing, which matters less here than a blind user being
  able to have the screen reader read the key back to confirm it was typed or
  pasted correctly.
* Turning the feature on asks first, because it sends the person's chat media
  (voice messages, photos, videos, PDFs) to third-party services. It starts
  switched off and the answer only ever moves it from off to on.
"""

import wx

from core.combo_search import bind_incremental_search
from core.claude_client import RECOMMENDED_MODELS as CLAUDE_RECOMMENDED_MODELS
from core.gemini_client import RECOMMENDED_MODELS as GEMINI_RECOMMENDED_MODELS
from core.groq_client import RECOMMENDED_MODELS as GROQ_RECOMMENDED_MODELS
from core.openai_client import RECOMMENDED_MODELS as OPENAI_RECOMMENDED_MODELS
from core.openrouter_client import RECOMMENDED_MODELS as OPENROUTER_RECOMMENDED_MODELS
from core import ai_providers

#: (provider id, display name, recommended models). The id is also the prefix
#: of the provider's settings keys and of its i18n keys.
AI_PROVIDER_UI = (
    ("gemini", "Gemini", GEMINI_RECOMMENDED_MODELS),
    ("openai", "OpenAI", OPENAI_RECOMMENDED_MODELS),
    ("claude", "Claude", CLAUDE_RECOMMENDED_MODELS),
    ("groq", "Groq", GROQ_RECOMMENDED_MODELS),
    ("openrouter", "OpenRouter", OPENROUTER_RECOMMENDED_MODELS),
)

#: The per-kind switches, (settings key, i18n key of its label).
_TOGGLES = (
    ("transcribe_audio", "ai_transcribe_audio_label"),
    ("describe_images", "ai_describe_images_label"),
    ("describe_videos", "ai_describe_videos_label"),
    ("transcribe_stickers", "ai_transcribe_stickers_label"),
    ("pdf_to_accessible_text", "ai_pdf_accessible_label"),
)


def model_choices(models, saved_value):
    """(labels-free values, index to select) for a provider's model combo.

    The first value, "", is "Automatic": it follows the current recommended
    model in core/<provider>_client.py, so someone who never reopens the combo
    keeps working when a model is retired. A model saved by a newer version or
    typed into settings.json is kept and selected instead of being silently
    dropped the next time Apply is pressed.
    """
    values = ["", *models]
    value = (saved_value or "").strip()
    if value not in values:
        values.append(value)
    return values, values.index(value)


class AIProviderDialog(wx.Dialog):
    """Key and model of one provider, plus whether it takes part in the
    automatic fallback."""

    def __init__(self, parent, i18n, provider_id, name, models, state, enabled):
        super().__init__(
            parent, title=i18n.t("ai_provider_button").format(provider=name)
        )
        self._values, selection = model_choices(models, state.get("model", ""))

        panel = wx.Panel(self)
        box = wx.BoxSizer(wx.VERTICAL)
        # A real CheckBox (not a list-item check): NVDA announces its state on
        # focus reliably.
        self._enabled_check = wx.CheckBox(
            panel, label=i18n.t("ai_provider_enabled_checkbox")
        )
        self._enabled_check.SetValue(enabled)
        box.Add(self._enabled_check, 0, wx.ALL, 8)

        box.Add(
            wx.StaticText(panel, label=i18n.t(f"{provider_id}_api_key_label")),
            0, wx.LEFT | wx.TOP | wx.RIGHT, 8,
        )
        self._key_field = wx.TextCtrl(panel, style=wx.TE_DONTWRAP)
        self._key_field.ChangeValue(state.get("key", ""))
        box.Add(self._key_field, 0, wx.EXPAND | wx.ALL, 8)
        box.Add(
            wx.StaticText(panel, label=i18n.t(f"{provider_id}_api_key_help_label")),
            0, wx.LEFT | wx.BOTTOM | wx.RIGHT, 8,
        )

        box.Add(
            wx.StaticText(panel, label=i18n.t(f"{provider_id}_model_label")),
            0, wx.LEFT | wx.TOP | wx.RIGHT, 8,
        )
        self._model_combo = wx.ComboBox(
            panel, style=wx.CB_READONLY,
            choices=[i18n.t("gemini_model_automatic_option"), *self._values[1:]],
        )
        self._model_combo.SetSelection(selection)
        bind_incremental_search(self._model_combo)
        box.Add(self._model_combo, 0, wx.EXPAND | wx.ALL, 8)
        box.Add(
            wx.StaticText(panel, label=i18n.t(f"{provider_id}_model_help_label")),
            0, wx.LEFT | wx.BOTTOM | wx.RIGHT, 8,
        )
        panel.SetSizer(box)

        buttons = wx.StdDialogButtonSizer()
        ok = wx.Button(self, wx.ID_OK, label=i18n.t("ok"))
        cancel = wx.Button(self, wx.ID_CANCEL, label=i18n.t("cancel"))
        buttons.AddButton(ok)
        buttons.AddButton(cancel)
        buttons.Realize()
        ok.SetDefault()

        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        outer.Add(buttons, 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizerAndFit(outer)
        self._key_field.SetFocus()

    def values(self):
        """(state dict, enabled) as currently entered."""
        selection = self._model_combo.GetSelection()
        model = self._values[selection] if 0 <= selection < len(self._values) else ""
        return (
            {"key": self._key_field.GetValue().strip(), "model": model},
            self._enabled_check.GetValue(),
        )


class AISettingsPage(wx.Panel):
    """The page itself. ``on_change`` is called when something changed that
    no checkbox/text event reports (reordering, the provider window)."""

    def __init__(self, parent, i18n, on_change):
        super().__init__(parent)
        self._i18n = i18n
        self._on_change = on_change
        self._state = {pid: {"key": "", "model": ""} for pid, _n, _m in AI_PROVIDER_UI}
        self._enabled = {pid: True for pid, _n, _m in AI_PROVIDER_UI}
        self._order = [pid for pid, _n, _m in AI_PROVIDER_UI]
        self._names = {pid: name for pid, name, _m in AI_PROVIDER_UI}
        self._models = {pid: models for pid, _n, models in AI_PROVIDER_UI}

        sizer = wx.BoxSizer(wx.VERTICAL)
        self._enabled_check = wx.CheckBox(
            self, label=i18n.t("ai_accessibility_enabled_label")
        )
        sizer.Add(self._enabled_check, 0, wx.ALL, 8)

        self._provider_list = wx.ListBox(self, choices=[])
        self._provider_list.SetName(i18n.t("ai_provider_list_label"))
        self._provider_list.Bind(wx.EVT_LISTBOX_DCLICK, self._on_configure)
        sizer.Add(self._provider_list, 0, wx.EXPAND | wx.ALL, 8)

        row = wx.BoxSizer(wx.HORIZONTAL)
        self._configure_button = wx.Button(
            self, label=i18n.t("ai_provider_configure_button")
        )
        self._configure_button.Bind(wx.EVT_BUTTON, self._on_configure)
        row.Add(self._configure_button, 0, wx.RIGHT, 8)
        self._up_button = wx.Button(self, label=i18n.t("ai_provider_move_up_button"))
        self._up_button.Bind(wx.EVT_BUTTON, lambda e: self._move(-1))
        row.Add(self._up_button, 0, wx.RIGHT, 8)
        self._down_button = wx.Button(self, label=i18n.t("ai_provider_move_down_button"))
        self._down_button.Bind(wx.EVT_BUTTON, lambda e: self._move(1))
        row.Add(self._down_button, 0)
        sizer.Add(row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self._provider_controls = [
            self._provider_list, self._configure_button,
            self._up_button, self._down_button,
        ]

        sizer.Add(wx.StaticLine(self), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)
        self._toggles = {}
        for setting_key, label_key in _TOGGLES:
            check = wx.CheckBox(self, label=i18n.t(label_key))
            sizer.Add(check, 0, wx.ALL, 8)
            self._toggles[setting_key] = check
        self.SetSizer(sizer)

        self._enabled_check.Bind(wx.EVT_CHECKBOX, self._on_enabled_toggle)
        self._refresh_list()
        self._update_enabled_state()

    # ── values ──────────────────────────────────────────────────────────────

    def load(self, ai_settings):
        """Populate the controls from the ai_accessibility settings."""
        ai_settings = ai_settings if isinstance(ai_settings, dict) else {}
        self._enabled_check.SetValue(bool(ai_settings.get("enabled", False)))
        for pid in self._state:
            self._state[pid] = {
                "key": (ai_settings.get(f"{pid}_api_key") or "").strip(),
                "model": (ai_settings.get(f"{pid}_model") or "").strip(),
            }
            self._enabled[pid] = ai_providers.is_provider_enabled(ai_settings, pid)
        self._order = ai_providers.provider_order(ai_settings)
        for setting_key, check in self._toggles.items():
            check.SetValue(bool(ai_settings.get(setting_key, True)))
        self._refresh_list()
        self._update_enabled_state()

    def collect(self):
        """The settings this page owns, ready to merge into ai_accessibility."""
        values = {
            "enabled": self._enabled_check.GetValue(),
            "provider_order": list(self._order),
        }
        for pid, state in self._state.items():
            values[f"{pid}_api_key"] = state["key"]
            values[f"{pid}_model"] = state["model"]
            values[f"{pid}_enabled"] = self._enabled.get(pid, True)
        for setting_key, check in self._toggles.items():
            values[setting_key] = check.GetValue()
        return values

    def refresh_labels(self, i18n):
        """Relabel after a language change."""
        self._i18n = i18n
        self._enabled_check.SetLabel(i18n.t("ai_accessibility_enabled_label"))
        self._provider_list.SetName(i18n.t("ai_provider_list_label"))
        self._configure_button.SetLabel(i18n.t("ai_provider_configure_button"))
        self._up_button.SetLabel(i18n.t("ai_provider_move_up_button"))
        self._down_button.SetLabel(i18n.t("ai_provider_move_down_button"))
        for setting_key, label_key in _TOGGLES:
            self._toggles[setting_key].SetLabel(i18n.t(label_key))
        self._refresh_list(selection=self._provider_list.GetSelection())
        self.Layout()

    # ── behaviour ───────────────────────────────────────────────────────────

    def _on_enabled_toggle(self, event):
        """Turning the feature ON asks first: it sends media from the person's
        chats to third-party services. Declining puts the box back and stops the
        event, so Settings is not marked as changed for nothing."""
        if self._enabled_check.GetValue():
            answer = wx.MessageBox(
                self._i18n.t("ai_privacy_notice"),
                self._i18n.t("ai_privacy_notice_title"),
                wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
                self,
            )
            if answer != wx.YES:
                self._enabled_check.SetValue(False)
                return
        self._update_enabled_state()
        event.Skip()

    def _update_enabled_state(self):
        """Provider list and per-kind switches only matter while the feature is
        on; off, they leave the Tab order."""
        on = self._enabled_check.GetValue()
        for control in self._provider_controls:
            control.Enable(on)
        for check in self._toggles.values():
            check.Enable(on)

    def _list_label(self, pid):
        """Item text with the state embedded, in the active language."""
        state = self._i18n.t(
            "ai_provider_state_enabled" if self._enabled.get(pid, True)
            else "ai_provider_state_disabled"
        )
        return f"{self._names[pid]}, {state}"

    def _refresh_list(self, selection=None):
        self._provider_list.Set([self._list_label(pid) for pid in self._order])
        if selection is not None and 0 <= selection < self._provider_list.GetCount():
            self._provider_list.SetSelection(selection)

    def _on_configure(self, event):
        index = self._provider_list.GetSelection()
        if index == wx.NOT_FOUND:
            return
        pid = self._order[index]
        dialog = AIProviderDialog(
            self.GetTopLevelParent(), self._i18n, pid, self._names[pid],
            self._models[pid], self._state[pid], self._enabled.get(pid, True),
        )
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            new_state, now_enabled = dialog.values()
        finally:
            dialog.Destroy()
        changed = new_state != self._state[pid] or now_enabled != self._enabled.get(pid, True)
        self._state[pid] = new_state
        self._enabled[pid] = now_enabled
        self._refresh_list(selection=index)
        if changed:
            self._on_change()

    def _move(self, direction):
        """Swap the selected provider with its neighbour: list order is the
        automatic try order."""
        index = self._provider_list.GetSelection()
        if index == wx.NOT_FOUND:
            return
        new_index = index + direction
        if not 0 <= new_index < len(self._order):
            return
        self._order[index], self._order[new_index] = (
            self._order[new_index], self._order[index],
        )
        self._refresh_list(selection=new_index)
        self._on_change()
