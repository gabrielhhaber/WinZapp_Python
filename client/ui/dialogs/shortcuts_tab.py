"""Settings' command inventory and staged shortcut edits."""
import copy
import wx
from core.keyboard_shortcuts import (
    SECTION, Shortcut, action_text, binding_for, conflicts, default_binding,
    sanitize_overrides,
)
from core.shortcut_catalog import SHORTCUTS
from ui.shortcut_names import shortcut_name, hotkey_binding
from ui.dialogs.shortcut_capture import ShortcutCaptureDialog
from ui.shortcut_bindings import refresh_shortcuts

GLOBAL = Shortcut('global.restore', 'global', 'restore', 'global_hotkey_label', 0, 0)
SCOPE_LABELS = {
    'global': 'shortcut_scope_global', 'main': 'shortcuts_nav_section',
    'navigation': 'main_nav',
    'autocomplete': 'shortcut_scope_mentions',
    'chats': 'conversations', 'archived': 'archived_chats', 'locked': 'locked_chats',
    'messages': 'shortcuts_conv_section', 'message_list': 'messages',
    'chat_selection': 'shortcuts_bulk_section', 'composer': 'type_message',
    'search': 'shortcuts_search_section', 'status': 'shortcuts_status_section',
    'calls': 'shortcuts_calls_section', 'media': 'media_viewer_title',
    'call': 'shortcuts_call_window_section', 'incoming': 'incoming_call_popup_title',
    'ai_result': 'tab_ai_accessibility',
    'emoji': 'emoji_picker_title', 'links': 'shortcut_scope_links',
    'mention': 'shortcut_scope_mentions', 'device': 'tab_audio_devices',
    'status_list': 'shortcuts_status_section', 'status_reply': 'status_reply_send',
}


def visible_shortcuts(window):
    """A hidden vault must not be disclosed by another Settings page."""
    vault = getattr(window, '_chat_lock_vault', None)
    hidden = (vault is not None and getattr(vault, 'configured', False)
              and getattr(vault, 'hide_navigation', False))
    accounts = bool(getattr(window, 'account_id', None) and getattr(window, 'registry', None))
    seen, result = set(), [GLOBAL]
    for shortcut in SHORTCUTS:
        if hidden and (shortcut.scope == 'locked' or shortcut.id in ('main.ID_ALT_7', 'main.lock_vault')):
            continue
        if not accounts and (shortcut.command.startswith('account[') or shortcut.command == 'close_account'):
            continue
        if shortcut.id not in seen:
            seen.add(shortcut.id)
            result.append(shortcut)
    return result


def shortcuts_help_text(window, i18n):
    lines, previous = [], None
    for shortcut in visible_shortcuts(window):
        if shortcut.scope != previous:
            lines.extend(['', i18n.t(SCOPE_LABELS[shortcut.scope]).replace('&', '')])
            previous = shortcut.scope
        if shortcut.id == GLOBAL.id:
            hotkey = window.settings.get('general', {}).get('global_hotkey') or {}
            binding = hotkey_binding(hotkey.get('vk', 0), hotkey.get('mod', 0))
        else:
            binding = binding_for(shortcut, window.settings.get(SECTION, {}), i18n.t)
        lines.append(f'{action_text(shortcut, i18n.t)}: {shortcut_name(binding, i18n)}')
    return '\n'.join(lines).strip()


class ShortcutsTabMixin:
    def _build_shortcuts_page(self):
        i18n = self.main_window.i18n
        self._shortcut_overrides = sanitize_overrides(
            copy.deepcopy(self.main_window.settings.get(SECTION, {})), SHORTCUTS)
        page = self._shortcuts_page = wx.Panel(self._notebook)
        sizer = wx.BoxSizer(wx.VERTICAL)
        self._shortcuts_hint = wx.StaticText(page, label=i18n.t('shortcut_settings_hint'))
        self._shortcuts_hint.Wrap(580)
        sizer.Add(self._shortcuts_hint, 0, wx.EXPAND | wx.ALL, 8)
        self._shortcut_list = wx.ListBox(page)
        self._shortcut_list.SetName(i18n.t('shortcut_settings_list'))
        sizer.Add(self._shortcut_list, 1, wx.EXPAND | wx.ALL, 8)
        self._shortcut_list.Bind(wx.EVT_LISTBOX_DCLICK, self._change_shortcut)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self._shortcut_buttons = []
        for key, handler in (
            ('shortcut_change', self._change_shortcut),
            ('shortcut_remove', self._remove_shortcut),
            ('shortcut_reset', self._reset_shortcut),
            ('shortcut_reset_all', self._reset_all_shortcuts),
        ):
            button = wx.Button(page, label=i18n.t(key))
            button.Bind(wx.EVT_BUTTON, handler)
            buttons.Add(button, 0, wx.ALL, 4)
            self._shortcut_buttons.append((button, key))
        sizer.Add(buttons, 0, wx.ALL, 4)
        page.SetSizer(sizer)
        self._notebook.AddPage(page, i18n.t('tab_shortcuts'))
        self._hotkey_field.Bind(wx.EVT_TEXT, self._shortcut_hotkey_changed)
        self._refresh_shortcut_rows()

    def _shortcut_hotkey_changed(self, event):
        self._refresh_shortcut_rows()
        event.Skip()

    def _shortcut_binding(self, shortcut):
        if shortcut.id == GLOBAL.id:
            return hotkey_binding(self._hotkey_field._vk, self._hotkey_field._mod)
        return binding_for(shortcut, self._shortcut_overrides, self.main_window.i18n.t)

    def _refresh_shortcut_rows(self):
        i18n = self.main_window.i18n
        self._shortcut_rows = visible_shortcuts(self.main_window)
        focused = self._shortcut_list.GetSelection()
        self._shortcut_list.Freeze()
        try:
            self._shortcut_list.Set([
                f'{i18n.t(SCOPE_LABELS[s.scope]).replace("&", "")} — '
                f'{action_text(s, i18n.t)}: {shortcut_name(self._shortcut_binding(s), i18n)}'
                for s in self._shortcut_rows
            ])
            if self._shortcut_rows:
                self._shortcut_list.SetSelection(max(0, min(focused, len(self._shortcut_rows) - 1)))
        finally:
            self._shortcut_list.Thaw()

    def _selected_shortcut(self):
        index = self._shortcut_list.GetSelection()
        return self._shortcut_rows[index] if 0 <= index < len(self._shortcut_rows) else None

    def _change_shortcut(self, event=None):
        shortcut = self._selected_shortcut()
        if shortcut is None:
            return
        i18n = self.main_window.i18n
        dialog = ShortcutCaptureDialog(self, action_text(shortcut, i18n.t),
                                       self._shortcut_binding(shortcut), shortcut.id == GLOBAL.id)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            if not self._accept_shortcut(shortcut, dialog.binding):
                return
            if shortcut.id == GLOBAL.id:
                self._hotkey_field._vk, self._hotkey_field._mod = dialog.vk, dialog.binding[0]
                self._hotkey_field.SetValue(shortcut_name(dialog.binding, i18n))
            else:
                self._shortcut_overrides[shortcut.id] = list(dialog.binding)
            self._shortcut_edit_finished()
        finally:
            dialog.Destroy()

    def _accept_shortcut(self, shortcut, candidate, translate=None):
        i18n = self.main_window.i18n
        found = conflicts(shortcut, candidate, self._shortcut_overrides, SHORTCUTS, translate or i18n.t)
        if candidate is not None and shortcut.id != GLOBAL.id and candidate == self._shortcut_binding(GLOBAL):
            found.append(GLOBAL)
        if found:
            names = '\n'.join(dict.fromkeys(action_text(s, i18n.t) for s in found))
            wx.MessageBox(i18n.t('shortcut_conflict').format(actions=names),
                          i18n.t('shortcut_change_title'), wx.OK | wx.ICON_WARNING, self)
            return False
        return True

    def _remove_shortcut(self, event=None):
        shortcut = self._selected_shortcut()
        if shortcut is None:
            return
        if shortcut.id == GLOBAL.id:
            self._hotkey_field._vk = self._hotkey_field._mod = 0
            self._hotkey_field.SetValue('')
        else:
            self._shortcut_overrides[shortcut.id] = None
        self._shortcut_edit_finished()

    def _reset_shortcut(self, event=None):
        shortcut = self._selected_shortcut()
        if shortcut is None:
            return
        if shortcut.id == GLOBAL.id:
            self._remove_shortcut()
            return
        candidate = default_binding(shortcut, self.main_window.i18n.t)
        if self._accept_shortcut(shortcut, candidate):
            self._shortcut_overrides.pop(shortcut.id, None)
            self._shortcut_edit_finished()

    def _reset_all_shortcuts(self, event=None):
        self._shortcut_overrides.clear()
        self._hotkey_field._vk = self._hotkey_field._mod = 0
        self._hotkey_field.SetValue('')
        self._shortcut_edit_finished()

    def _shortcut_edit_finished(self):
        self._refresh_shortcut_rows()
        self._mark_dirty()
        self._shortcut_list.SetFocus()

    def _apply_shortcut_values(self):
        self.main_window.settings[SECTION] = copy.deepcopy(self._shortcut_overrides)
        refresh_shortcuts(self.main_window)

    def _validate_shortcut_values(self):
        """Also check General's legacy capture field before any settings mutate."""
        translate = self.main_window.i18n.t
        selection = self._lang_combo.GetSelection()
        if selection != wx.NOT_FOUND and self._lang_codes[selection] != self.main_window.i18n.language:
            from core.translation_catalog import load_catalog
            translations = load_catalog(self._lang_codes[selection])
            translate = lambda key: translations.get(key, self.main_window.i18n.t(key))
        for shortcut in SHORTCUTS:
            if shortcut.id in self._shortcut_overrides:
                candidate = binding_for(shortcut, self._shortcut_overrides, translate)
                if not self._accept_shortcut(shortcut, candidate, translate):
                    self._notebook.SetSelection(self._notebook.FindPage(self._shortcuts_page))
                    return False
        global_binding = self._shortcut_binding(GLOBAL)
        if global_binding is not None and not self._accept_shortcut(GLOBAL, global_binding, translate):
            self._notebook.SetSelection(self._notebook.FindPage(self._shortcuts_page))
            return False
        return True

    def _refresh_shortcut_labels(self):
        i18n = self.main_window.i18n
        self._notebook.SetPageText(self._notebook.FindPage(self._shortcuts_page), i18n.t('tab_shortcuts'))
        self._shortcuts_hint.SetLabel(i18n.t('shortcut_settings_hint'))
        self._shortcut_list.SetName(i18n.t('shortcut_settings_list'))
        for button, key in self._shortcut_buttons:
            button.SetLabel(i18n.t(key))
        self._refresh_shortcut_rows()
