"""Shortcut preferences and conflict rules, independent of wx and the desktop.

Bindings are stored by command, never by a translated name or a runtime wx id.
An absent override follows the current locale's mnemonic; None disables a key.
The shipped overlaps are intentional (a child table wins over its parent), so
only newly introduced overlaps are refused. Native editor/OS keys remain native.
"""
from dataclasses import dataclass

ALT, CTRL, SHIFT = 1, 2, 4
SECTION = 'keyboard_shortcuts'


@dataclass(frozen=True)
class Shortcut:
    id: str
    scope: str
    command: str
    label: str
    mod: int
    key: int | str
    slot: int = 0


def normalize_key(key: int) -> int:
    key = int(key)  # wx constants are int subclasses; preferences are JSON ints.
    return key - 32 if 97 <= key <= 122 else key


def mnemonic_keycode(letter):
    """Resolve a translated mnemonic through the current Windows layout."""
    if letter.isascii():
        return ord(letter.upper())
    import sys
    if sys.platform != 'win32':
        return None
    import ctypes
    scan = ctypes.windll.user32.VkKeyScanW
    scan.restype = ctypes.c_short
    result = scan(ord(letter.lower()))
    return None if result == -1 or (result >> 8) & 0xff else result & 0xff


def default_binding(shortcut, translate):
    key = shortcut.key
    if key == 0:
        return None
    if isinstance(key, str):
        text = translate(key[1:])
        pos = 0
        letter = None
        while pos < len(text) - 1:
            if text[pos] == '&':
                if text[pos + 1] != '&':
                    letter = text[pos + 1].upper()
                    break
                pos += 1
            pos += 1
        if not letter or len(letter) != 1:
            return None
        key = mnemonic_keycode(letter)
        if key is None:
            return None
    return shortcut.mod, normalize_key(key)


def valid_binding(value):
    if value is None:
        return True
    return (isinstance(value, (list, tuple)) and len(value) == 2
            and isinstance(value[0], int) and not isinstance(value[0], bool) and 0 <= value[0] <= 7
            and isinstance(value[1], int) and not isinstance(value[1], bool) and 0 < value[1] <= 65535)


def sanitize_overrides(value, catalog):
    if not isinstance(value, dict):
        return {}
    known = {s.id for s in catalog}
    return {key: (None if binding is None else [int(binding[0]), normalize_key(binding[1])])
            for key, binding in value.items() if key in known and valid_binding(binding)}


def binding_for(shortcut, overrides, translate):
    if not isinstance(overrides, dict):
        return default_binding(shortcut, translate)
    if shortcut.id in overrides and valid_binding(overrides[shortcut.id]):
        value = overrides[shortcut.id]
        return None if value is None else (value[0], normalize_key(value[1]))
    return default_binding(shortcut, translate)


_MAIN_SCOPES = {'main', 'chats', 'archived', 'locked', 'messages',
                'message_list', 'composer', 'search', 'chat_selection', 'status', 'calls',
                'status_list', 'status_reply', 'links', 'mention', 'navigation', 'autocomplete'}


def scopes_overlap(left, right):
    if left == right:
        return True
    if 'global' in (left, right):
        return True
    if 'main' in (left, right):
        return left in _MAIN_SCOPES and right in _MAIN_SCOPES
    # The conversations table is the parent of the open conversation pane.
    if 'chats' in (left, right):
        return {left, right} <= {'chats', 'messages', 'message_list', 'composer', 'search', 'chat_selection', 'links', 'mention', 'autocomplete'}
    if 'messages' in (left, right):
        return {left, right} <= {'messages', 'message_list', 'composer', 'search', 'links', 'mention', 'autocomplete'}
    if {left, right} == {'composer', 'autocomplete'}:
        return True
    if 'status' in (left, right):
        return {left, right} <= {'status', 'status_list', 'status_reply'}
    if 'chat_selection' in (left, right):
        return left in {'chats', 'archived', 'locked', 'chat_selection'} and right in {'chats', 'archived', 'locked', 'chat_selection'}
    return False


def conflicts(shortcut, candidate, overrides, catalog, translate):
    if candidate is None:
        return []
    candidate = tuple(candidate)
    own_default = default_binding(shortcut, translate)
    own_scopes = {s.scope for s in catalog if s.id == shortcut.id} | {shortcut.scope}
    result = []
    for other in catalog:
        if other.id == shortcut.id or not any(scopes_overlap(scope, other.scope) for scope in own_scopes):
            continue
        if binding_for(other, overrides, translate) != candidate:
            continue
        # Preserve the existing child/parent priority only at its shipped keys.
        if candidate == own_default == default_binding(other, translate):
            continue
        result.append(other)
    return result


def logical_key(scope, chord, overrides, catalog, translate):
    """Adapt configurable command gestures to an existing stateful key handler.

    Native caret/Tab/slider handling is not rewritten. A moved command's old
    gesture no longer enters that command's branch; an unchanged command keeps
    precisely the old handler, including playback/selection and focus guards.
    """
    chord = chord[0], normalize_key(chord[1])
    if not isinstance(overrides, dict):
        return chord
    choices = [s for s in catalog if s.scope == scope]
    for shortcut in choices:
        if shortcut.id in overrides and binding_for(shortcut, overrides, translate) == chord:
            return default_binding(shortcut, translate)
    for shortcut in choices:
        if shortcut.id in overrides and default_binding(shortcut, translate) == chord:
            if binding_for(shortcut, overrides, translate) != chord:
                return 0, -1
    return chord


def action_text(shortcut, translate):
    text = translate(shortcut.label).replace('&', '')
    if shortcut.label.startswith('shortcut_'):
        text = text.partition(':')[2].strip() or text
    if '[' in shortcut.command:
        text += ' (' + shortcut.command.split('[', 1)[1].rstrip(']') + ')'
    return text
