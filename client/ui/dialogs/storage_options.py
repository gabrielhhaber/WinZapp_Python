"""Optional storage preferences, with saving separated from file actions."""
from __future__ import annotations

import json
import time
import wx

from core.storage_manager.forms import imported, number, portable
from core.storage_manager.policy import Preferences
from core.storage_manager.types import CATEGORIES
from .storage_presentation import category_text


class StorageOptionsPanel(wx.ScrolledWindow):
    def __init__(self, parent, owner):
        super().__init__(parent)
        self.owner, self.i18n = owner, owner.i18n
        self._checks, self._numbers = {}, {}
        self.SetScrollRate(0, 16)
        sizer = wx.BoxSizer(wx.VERTICAL)
        note = wx.StaticText(self, label=self.i18n.t("storage_options_note"))
        note.Wrap(650)
        sizer.Add(note, 0, wx.EXPAND | wx.ALL, 8)
        keys = {
            "include_other_accounts": "storage_option_other_accounts",
            "include_external_models": "storage_option_external_models",
            "scan_on_open": "storage_option_scan_open",
            "scan_on_start": "storage_option_scan_start",
            "announce_progress": "storage_option_announce_progress",
            "match_media": "storage_option_match_media",
        }
        for attr, key in keys.items():
            control = wx.CheckBox(self, label=self.i18n.t(key))
            self._checks[attr] = control
            sizer.Add(control, 0, wx.EXPAND | wx.ALL, 5)
        fields = {
            "interval_minutes": "storage_interval_minutes",
            "warning_bytes": "storage_warning_gib",
            "target_bytes": "storage_target_gib",
            "history_days": "storage_history_days",
            "warning_repeat_minutes": "storage_warning_repeat_minutes",
        }
        for attr, key in fields.items():
            label = self.i18n.t(key)
            sizer.Add(wx.StaticText(self, label=label), 0, wx.LEFT | wx.TOP, 6)
            control = wx.TextCtrl(self, name=label.replace("&", ""))
            self._numbers[attr] = control
            sizer.Add(control, 0, wx.EXPAND | wx.ALL, 6)
        sizer.Add(wx.StaticText(self, label=self.i18n.t("storage_target_action")), 0, wx.ALL, 6)
        self.action = wx.Choice(self, choices=[self.i18n.t("storage_notify_only"),
                                               self.i18n.t("storage_pause_downloads")],
                                name=self.i18n.t("storage_target_action").replace("&", ""))
        sizer.Add(self.action, 0, wx.EXPAND | wx.ALL, 6)
        self.target_scope = wx.Choice(self, choices=[self.i18n.t(k) for k in
            ("storage_target_scope_all", "storage_protect_scope_account", "storage_protect_scope_shared")],
            name=self.i18n.t("storage_target_scope"))
        self.target_category = wx.Choice(self, choices=[self.i18n.t("storage_all_categories")]
            + [category_text(c, self.i18n) for c in CATEGORIES], name=self.i18n.t("storage_target_category"))
        for control, key in ((self.target_scope, "storage_target_scope"), (self.target_category, "storage_target_category")):
            sizer.Add(wx.StaticText(self, label=self.i18n.t(key)), 0, wx.ALL, 6)
            sizer.Add(control, 0, wx.EXPAND | wx.ALL, 6)
        buttons = wx.WrapSizer(wx.HORIZONTAL)
        for key, callback in (("storage_save_preferences", self.save),
                              ("storage_reset_preferences", self.reset),
                              ("storage_export_preferences", self.export),
                              ("storage_import_preferences", self.import_)):
            button = wx.Button(self, label=self.i18n.t(key))
            button.Bind(wx.EVT_BUTTON, callback)
            buttons.Add(button, 0, wx.ALL, 4)
        sizer.Add(buttons, 0, wx.ALL, 4)
        matching = wx.Button(self, label=self.i18n.t("storage_choose_matching_chats"))
        matching.Bind(wx.EVT_BUTTON, self.choose_chats)
        sizer.Add(matching, 0, wx.ALL, 6)
        row = wx.WrapSizer(wx.HORIZONTAL)
        for key, callback in (("storage_pause_hour", lambda e: self.pause(3600)),
                              ("storage_pause_day", lambda e: self.pause(86400)),
                              ("storage_resume_schedule", lambda e: self.pause(0)),
                              ("storage_reset_target", self.reset_target)):
            button = wx.Button(self, label=self.i18n.t(key))
            button.Bind(wx.EVT_BUTTON, callback)
            row.Add(button, 0, wx.ALL, 4)
        sizer.Add(row, 0, wx.EXPAND)
        self.SetSizer(sizer)
        self.load(owner.service.preferences)

    def load(self, preferences):
        for attr, control in self._checks.items():
            control.SetValue(getattr(preferences, attr))
        for attr, control in self._numbers.items():
            value = getattr(preferences, attr)
            if attr.endswith("_bytes"):
                value /= 1024**3
            control.ChangeValue(str(value).replace(".", self.i18n.t("decimal_separator")))
        self.action.SetSelection(1 if preferences.target_action == "pause_downloads" else 0)
        self.target_scope.SetSelection(("all", "account", "shared").index(preferences.target_scope))
        self.target_category.SetSelection(0 if preferences.target_category == "all" else CATEGORIES.index(preferences.target_category) + 1)

    def collect(self):
        source = self.owner._staged_import or self.owner.service.preferences
        preferences = Preferences.read(source.as_dict())
        preferences.rules = list(self.owner.rules.rules)
        for attr, control in self._checks.items():
            setattr(preferences, attr, control.GetValue())
        for attr, control in self._numbers.items():
            try:
                size = attr.endswith("_bytes")
                maximum = 10**15 / 1024**3 if size else (3650 if attr == "history_days" else 525600)
                value = number(control.GetValue(), integer=not size, maximum=maximum)
                setattr(preferences, attr, int(value * 1024**3) if size else value)
            except (ValueError, OverflowError):
                control.SetFocus()
                raise
        preferences.target_action = "pause_downloads" if self.action.GetSelection() == 1 else "notify"
        preferences.target_scope = ("all", "account", "shared")[max(0, self.target_scope.GetSelection())]
        preferences.target_category = ("all", *CATEGORIES)[max(0, self.target_category.GetSelection())]
        return preferences

    def pause(self, seconds):
        preferences = Preferences.read(self.owner.service.preferences.as_dict())
        preferences.paused_until = int(time.time() + seconds) if seconds else 0
        self.owner.save_preferences(preferences)

    def choose_chats(self, event=None):
        if not self.owner.available():
            return
        from .storage_matching import StorageMatchingDialog
        try:
            preferences = self.collect()
        except (ValueError, OverflowError):
            self.owner.error("storage_preferences_invalid")
            return
        with StorageMatchingDialog(self, self.owner, preferences) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                preferences.match_chats = dialog.selection()
                self.owner.stage_import(preferences)
                self.owner.say(self.i18n.t("storage_rules_save_note"))

    def reset_target(self, event=None):
        # Edit this group only; the ordinary Save action commits it.
        self._numbers["target_bytes"].ChangeValue("0")
        self.action.SetSelection(0)
        self.target_scope.SetSelection(0)
        self.target_category.SetSelection(0)

    def save(self, event=None):
        try:
            self.owner.save_preferences(self.collect())
        except (OSError, ValueError):
            self.owner.error("storage_preferences_invalid")

    def reset(self, event=None):
        if not self.owner.confirm("storage_reset_confirm"):
            return
        # Reset preferences, not held files or media blocks. No file actions.
        self.owner.reset_preferences()

    def export(self, event=None):
        try:
            raw = portable(self.collect())
            with wx.FileDialog(self, self.i18n.t("storage_export_preferences"),
                               wildcard="JSON (*.json)|*.json", style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as dialog:
                if dialog.ShowModal() != wx.ID_OK:
                    return
                with open(dialog.GetPath(), "w", encoding="utf-8") as stream:
                    json.dump(raw, stream, ensure_ascii=False, indent=2)
        except (OSError, ValueError):
            self.owner.error("storage_preferences_invalid")

    def import_(self, event=None):
        try:
            with wx.FileDialog(self, self.i18n.t("storage_import_preferences"),
                               wildcard="JSON (*.json)|*.json", style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as dialog:
                if dialog.ShowModal() != wx.ID_OK:
                    return
                with open(dialog.GetPath(), encoding="utf-8") as stream:
                    text = stream.read(2_000_001)
                    if len(text) > 2_000_000:
                        raise ValueError("preference template too large")
                    raw = json.loads(text)
            preferences = imported(raw, self.owner.service.preferences)
            self.load(preferences)
            self.owner.stage_import(preferences)
            self.owner.say(self.i18n.t("storage_import_staged"))
        except (OSError, ValueError):
            self.owner.error("storage_preferences_invalid")
