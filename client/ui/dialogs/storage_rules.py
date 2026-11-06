"""Native, explicit rule editor: drafts never consent to automatic deletion."""
from dataclasses import replace

import wx

from core.storage_manager.forms import number
from core.storage_manager.policy import Rule
from .storage_presentation import category_text, size_text


CATEGORIES = ("media", "voice", "compile_cache")
MODES = ("suggest", "notify", "automatic")
MODE_KEYS = ("storage_rule_suggest", "storage_rule_notify", "storage_rule_automatic")
MEDIA_TYPES = ("", "imageMessage", "videoMessage", "documentMessage", "audioMessage", "voiceMessage")
TYPE_KEYS = ("storage_all_types", "cleanup_cat_photos", "cleanup_cat_videos",
             "cleanup_cat_documents", "cleanup_cat_audios", "cleanup_cat_voice_messages")


class RuleEditor(wx.Dialog):
    def __init__(self, parent, i18n, rule=None):
        super().__init__(parent, title=i18n.t("storage_rule_editor"),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.i18n, self.result = i18n, None
        page = wx.ScrolledWindow(self)
        page.SetScrollRate(0, 16)
        sizer = wx.BoxSizer(wx.VERTICAL)
        fields = (("category", "storage_category", [category_text(c, i18n) for c in CATEGORIES]),
                  ("media_type", "storage_media_type", [i18n.t(k) for k in TYPE_KEYS]),
                  ("mode", "storage_rule_mode", [i18n.t(k) for k in MODE_KEYS]),
                  ("combine", "storage_rule_combine", [i18n.t("storage_match_all"), i18n.t("storage_match_any")]))
        for attr, key, choices in fields:
            label = i18n.t(key)
            sizer.Add(wx.StaticText(page, label=label), 0, wx.ALL, 5)
            control = wx.Choice(page, choices=choices, name=label.replace("&", ""))
            control.SetSelection(0)
            setattr(self, attr, control)
            sizer.Add(control, 0, wx.EXPAND | wx.ALL, 5)
        for attr, key in (("days", "storage_rule_days"), ("minimum", "storage_rule_minimum_mib")):
            label = i18n.t(key)
            sizer.Add(wx.StaticText(page, label=label), 0, wx.ALL, 5)
            control = wx.TextCtrl(page, value="0", name=label.replace("&", ""))
            setattr(self, attr, control)
            sizer.Add(control, 0, wx.EXPAND | wx.ALL, 5)
        self.enabled = wx.CheckBox(page, label=i18n.t("storage_rule_enabled"))
        sizer.Add(self.enabled, 0, wx.ALL, 6)
        note = wx.StaticText(page, label=i18n.t("storage_rules_note"))
        note.Wrap(500)
        sizer.Add(note, 0, wx.EXPAND | wx.ALL, 6)
        page.SetSizer(sizer)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(page, 1, wx.EXPAND | wx.ALL, 5)
        outer.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(outer)
        self.SetMinSize((360, 320))
        self.SetSize((580, 650))
        self.Bind(wx.EVT_BUTTON, self.accept, id=wx.ID_OK)
        self.category.Bind(wx.EVT_CHOICE, self.changed)
        self.mode.Bind(wx.EVT_CHOICE, self.changed)
        if rule is not None:
            self.category.SetSelection(CATEGORIES.index(rule.category))
            self.changed()
            self.media_type.SetSelection(self._types.index(rule.media_type) if rule.media_type in self._types else 0)
            self.mode.SetSelection(MODES.index(rule.mode))
            self.combine.SetSelection(0 if rule.combine == "all" else 1)
            self.days.ChangeValue(str(rule.days))
            self.minimum.ChangeValue(str(rule.minimum / 1024**2))
            self.enabled.SetValue(rule.enabled)
        else:
            self.changed()

    def changed(self, event=None):
        category = CATEGORIES[self.category.GetSelection()]
        previous_mode = max(0, self.mode.GetSelection())
        available = MODES if category == "compile_cache" else MODES[:2]
        self.mode.Set([self.i18n.t(MODE_KEYS[MODES.index(mode)]) for mode in available])
        self.mode.SetSelection(min(previous_mode, len(available) - 1))
        kinds = {"media": ("", "imageMessage", "videoMessage", "documentMessage"),
                 "voice": ("", "audioMessage", "voiceMessage"), "compile_cache": ("",)}
        types = kinds[category]
        if getattr(self, "_types", None) != types:
            self._types = types
            self.media_type.Set([self.i18n.t(TYPE_KEYS[MEDIA_TYPES.index(kind)]) for kind in types])
            self.media_type.SetSelection(0)
        self.media_type.Enable(len(types) > 1)

    def accept(self, event):
        try:
            category = CATEGORIES[self.category.GetSelection()]
            value = Rule(category, number(self.days.GetValue(), maximum=36500),
                         int(number(self.minimum.GetValue(), maximum=10**15 / 1024**2) * 1024**2),
                         "all" if self.combine.GetSelection() == 0 else "any",
                         MODES[self.mode.GetSelection()], self.enabled.GetValue(),
                         self._types[self.media_type.GetSelection()])
            if value.mode == "automatic" and value.enabled:
                if wx.MessageBox(self.i18n.t("storage_rule_automatic_confirm"),
                                 self.i18n.t("storage_rule_editor"),
                                 wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING, self) != wx.YES:
                    return
            self.result = value
            self.EndModal(wx.ID_OK)
        except (ValueError, OverflowError):
            wx.MessageBox(self.i18n.t("storage_preferences_invalid"), self.GetTitle(), wx.OK | wx.ICON_ERROR, self)


class StorageRulesPanel(wx.Panel):
    def __init__(self, parent, owner):
        super().__init__(parent)
        self.owner, self.rules = owner, list(owner.service.preferences.rules)
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.list = wx.ListBox(self, name=owner.i18n.t("storage_rules_tab"))
        sizer.Add(self.list, 1, wx.EXPAND | wx.ALL, 8)
        buttons = wx.WrapSizer(wx.HORIZONTAL)
        for key, callback in (("storage_add_rule", self.add), ("storage_edit_rule", self.edit),
                              ("storage_remove_rule", self.remove)):
            button = wx.Button(self, label=owner.i18n.t(key))
            button.Bind(wx.EVT_BUTTON, callback)
            buttons.Add(button, 0, wx.ALL, 5)
        sizer.Add(buttons)
        note = wx.StaticText(self, label=owner.i18n.t("storage_rules_save_note"))
        note.Wrap(650)
        sizer.Add(note, 0, wx.ALL, 8)
        self.SetSizer(sizer)
        self.refresh()

    def refresh(self):
        previous = self.list.GetSelection()
        self.list.Freeze()
        try:
            lines = []
            for rule in self.rules:
                lines.append(self.owner.i18n.t("storage_rule_row").format(
                    category=category_text(rule.category, self.owner.i18n),
                    mode=self.owner.i18n.t(MODE_KEYS[MODES.index(rule.mode)]),
                    state=self.owner.i18n.t("storage_rule_on" if rule.enabled else "storage_rule_off"),
                    days=rule.days, size=size_text(rule.minimum, self.owner.i18n)))
                lines[-1] += " — " + self.owner.i18n.t(TYPE_KEYS[MEDIA_TYPES.index(rule.media_type)])
                lines[-1] += " — " + self.owner.i18n.t("storage_match_all" if rule.combine == "all" else "storage_match_any")
            self.list.Set(lines)
            if lines:
                self.list.SetSelection(min(max(0, previous), len(lines) - 1))
        finally:
            self.list.Thaw()

    def _edit(self, index):
        with RuleEditor(self, self.owner.i18n, None if index is None else self.rules[index]) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            if index is None:
                self.rules.append(dialog.result)
            else:
                self.rules[index] = dialog.result
        self.refresh()
        self.owner.say(self.owner.i18n.t("storage_rules_save_note"))

    def add(self, event=None):
        if len(self.rules) >= 100:
            self.owner.error("storage_rule_limit")
            return
        self._edit(None)

    def edit(self, event=None):
        index = self.list.GetSelection()
        if index != wx.NOT_FOUND:
            self._edit(index)

    def remove(self, event=None):
        index = self.list.GetSelection()
        if index != wx.NOT_FOUND:
            del self.rules[index]
            self.refresh()
