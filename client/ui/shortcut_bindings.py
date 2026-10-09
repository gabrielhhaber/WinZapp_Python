"""wx boundary for the command catalog; rebuilding tables never rebinds handlers."""
import weakref
import inspect
import wx
from core.keyboard_shortcuts import (
    SECTION, binding_for, default_binding, logical_key, normalize_key,
)
from core.shortcut_catalog import SHORTCUTS


def main_window_for(owner):
    node = owner
    seen = set()
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        settings = _declared_attribute(node, 'settings')
        if isinstance(settings, dict) and _declared_attribute(node, 'i18n') is not None:
            return node
        reference = _declared_attribute(node, '_shortcut_window_ref')
        if isinstance(reference, weakref.ReferenceType) and reference() is not None:
            return reference()
        for attribute in ('main_window', '_mw', '_main_window'):
            candidate = _declared_attribute(node, attribute)
            if candidate is not None:
                node = candidate
                break
        else:
            get_parent = _declared_attribute(node, 'GetParent')
            node = get_parent() if get_parent is not None else None
    return None


def _declared_attribute(owner, name):
    """Do not let recording mocks manufacture a window or its preferences."""
    try:
        inspect.getattr_static(owner, name)
    except AttributeError:
        return None
    return getattr(owner, name, None)


def _command_id(owner, command):
    if '[' in command:
        name, digit = command.split('[', 1)
        return int(getattr(owner, name)[int(digit.rstrip(']'))])
    return int(getattr(owner, command))


def _table(rows, window):
    settings = getattr(window, 'settings', {})
    translate = window.i18n.t
    overrides = settings.get(SECTION, {})
    entries = []
    seen = set()
    for shortcut, command_id in rows:
        binding = binding_for(shortcut, overrides, translate)
        if binding is not None and (*binding, command_id) not in seen:
            seen.add((*binding, command_id))
            entries.append((*binding, command_id))
    return wx.AcceleratorTable(entries)


def make_shortcut_table(owner, scope, entries, target=None):
    window = main_window_for(owner)
    if window is None:
        # Recording stubs and early-startup dialogs retain the native table.
        return wx.AcceleratorTable(entries)
    target = owner if target is None else target
    descriptors = [s for s in SHORTCUTS if s.scope == scope]
    by_id = {}
    for shortcut in descriptors:
        try:
            command_id = _command_id(owner, shortcut.command)
        except (AttributeError, TypeError, IndexError):
            continue
        by_id.setdefault(command_id, []).append(shortcut)
    used, rows = {}, []
    for mod, key, command_id in entries:
        command_id = int(command_id)
        chord = mod, normalize_key(key)
        slots = used.setdefault(command_id, [])
        if chord not in slots:
            slots.append(chord)
        slot = slots.index(chord)
        choices = [s for s in by_id.get(command_id, ()) if s.slot == slot]
        if not choices:
            raise ValueError(f'Uncatalogued shortcut: {scope}, {command_id}, {slot}')
        row = choices[0], command_id
        if row not in rows:
            rows.append(row)
    target._configured_shortcut_rows = rows
    target._shortcut_window_ref = weakref.ref(window)
    targets = getattr(window, '_shortcut_targets', None)
    if targets is None:
        targets = window._shortcut_targets = weakref.WeakSet()
    targets.add(target)
    return _table(rows, window)


def refresh_shortcuts(window):
    for target in tuple(getattr(window, '_shortcut_targets', ())):
        try:
            target.SetAcceleratorTable(_table(target._configured_shortcut_rows, window))
        except RuntimeError:  # a modeless window was destroyed since registration
            window._shortcut_targets.discard(target)
    refresh_menu_shortcuts(window)
    if hasattr(window, '_set_bookmark_zero_hotkey') and hasattr(window, 'IsActive'):
        window._set_bookmark_zero_hotkey(False)
        window._set_bookmark_zero_hotkey(window.IsActive())
    for dialog in getattr(window, '_incoming_call_dialogs', {}).values():
        if dialog is not None:
            dialog._apply_labels()
    refresh_mnemonics(window)
    navigation = getattr(window, 'navigation_panel', None)
    if navigation is not None:
        navigation.rebuild_items()


def event_chord(event):
    modifiers = event.GetModifiers() if hasattr(event, 'GetModifiers') else 0
    ctrl = getattr(event, 'ControlDown', lambda: bool(modifiers & wx.MOD_CONTROL))()
    alt = getattr(event, 'AltDown', lambda: bool(modifiers & wx.MOD_ALT))()
    shift = getattr(event, 'ShiftDown', lambda: bool(modifiers & wx.MOD_SHIFT))()
    mod = (2 if ctrl else 0) | (1 if alt else 0) | (4 if shift else 0)
    if getattr(event, 'MetaDown', lambda: False)():
        mod |= 8  # Windows/Command gestures never match an application binding.
    key = event.GetKeyCode()
    aliases = {wx.WXK_NUMPAD_ENTER: wx.WXK_RETURN,
               wx.WXK_NUMPAD_LEFT: wx.WXK_LEFT, wx.WXK_NUMPAD_RIGHT: wx.WXK_RIGHT,
               wx.WXK_NUMPAD_UP: wx.WXK_UP, wx.WXK_NUMPAD_DOWN: wx.WXK_DOWN,
               wx.WXK_NUMPAD_HOME: wx.WXK_HOME, wx.WXK_NUMPAD_END: wx.WXK_END,
               wx.WXK_NUMPAD_PAGEUP: wx.WXK_PAGEUP, wx.WXK_NUMPAD_PAGEDOWN: wx.WXK_PAGEDOWN}
    aliases.update({wx.WXK_NUMPAD0 + digit: ord('0') + digit for digit in range(10)})
    return mod, normalize_key(aliases.get(key, key))


def matches(owner, event, shortcut_id):
    window = main_window_for(owner)
    shortcut = next(s for s in SHORTCUTS if s.id == shortcut_id)
    if window is None:
        return default_binding(shortcut, lambda key: key) == event_chord(event)
    return binding_for(shortcut, window.settings.get(SECTION, {}), window.i18n.t) == event_chord(event)


class _CommandKeyEvent:
    """Delegates native event operations; changes only the command gesture read."""
    def __init__(self, event, chord, scope):
        self._event, self._chord = event, chord
        self._native_event = _declared_attribute(event, '_native_event') or event
        self._original_chord = _declared_attribute(event, '_original_chord') or event_chord(event)
        self._pass_accelerator = bool(_declared_attribute(event, '_pass_accelerator'))
        self._configured_shortcut_scopes = (_declared_attribute(event, '_configured_shortcut_scopes') or set()) | {scope}

    def __getattr__(self, name):
        return getattr(self._event, name)

    def GetKeyCode(self):
        return self._chord[1]

    def Skip(self, *args, **kwargs):
        if self._chord[1] != -1 or self._pass_accelerator:
            self._native_event.Skip(*args, **kwargs)

    def ControlDown(self):
        return bool(self._chord[0] & 2)

    def AltDown(self):
        return bool(self._chord[0] & 1)

    def ShiftDown(self):
        return bool(self._chord[0] & 4)

    def HasAnyModifiers(self):
        return bool(self._chord[0])

    def GetModifiers(self):
        return ((wx.MOD_CONTROL if self.ControlDown() else 0)
                | (wx.MOD_ALT if self.AltDown() else 0)
                | (wx.MOD_SHIFT if self.ShiftDown() else 0))


def command_key_event(owner, scope, event):
    if scope in (_declared_attribute(event, '_configured_shortcut_scopes') or ()):
        return event
    window = main_window_for(owner)
    if window is None or not window.settings.get(SECTION):
        return event
    original = _declared_attribute(event, '_original_chord') or event_chord(event)
    chord = logical_key(scope, original, window.settings[SECTION], SHORTCUTS, window.i18n.t)
    # A sibling/child handler must not reinterpret another scope's logical key.
    if chord == original:
        chord = event_chord(event)
    result = _CommandKeyEvent(event, chord, scope)
    if chord[1] == -1:
        result._pass_accelerator = _ancestor_uses_chord(owner, event, window, original)
    return result


def _ancestor_uses_chord(owner, event, window, chord):
    """A freed gesture may now belong to an actual ancestor accelerator."""
    control = event.GetEventObject() if hasattr(event, 'GetEventObject') else owner
    seen = set()
    while control is not None and id(control) not in seen:
        seen.add(id(control))
        for shortcut, _ in getattr(control, '_configured_shortcut_rows', ()):
            if binding_for(shortcut, window.settings[SECTION], window.i18n.t) == chord:
                return True
        control = control.GetParent() if hasattr(control, 'GetParent') else None
    return False


def pass_to_ancestor_command(owner, event):
    window = main_window_for(owner)
    if window is not None and _ancestor_uses_chord(owner, event, window, event_chord(event)):
        event.Skip()
        return True
    return False


MENU_COMMANDS = {
    '_ID_SETTINGS': 'main.ID_CTRL_COMMA', '_ID_SHORTCUTS': 'main.ID_F1',
    '_ID_MARK_ALL_READ': 'main.mark_all_read', '_ID_DISCONNECT': 'main.disconnect',
    '_ID_EXIT': 'main.exit', '_ID_RESYNC_ALL': 'main.resync_all',
    '_ID_RESYNC_CONVERSATION': 'main.resync_conversation',
    '_ID_SYNC_MEDIA': 'main.sync_media', '_ID_OFFLINE_MENU': 'main.offline',
}


def navigation_shortcut_label(window, row, label):
    """Replace the shipped suffix without altering the translated row/count."""
    import re
    from ui.shortcut_names import shortcut_name
    shortcut_id = {'conversations': 'main.ID_ALT_1', 'archived': 'main.ID_ALT_4',
                   'locked': 'main.ID_ALT_7', 'status': 'main.ID_ALT_5',
                   'calls': 'main.ID_ALT_6', 'settings': 'main.ID_CTRL_COMMA'}[row]
    overrides = getattr(window, 'settings', {}).get(SECTION, {})
    if shortcut_id not in overrides:
        return label
    shortcut = next(s for s in SHORTCUTS if s.id == shortcut_id)
    binding = binding_for(shortcut, overrides, window.i18n.t)
    name = shortcut_name(binding, window.i18n) if binding is not None else ''
    if row == 'settings':
        suffix = window.i18n.t('settings_shortcut')
        text = label[:-len(suffix)].rstrip() if suffix and label.endswith(suffix) else label
    else:
        text = re.sub(r'\s*alt\+[1-7]\s*$', '', label, flags=re.I)
    return text + (' ' + name if name else '')


def refresh_menu_shortcuts(window):
    menubar = window.GetMenuBar() if hasattr(window, 'GetMenuBar') else None
    if menubar is None:
        return
    from ui.shortcut_names import shortcut_name
    by_id = {s.id: s for s in SHORTCUTS}
    for attribute, shortcut_id in MENU_COMMANDS.items():
        item_id = getattr(window, attribute, None)
        item = menubar.FindItemById(int(item_id)) if item_id is not None else None
        if item is not None:
            binding = binding_for(by_id[shortcut_id], window.settings.get(SECTION, {}), window.i18n.t)
            name = shortcut_name(binding, None)
            label = item.GetItemLabel().split('\t', 1)[0]
            item.SetItemLabel(label + ('\t' + name if name else ''))
    for item_id, action in getattr(window, '_accounts_menu_id_map', {}).items():
        shortcut_id = None
        if action.get('switch'):
            slot = next((n for n, account_id in getattr(window, '_account_hotkey_slots', {}).items()
                         if account_id == action['switch']), None)
            if slot is not None:
                shortcut_id = f'main.account[{slot}]'
        elif action.get('close_current'):
            shortcut_id = 'main.close_account'
        item = menubar.FindItemById(int(item_id))
        if item is not None and shortcut_id in by_id:
            binding = binding_for(by_id[shortcut_id], window.settings.get(SECTION, {}), window.i18n.t)
            name = shortcut_name(binding, None)
            item.SetItemLabel(item.GetItemLabel().split('\t', 1)[0] + ('\t' + name if name else ''))


def popup_label_chord(label):
    """(modifiers, key) a popup item label advertises after its tab, or None
    when wx cannot read it. FromString is an instance method returning a bool:
    calling it on the class raised TypeError and stopped every context menu
    from opening, including from the Applications key."""
    entry = wx.AcceleratorEntry()
    if not entry.FromString(label):
        return None
    return entry.GetFlags(), normalize_key(entry.GetKeyCode())


def refresh_popup_shortcuts(owner, scope, menu):
    window = main_window_for(owner)
    if window is None:
        return
    from ui.shortcut_names import shortcut_name
    for item in menu.GetMenuItems():
        submenu = item.GetSubMenu()
        if submenu is not None:
            refresh_popup_shortcuts(owner, scope, submenu)
        label = item.GetItemLabel()
        if '\t' not in label:
            continue
        chord = popup_label_chord(label)
        if chord is None:
            continue
        shortcut = next((s for s in SHORTCUTS if s.scope == scope
                         and default_binding(s, window.i18n.t) == chord), None)
        if shortcut is not None:
            binding = binding_for(shortcut, window.settings.get(SECTION, {}), window.i18n.t)
            name = shortcut_name(binding, None)
            item.SetItemLabel(label.split('\t', 1)[0] + ('\t' + name if name else ''))


def refresh_mnemonics(window):
    surfaces = [
        (getattr(window, 'navigation_panel', None), 'nav_label', 'main_nav', 'main.ID_ALT_NAV'),
        (getattr(window, 'conversations_panel', None), 'messages_label', 'messages', 'navigation.messages'),
        (getattr(window, 'conversations_panel', None), 'message_label', 'type_message', 'messages.ID_ALT_FOCUS_FIELD'),
    ]
    for panel, attribute, label, shortcut_id in surfaces:
        control = getattr(panel, attribute, None)
        if control is not None:
            text = control.GetLabel()
            previous = _declared_attribute(control, '_shortcut_label_source')
            if isinstance(previous, str) and text == _without_mnemonics(previous):
                text = previous
            if isinstance(text, str):
                set_shortcut_label(window, control, shortcut_id, text)


def _without_mnemonics(text):
    import re
    return re.sub(r'(?<!&)&(?!&)(?=.)', '', text)


def set_shortcut_label(owner, control, shortcut_id, text):
    """Keep reply/contact/permission context while suppressing a moved mnemonic."""
    window = main_window_for(owner)
    control._shortcut_label_source = text
    if window is not None and shortcut_id in window.settings.get(SECTION, {}):
        text = _without_mnemonics(text)
    control.SetLabel(text)


def accessible_shortcut(accessible, fallback, candidates):
    control = accessible.GetWindow()
    window = main_window_for(control) or main_window_for(getattr(accessible, '_mw', None))
    if window is None:
        return fallback
    while control is not None:
        rows = getattr(control, '_configured_shortcut_rows', ())
        shortcut = next((s for s, _ in rows if s.id in candidates), None)
        if shortcut is not None:
            from ui.shortcut_names import shortcut_name
            binding = binding_for(shortcut, window.settings.get(SECTION, {}), window.i18n.t)
            return shortcut_name(binding, window.i18n) if binding is not None else ''
        control = control.GetParent()
    shortcut = next((s for s in SHORTCUTS if s.id in candidates), None)
    if shortcut is not None:
        from ui.shortcut_names import shortcut_name
        binding = binding_for(shortcut, window.settings.get(SECTION, {}), window.i18n.t)
        return shortcut_name(binding, window.i18n) if binding is not None else ''
    return fallback


def bind_enter_command(owner, field, shortcut_id, handler):
    """Replace a TextCtrl's command Enter while leaving its editing keys native."""
    def on_key(event):
        if matches(owner, event, shortcut_id):
            handler(event)
            return
        window = main_window_for(owner)
        if (window is not None and shortcut_id in window.settings.get(SECTION, {})
                and event.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER)):
            pass_to_ancestor_command(owner, event)
            return
        event.Skip()
    field.Bind(wx.EVT_KEY_DOWN, on_key)


def handle_calls_shortcut(owner, event):
    if matches(owner, event, 'calls.next_filter'):
        owner.notebook.AdvanceSelection(True)
        return
    if matches(owner, event, 'calls.previous_filter'):
        owner.notebook.AdvanceSelection(False)
        return
    event = command_key_event(owner, 'calls', event)
    event.Skip()


def handle_call_list_shortcut(owner, event):
    event = command_key_event(owner, 'calls', event)
    if event.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
        owner._on_item_activated(event)
        return
    event.Skip()
