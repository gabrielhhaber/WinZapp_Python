"""Readable command-key names. wx key codes are distinct from Win32 hotkey VKs."""
import wx

KEY_NAMES = {
    wx.WXK_BACK: 'shortcut_key_backspace', wx.WXK_DELETE: 'shortcut_key_delete',
    wx.WXK_RETURN: 'shortcut_key_enter', wx.WXK_NUMPAD_ENTER: 'shortcut_key_enter',
    wx.WXK_ESCAPE: 'shortcut_key_escape', wx.WXK_SPACE: 'shortcut_key_space',
    wx.WXK_TAB: 'shortcut_key_tab', wx.WXK_LEFT: 'shortcut_key_left',
    wx.WXK_RIGHT: 'shortcut_key_right', wx.WXK_UP: 'shortcut_key_up',
    wx.WXK_DOWN: 'shortcut_key_down', wx.WXK_HOME: 'shortcut_key_home',
    wx.WXK_END: 'shortcut_key_end', wx.WXK_PAGEUP: 'shortcut_key_pageup',
    wx.WXK_PAGEDOWN: 'shortcut_key_pagedown', wx.WXK_INSERT: 'shortcut_key_insert',
}
ENGLISH_KEYS = {
    'shortcut_key_backspace': 'Backspace', 'shortcut_key_delete': 'Delete',
    'shortcut_key_enter': 'Enter', 'shortcut_key_escape': 'Esc',
    'shortcut_key_space': 'Space', 'shortcut_key_tab': 'Tab',
    'shortcut_key_left': 'Left', 'shortcut_key_right': 'Right',
    'shortcut_key_up': 'Up', 'shortcut_key_down': 'Down',
    'shortcut_key_home': 'Home', 'shortcut_key_end': 'End',
    'shortcut_key_pageup': 'PageUp', 'shortcut_key_pagedown': 'PageDown',
    'shortcut_key_insert': 'Insert',
}


def layout_key_name(vk):
    """OEM punctuation/letters belong to the active layout, not Latin-1."""
    import sys
    if sys.platform != 'win32' or vk not in (*range(0xBA, 0xC1), *range(0xDB, 0xE3)):
        return None
    import ctypes
    user32 = ctypes.windll.user32
    user32.GetKeyboardLayout.restype = ctypes.c_void_p
    user32.MapVirtualKeyExW.argtypes = (ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p)
    code = user32.MapVirtualKeyExW(vk, 2, user32.GetKeyboardLayout(0)) & 0x7fffffff
    return chr(code).upper() if 0 < code <= 0x10ffff else None


def shortcut_name(binding, i18n):
    if binding is None:
        return i18n.t('shortcut_unassigned') if i18n else ''
    mod, key = binding
    parts = []
    for bit, name, english in ((2, 'shortcut_key_control', 'Ctrl'),
                               (1, 'shortcut_key_alt', 'Alt'),
                               (4, 'shortcut_key_shift', 'Shift')):
        if mod & bit:
            parts.append(i18n.t(name) if i18n else english)
    if key in KEY_NAMES:
        name = KEY_NAMES[key]
        parts.append(i18n.t(name) if i18n else ENGLISH_KEYS[name])
    elif wx.WXK_F1 <= key <= wx.WXK_F24:
        parts.append(f'F{key - wx.WXK_F1 + 1}')
    elif 33 <= key <= 0x10ffff:
        parts.append((layout_key_name(key) if i18n else None) or chr(key).upper())
    else:
        parts.append(str(key))
    return '+'.join(parts)


def hotkey_binding(vk, mod):
    special = {0x08: wx.WXK_BACK, 0x09: wx.WXK_TAB, 0x0D: wx.WXK_RETURN,
               0x1B: wx.WXK_ESCAPE, 0x20: wx.WXK_SPACE, 0x21: wx.WXK_PAGEUP,
               0x22: wx.WXK_PAGEDOWN, 0x23: wx.WXK_END, 0x24: wx.WXK_HOME,
               0x25: wx.WXK_LEFT, 0x26: wx.WXK_UP, 0x27: wx.WXK_RIGHT,
               0x28: wx.WXK_DOWN, 0x2D: wx.WXK_INSERT, 0x2E: wx.WXK_DELETE}
    if not vk:
        return None
    key = wx.WXK_F1 + vk - 0x70 if 0x70 <= vk <= 0x87 else special.get(vk, vk)
    return mod, key
