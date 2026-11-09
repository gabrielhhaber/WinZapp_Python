"""Settings' command inventory and staged shortcut edits."""
import copy
import wx
from core.keyboard_shortcuts import (
    SECTION, Shortcut, action_text, binding_for, conflicts, default_binding,
    sanitize_overrides,
)
from core.call_logic import CALL_HOTKEY_DEFAULTS, call_hotkey_setting
from core.shortcut_catalog import SHORTCUTS
from ui.shortcut_names import shortcut_name, hotkey_binding
from ui.accessible import AccessibleShortcutCapture
from ui.dialogs.shortcut_capture import ShortcutCaptureDialog, classify_chord
from ui.dialogs.settings_pages import SubPageDialog, _fold
from ui.shortcut_bindings import event_chord, refresh_shortcuts

GLOBAL = Shortcut('global.restore', 'global', 'restore', 'global_hotkey_label', 0, 0)
# The two call keys are system-wide too, but live in their own settings
# (general.call_answer_hotkey / call_end_hotkey) and have a shipped key.
CALL_ANSWER = Shortcut('global.call_answer', 'global', 'call_answer', 'call_answer_hotkey_label', 0, 0)
CALL_END = Shortcut('global.call_end', 'global', 'call_end', 'call_end_hotkey_label', 0, 0)
CALL_KEYS = {CALL_ANSWER.id: 'call_answer_hotkey', CALL_END.id: 'call_end_hotkey'}
GLOBALS = (GLOBAL, CALL_ANSWER, CALL_END)
GLOBAL_IDS = frozenset(s.id for s in GLOBALS)
SCOPE_LABELS = {
    'global': 'shortcut_scope_global', 'main': 'shortcuts_nav_section',
    'navigation': 'main_nav',
    'autocomplete': 'shortcut_scope_mentions',
    'chats': 'conversations', 'archived': 'archived_chats', 'locked': 'locked_chats',
    'messages': 'shortcuts_conv_section', 'message_list': 'messages',
    'chat_selection': 'shortcuts_bulk_section', 'composer': 'type_message',
    'search': 'shortcuts_search_section', 'status': 'shortcuts_status_section',
    'calls': 'shortcuts_calls_section', 'media': 'media_viewer_title',
    'channels': 'channels_title', 'channel_people': 'shortcuts_channel_people_section',
    'call': 'shortcuts_call_window_section', 'incoming': 'incoming_call_popup_title',
    'ai_result': 'tab_ai_accessibility',
    'emoji': 'emoji_picker_title', 'links': 'shortcut_scope_links',
    'mention': 'shortcut_scope_mentions', 'device': 'tab_audio_devices',
    'status_list': 'shortcuts_status_section', 'status_reply': 'status_reply_send',
}


# F1 only: two contexts that share a name but use the same keys for different
# things get a name of their own, so Enter while typing a mention and Enter on
# a mention inside a message are not listed under one heading. The Settings
# page keeps SCOPE_LABELS.
HELP_HEADINGS = {
    'autocomplete': 'mention_suggestions_label',
    'mention': 'mentions_section_label',
}


def visible_shortcuts(window):
    """A hidden vault must not be disclosed by another Settings page."""
    vault = getattr(window, '_chat_lock_vault', None)
    hidden = (vault is not None and getattr(vault, 'configured', False)
              and getattr(vault, 'hide_navigation', False))
    accounts = bool(getattr(window, 'account_id', None) and getattr(window, 'registry', None))
    seen, result = set(), list(GLOBALS)
    for shortcut in SHORTCUTS:
        if hidden and (shortcut.scope == 'locked' or shortcut.id in ('main.ID_ALT_7', 'main.lock_vault')):
            continue
        if not accounts and (shortcut.command.startswith('account[') or shortcut.command == 'close_account'):
            continue
        if shortcut.id not in seen:
            seen.add(shortcut.id)
            result.append(shortcut)
    return result


def shortcut_groups(rows, category_of, text_of, query=""):
    """The shortcuts list as categories: [(category, [(shortcut, text), ...])],
    in the order the categories first appear in *rows*. Two scopes shown under
    the same name are one category. With *query*, only the shortcuts whose
    category, command or keys hold every word of it (accents and case
    ignored), and only the categories left with one."""
    words = _fold(query).split()
    groups, order = {}, []
    for shortcut in rows:
        category = category_of(shortcut)
        text = text_of(shortcut)
        if words and not all(word in _fold(f"{category} {text}") for word in words):
            continue
        if category not in groups:
            groups[category] = []
            order.append(category)
        groups[category].append((shortcut, text))
    return [(category, groups[category]) for category in order]


def rows_with_binding(rows, binding_of, chord):
    """The shortcuts of every category bound to *chord*; all of them with no
    chord."""
    if chord is None:
        return list(rows)
    chord = tuple(chord)
    return [shortcut for shortcut in rows if binding_of(shortcut) == chord]


def key_search_action(classification):
    """What the search-by-keys field does with a key, from classify_chord():
    'pass' lets Tab, Escape and reserved keys (Alt+F4) do what they always do,
    'wait' ignores a modifier pressed on its own, 'search' looks the chord up.
    A key that cannot be assigned without a modifier is still searched: Space
    and Delete are shipped shortcuts."""
    if classification in ('pass', 'shortcut_capture_reserved'):
        return 'pass'
    if classification == 'shortcut_capture_wait':
        return 'wait'
    return 'search'


def shortcuts_help_text(window, i18n):
    """The F1 list: one heading per context name, in order of first appearance.

    Scopes that share a name (and a context the catalog visits twice) fall
    under a single heading, except the two mention contexts (HELP_HEADINGS), and a line repeated word for word under the same
    heading is shown once. Keys and commands are untouched.
    """
    groups, order = {}, []
    for shortcut in visible_shortcuts(window):
        heading = i18n.t(HELP_HEADINGS.get(shortcut.scope) or SCOPE_LABELS[shortcut.scope]).replace('&', '')
        if shortcut.id == GLOBAL.id:
            hotkey = window.settings.get('general', {}).get('global_hotkey') or {}
            binding = hotkey_binding(hotkey.get('vk', 0), hotkey.get('mod', 0))
        elif shortcut.id in CALL_KEYS:
            pair = call_hotkey_setting(window.settings.get('general', {}), CALL_KEYS[shortcut.id])
            binding = hotkey_binding(*pair) if pair else None
        else:
            binding = binding_for(shortcut, window.settings.get(SECTION, {}), i18n.t)
        line = f'{action_text(shortcut, i18n.t)}: {shortcut_name(binding, i18n)}'
        if heading not in groups:
            groups[heading] = []
            order.append(heading)
        if line not in groups[heading]:
            groups[heading].append(line)
    lines = []
    for heading in order:
        lines.extend(['', heading, *groups[heading]])
    return '\n'.join(lines).strip()


class ShortcutsTabMixin:
    def _build_shortcuts_page(self):
        i18n = self.main_window.i18n
        self._shortcut_overrides = sanitize_overrides(
            copy.deepcopy(self.main_window.settings.get(SECTION, {})), SHORTCUTS)
        general = self.main_window.settings.get('general', {})
        self._call_hotkeys = {key: call_hotkey_setting(general, key) for key in CALL_HOTKEY_DEFAULTS}
        # What the rows started from: a key left alone is not saved, so one
        # shown as off only because the show-WinZapp key has its combination
        # comes back by itself once that key moves.
        self._call_hotkeys_loaded = dict(self._call_hotkeys)
        # A button on the Interface page opens these options in a window.
        page = self._shortcuts_page = wx.Panel(self._shortcuts_dialog)
        sizer = wx.BoxSizer(wx.VERTICAL)
        self._shortcuts_hint = wx.StaticText(page, label=i18n.t('shortcut_settings_hint'))
        self._shortcuts_hint.Wrap(580)
        sizer.Add(self._shortcuts_hint, 0, wx.EXPAND | wx.ALL, 8)
        # Down arrow goes from the search into the results, as in the
        # settings' own search; Up from the first row comes back.
        self._shortcut_search_label = wx.StaticText(page, label=i18n.t('shortcut_search_label'))
        sizer.Add(self._shortcut_search_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        self._shortcut_search = wx.TextCtrl(page)
        self._shortcut_search.SetName(i18n.t('shortcut_search_label').replace('&', ''))
        self._shortcut_search.Bind(wx.EVT_TEXT, self._on_shortcut_search_text)
        self._shortcut_search.Bind(wx.EVT_KEY_DOWN, self._on_shortcut_search_key)
        sizer.Add(self._shortcut_search, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        # Search by keys: the combination pressed here keeps, from every
        # category, the commands that use it.
        self._shortcut_chord = None
        self._shortcut_keys_label = wx.StaticText(page, label=i18n.t('shortcut_keys_search_label'))
        sizer.Add(self._shortcut_keys_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        keys_row = wx.BoxSizer(wx.HORIZONTAL)
        self._shortcut_keys = wx.TextCtrl(page, style=wx.TE_PROCESS_ENTER)
        keys_name = i18n.t('shortcut_keys_search_label').replace('&', '')
        self._shortcut_keys.SetName(keys_name)
        self._shortcut_keys.SetAccessible(AccessibleShortcutCapture(self._shortcut_keys, keys_name))
        self._shortcut_keys.Bind(wx.EVT_CHAR_HOOK, self._on_shortcut_keys_key)
        self._shortcut_keys.Bind(wx.EVT_CHAR, self._on_shortcut_keys_char)
        keys_row.Add(self._shortcut_keys, 1, wx.EXPAND | wx.RIGHT, 4)
        self._shortcut_keys_clear = wx.Button(page, label=i18n.t('shortcut_keys_search_clear'))
        self._shortcut_keys_clear.Bind(wx.EVT_BUTTON, self._clear_shortcut_keys)
        self._shortcut_keys_clear.Enable(False)
        keys_row.Add(self._shortcut_keys_clear, 0)
        sizer.Add(keys_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        # A tree: each category is said once, on its own row, and opens with
        # the right arrow, instead of being read before every command.
        self._shortcut_list = wx.TreeCtrl(
            page, style=wx.TR_HIDE_ROOT | wx.TR_HAS_BUTTONS | wx.TR_LINES_AT_ROOT | wx.TR_SINGLE)
        self._shortcut_list.SetName(i18n.t('shortcut_settings_list'))
        sizer.Add(self._shortcut_list, 1, wx.EXPAND | wx.ALL, 8)
        self._shortcut_list.Bind(wx.EVT_TREE_ITEM_ACTIVATED, self._on_shortcut_activated)
        self._shortcut_list.Bind(wx.EVT_KEY_DOWN, self._on_shortcut_tree_key)
        self._shortcut_list.Bind(wx.EVT_SET_FOCUS, self._on_shortcut_tree_focus)
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
        self._shortcuts_dialog.set_body(page)
        self._hotkey_field.Bind(wx.EVT_TEXT, self._shortcut_hotkey_changed)
        # The rows are built once, by _load_values() after the values
        # are loaded: building them here too cost a whole tree (0.2 s with a
        # screen reader running) that was thrown away a moment later.

    def _shortcut_hotkey_changed(self, event):
        # Coalesced and after the event: loading the dialog's values sets this
        # field, which rebuilt the whole tree a moment after it was built,
        # while the Settings window was still being put together.
        event.Skip()
        if getattr(self, '_loading_values', False):
            return       # SettingsDialog.__init__ builds the rows after loading
        if getattr(self, '_shortcut_refresh_pending', False):
            return
        self._shortcut_refresh_pending = True
        wx.CallAfter(self._deferred_shortcut_refresh)

    def _deferred_shortcut_refresh(self):
        self._shortcut_refresh_pending = False
        try:
            self._refresh_shortcut_rows()
        except RuntimeError:     # the dialog was closed in the meantime
            pass

    def _shortcut_binding(self, shortcut):
        if shortcut.id == GLOBAL.id:
            return hotkey_binding(self._hotkey_field._vk, self._hotkey_field._mod)
        if shortcut.id in CALL_KEYS:
            pair = self._call_hotkeys[CALL_KEYS[shortcut.id]]
            return hotkey_binding(*pair) if pair else None
        return binding_for(shortcut, self._shortcut_overrides, self.main_window.i18n.t)

    def _shortcut_groups(self):
        i18n = self.main_window.i18n
        self._shortcut_rows = visible_shortcuts(self.main_window)
        return shortcut_groups(
            rows_with_binding(self._shortcut_rows, self._shortcut_binding,
                              getattr(self, '_shortcut_chord', None)),
            lambda s: i18n.t(SCOPE_LABELS[s.scope]).replace('&', ''),
            lambda s: f'{action_text(s, i18n.t)}: {shortcut_name(self._shortcut_binding(s), i18n)}',
            self._shortcut_search.GetValue())

    def _refresh_shortcut_rows(self):
        """Rebuild the tree, keeping the command that was selected selected
        (its category open). While searching every category is open.

        The row is selected only while the tree has the focus: selecting a
        row moves the tree's caret, and the screen reader announces that as
        if the focus had gone there. Typing in the search field, or Apply
        relabelling the hidden Shortcuts window, then sounded like a jump to
        the results. Otherwise the row waits for the tree to take the focus
        (_on_shortcut_tree_focus)."""
        tree = self._shortcut_list
        previous = self._selected_shortcut()
        self._shortcut_pending_item = None
        filtering = (bool(self._shortcut_search.GetValue().strip())
                     or getattr(self, '_shortcut_chord', None) is not None)
        groups = self._shortcut_groups()
        tree.Freeze()
        try:
            tree.DeleteAllItems()
            root = tree.AddRoot('')
            chosen = None
            for category, items in groups:
                node = tree.AppendItem(root, category)
                for shortcut, text in items:
                    item = tree.AppendItem(node, text, data=shortcut)
                    if previous is not None and shortcut.id == previous.id:
                        chosen = item
                if filtering:
                    tree.Expand(node)
            if not groups:
                tree.AppendItem(root, self.main_window.i18n.t('shortcut_search_none'))
            if chosen is None and filtering:
                chosen = self._first_shortcut_item()
            if chosen is None:
                chosen = tree.GetFirstChild(root)[0]
            if chosen is not None and chosen.IsOk():
                if self._shortcut_tree_has_focus():
                    tree.EnsureVisible(chosen)
                    tree.SelectItem(chosen)
                else:
                    self._shortcut_pending_item = chosen
        finally:
            tree.Thaw()
        return sum(len(items) for _category, items in groups)

    def _on_shortcut_keys_char(self, event):
        # The field shows the combination, never the letters typed.
        if event.GetKeyCode() == wx.WXK_TAB:
            event.Skip()

    def _on_shortcut_keys_key(self, event):
        mod, key = event_chord(event)
        action = key_search_action(classify_chord(mod, key))
        if action == 'pass':
            event.Skip()
            return
        if action == 'wait':
            return
        self._search_shortcut_keys((mod, key))

    def _on_shortcut_search_text(self, event):
        # One search at a time: typing a name ends the search by keys.
        if self._shortcut_chord is not None:
            self._shortcut_chord = None
            self._shortcut_keys.ChangeValue('')
            self._shortcut_keys_clear.Enable(False)
        self._refresh_shortcut_rows()

    def _search_shortcut_keys(self, chord):
        """Every category's commands on *chord*, whatever the name search
        held (it is emptied), and how many there are, spoken."""
        i18n = self.main_window.i18n
        keys = shortcut_name(chord, i18n)
        self._shortcut_chord = chord
        self._shortcut_search.ChangeValue('')
        self._shortcut_keys.ChangeValue(keys)
        self._shortcut_keys_clear.Enable(True)
        count = self._refresh_shortcut_rows()
        if count:
            text = i18n.t('shortcut_keys_search_found').format(keys=keys, count=count)
        else:
            text = i18n.t('shortcut_keys_search_none').format(keys=keys)
        self.main_window.speak_output.output(text)

    def _clear_shortcut_keys(self, event=None):
        self._shortcut_chord = None
        self._shortcut_keys.ChangeValue('')
        # The button is about to be disabled: the focus goes back to the field.
        self._shortcut_keys.SetFocus()
        self._shortcut_keys_clear.Enable(False)
        self._refresh_shortcut_rows()

    def _shortcut_tree_has_focus(self):
        try:
            return wx.Window.FindFocus() is self._shortcut_list
        except Exception:
            return False

    def _pending_shortcut(self):
        """The command waiting to be selected when the tree takes the focus."""
        item = getattr(self, '_shortcut_pending_item', None)
        if item is None or not item.IsOk():
            return None
        data = self._shortcut_list.GetItemData(item)
        return data if isinstance(data, Shortcut) else None

    def _on_shortcut_tree_focus(self, event):
        # Before the control's own focus handling, which would otherwise put
        # its caret on the first row.
        item = getattr(self, '_shortcut_pending_item', None)
        self._shortcut_pending_item = None
        if item is not None and item.IsOk():
            self._shortcut_list.EnsureVisible(item)
            self._shortcut_list.SelectItem(item)
        event.Skip()

    def _selected_shortcut(self):
        tree = self._shortcut_list
        item = tree.GetSelection()
        if not item.IsOk():
            # A button pressed before the tree ever took the focus.
            return self._pending_shortcut()
        data = tree.GetItemData(item)
        return data if isinstance(data, Shortcut) else None

    def _first_shortcut_item(self):
        tree = self._shortcut_list
        category = tree.GetFirstChild(tree.GetRootItem())[0]
        while category.IsOk():
            child = tree.GetFirstChild(category)[0]
            if child.IsOk():
                return child
            category = tree.GetNextSibling(category)
        return None

    def _on_shortcut_search_key(self, event):
        if event.GetKeyCode() == wx.WXK_DOWN:
            item = self._first_shortcut_item()
            if item is not None:
                self._shortcut_pending_item = None
                self._shortcut_list.EnsureVisible(item)
                self._shortcut_list.SelectItem(item)
                self._shortcut_list.SetFocus()
                return
        event.Skip()

    def _on_shortcut_tree_key(self, event):
        tree = self._shortcut_list
        if event.GetKeyCode() == wx.WXK_UP:
            selection = tree.GetSelection()
            if not selection.IsOk() or not tree.GetPrevVisible(selection).IsOk():
                self._shortcut_search.SetFocus()
                return
        event.Skip()

    def _on_shortcut_activated(self, event):
        # Enter on a command changes it; on a category it opens or closes it.
        if self._selected_shortcut() is not None:
            self._change_shortcut()
        else:
            event.Skip()

    def _change_shortcut(self, event=None):
        shortcut = self._selected_shortcut()
        if shortcut is None:
            return
        i18n = self.main_window.i18n
        dialog = ShortcutCaptureDialog(self._shortcuts_dialog, action_text(shortcut, i18n.t),
                                       self._shortcut_binding(shortcut), shortcut.id in GLOBAL_IDS,
                                       main_window=self.main_window)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            if not self._accept_shortcut(shortcut, dialog.binding):
                return
            if shortcut.id == GLOBAL.id:
                self._hotkey_field._vk, self._hotkey_field._mod = dialog.vk, dialog.binding[0]
                self._hotkey_field.SetValue(shortcut_name(dialog.binding, i18n))
            elif shortcut.id in CALL_KEYS:
                self._call_hotkeys[CALL_KEYS[shortcut.id]] = (dialog.vk, dialog.binding[0])
            else:
                self._shortcut_overrides[shortcut.id] = list(dialog.binding)
            self._shortcut_edit_finished()
        finally:
            dialog.Destroy()

    def _accept_shortcut(self, shortcut, candidate, translate=None):
        i18n = self.main_window.i18n
        found = conflicts(shortcut, candidate, self._shortcut_overrides, SHORTCUTS, translate or i18n.t)
        for other in GLOBALS:
            if (candidate is not None and other.id != shortcut.id
                    and candidate == self._shortcut_binding(other)):
                found.append(other)
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
        elif shortcut.id in CALL_KEYS:
            self._call_hotkeys[CALL_KEYS[shortcut.id]] = None
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
        if shortcut.id in CALL_KEYS:
            key = CALL_KEYS[shortcut.id]
            candidate = hotkey_binding(*CALL_HOTKEY_DEFAULTS[key])
            if self._accept_shortcut(shortcut, candidate):
                self._call_hotkeys[key] = CALL_HOTKEY_DEFAULTS[key]
                self._shortcut_edit_finished()
            return
        candidate = default_binding(shortcut, self.main_window.i18n.t)
        if self._accept_shortcut(shortcut, candidate):
            self._shortcut_overrides.pop(shortcut.id, None)
            self._shortcut_edit_finished()

    def _reset_all_shortcuts(self, event=None):
        self._shortcut_overrides.clear()
        self._call_hotkeys = dict(CALL_HOTKEY_DEFAULTS)
        self._hotkey_field._vk = self._hotkey_field._mod = 0
        self._hotkey_field.SetValue('')
        self._shortcut_edit_finished()

    def _shortcut_edit_finished(self):
        self._refresh_shortcut_rows()
        self._mark_dirty()
        self._shortcut_list.SetFocus()

    def _apply_shortcut_values(self):
        self.main_window.settings[SECTION] = copy.deepcopy(self._shortcut_overrides)
        general = self.main_window.settings.setdefault('general', {})
        loaded = getattr(self, '_call_hotkeys_loaded', None) or {}
        for key, pair in self._call_hotkeys.items():
            if key in loaded and loaded[key] == pair:
                continue
            general[key] = {'vk': pair[0], 'mod': pair[1]} if pair else {'vk': 0, 'mod': 0}
        refresh_shortcuts(self.main_window)
        # A call that is ringing right now picks up the keys just chosen. The
        # dialog can also sit on a plain frame (its tests), without the mixin.
        sync = getattr(self.main_window, '_request_call_hotkey_sync', None)
        if sync is not None:
            sync()

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
                    self._show_page(self._shortcuts_page, self._shortcut_list)
                    return False
        for global_shortcut in GLOBALS:
            global_binding = self._shortcut_binding(global_shortcut)
            if global_binding is not None and not self._accept_shortcut(
                    global_shortcut, global_binding, translate):
                self._show_page(self._shortcuts_page, self._shortcut_list)
                return False
        return True

    def _refresh_shortcut_labels(self):
        i18n = self.main_window.i18n
        self._shortcuts_hint.SetLabel(i18n.t('shortcut_settings_hint'))
        self._shortcut_list.SetName(i18n.t('shortcut_settings_list'))
        self._shortcut_search_label.SetLabel(i18n.t('shortcut_search_label'))
        self._shortcut_search.SetName(i18n.t('shortcut_search_label').replace('&', ''))
        keys_name = i18n.t('shortcut_keys_search_label').replace('&', '')
        self._shortcut_keys_label.SetLabel(i18n.t('shortcut_keys_search_label'))
        self._shortcut_keys.SetName(keys_name)
        self._shortcut_keys.SetAccessible(AccessibleShortcutCapture(self._shortcut_keys, keys_name))
        self._shortcut_keys_clear.SetLabel(i18n.t('shortcut_keys_search_clear'))
        if self._shortcut_chord is not None:
            self._shortcut_keys.ChangeValue(shortcut_name(self._shortcut_chord, i18n))
        for button, key in self._shortcut_buttons:
            button.SetLabel(i18n.t(key))
        self._refresh_shortcut_rows()
