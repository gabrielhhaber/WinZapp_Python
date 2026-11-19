"""The character a key press types, for the "type anywhere to reply" redirect.

The conversation's EVT_CHAR_HOOK sends a letter or digit typed in the message
list to the message field. It used to take event.GetUnicodeKey() for that
character, but in a char hook that is the key's *virtual key code* for
anything but A-Z and 0-9. On a Brazilian ABNT2 keyboard the "/ ? °" key is
VK_ABNT_C1, 0xC1: chr(0xC1) is "Á", which isalnum() lets through, so "/" in
the message list wrote "Á" into the field (reported 2026-11-09). The "ç" key,
VK_OEM_1 (0xBA), came out as "º" the same way.

`typed_char()` asks the active keyboard layout what the key types, with the
Shift and Caps Lock state of the press (Windows ToUnicodeEx, flag 4: the
keyboard's dead-key state is left alone, so a pending accent is not eaten).
"" when the key types nothing or starts a dead key; None off Windows, where
the caller keeps to plain A-Z and 0-9.

Tested in test_typed_key.py.
"""
import sys

VK_SHIFT, VK_LSHIFT, VK_CAPITAL = 0x10, 0xA0, 0x14
#: ToUnicodeEx: do not change the keyboard state (Windows 10 1607 and later)
DONT_CHANGE_STATE = 0x4


def _windows_translate(vk, scan, shift, caps):
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetKeyboardLayout.argtypes = (wintypes.DWORD,)
    user32.GetKeyboardLayout.restype = ctypes.c_void_p
    user32.ToUnicodeEx.argtypes = (
        wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_ubyte), wintypes.LPWSTR,
        ctypes.c_int, wintypes.UINT, ctypes.c_void_p)
    user32.ToUnicodeEx.restype = ctypes.c_int
    state = (ctypes.c_ubyte * 256)()
    if shift:
        state[VK_SHIFT] = state[VK_LSHIFT] = 0x80
    if caps:
        state[VK_CAPITAL] = 0x01
    buffer = ctypes.create_unicode_buffer(8)
    count = user32.ToUnicodeEx(int(vk), int(scan), state, buffer, len(buffer),
                               DONT_CHANGE_STATE, user32.GetKeyboardLayout(0))
    return buffer.value[:count] if count > 0 else ""


def typed_char(vk, scan, shift, caps, translate=None):
    """The one printable character the key *vk* types, "" for none (a
    function key, a dead key, several characters), None with no translator."""
    if translate is None:
        if sys.platform != "win32":
            return None
        translate = _windows_translate
    if not vk:
        return ""
    try:
        text = translate(vk, scan, bool(shift), bool(caps))
    except Exception:
        return None
    return text if len(text) == 1 and text.isprintable() else ""


def ascii_letter_or_digit(code, shift, caps):
    """Without a translator: a plain A-Z (cased by Shift and Caps Lock) or
    0-9 from the char hook's key code, "" for anything else."""
    if ord("A") <= code <= ord("Z"):
        return chr(code) if bool(shift) != bool(caps) else chr(code).lower()
    if ord("0") <= code <= ord("9"):
        return chr(code)
    return ""
