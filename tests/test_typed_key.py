"""Typing in the message list goes to the message field as the layout types it.

"/" on an ABNT2 keyboard wrote "Á" into the message field: the char hook's
GetUnicodeKey() is the key's virtual code (VK_ABNT_C1 = 0xC1) for anything
but A-Z and 0-9, and chr(0xC1) is a letter.
"""
import sys

import pytest

from core.typed_key import ascii_letter_or_digit, typed_char
from ui.conversations import ConversationsPanel

VK_ABNT_C1, VK_OEM_1, VK_A, VK_F5 = 0xC1, 0xBA, 0x41, 0x74

# What a Portuguese (Brazil ABNT2) layout types for these keys
ABNT2 = {
    (VK_ABNT_C1, False): "/", (VK_ABNT_C1, True): "?",
    (VK_OEM_1, False): "ç", (VK_OEM_1, True): "Ç",
    (VK_A, False): "a", (VK_A, True): "A",
    (0xDB, False): "",          # ´ is a dead key: types nothing yet
}


def abnt2(vk, scan, shift, caps):
    return ABNT2.get((vk, shift != caps if vk == VK_A else shift), "")


def test_the_slash_key_types_a_slash_not_a_latin_letter():
    assert typed_char(VK_ABNT_C1, 0x73, False, False, abnt2) == "/"
    assert chr(VK_ABNT_C1) == "Á"           # what used to be written


def test_cedilla_and_letters_follow_shift_and_caps_lock():
    assert typed_char(VK_OEM_1, 0x27, False, False, abnt2) == "ç"
    assert typed_char(VK_OEM_1, 0x27, True, False, abnt2) == "Ç"
    assert typed_char(VK_A, 0x1E, False, True, abnt2) == "A"
    assert typed_char(VK_A, 0x1E, True, True, abnt2) == "a"


def test_a_dead_key_or_a_function_key_types_nothing():
    assert typed_char(0xDB, 0x1A, False, False, abnt2) == ""
    assert typed_char(VK_F5, 0x3F, False, False, abnt2) == ""
    assert typed_char(0, 0, False, False, abnt2) == ""


def test_several_characters_or_a_control_character_are_nothing():
    assert typed_char(VK_A, 0, False, False, lambda *a: "ab") == ""
    assert typed_char(VK_A, 0, False, False, lambda *a: "\x08") == ""


def test_without_a_translator_only_plain_letters_and_digits():
    assert ascii_letter_or_digit(VK_A, False, False) == "a"
    assert ascii_letter_or_digit(VK_A, True, False) == "A"
    assert ascii_letter_or_digit(ord("7"), False, False) == "7"
    assert ascii_letter_or_digit(VK_ABNT_C1, False, False) == ""


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows layout API")
def test_the_real_translator_answers_without_raising():
    # whatever the machine's layout, "A" types a letter
    assert typed_char(VK_A, 0x1E, False, False).isalpha()


class _Event:
    def __init__(self, vk, unicode_key, shift=False):
        self.vk, self.unicode_key, self.shift = vk, unicode_key, shift

    def GetRawKeyCode(self):
        return self.vk

    def GetRawKeyFlags(self):
        return 0x73 << 16

    def GetUnicodeKey(self):
        return self.unicode_key

    def ShiftDown(self):
        return self.shift


def test_the_conversation_hook_asks_the_layout(monkeypatch):
    import core.typed_key as tk
    import ui.conversation_panel.composer as composer
    monkeypatch.setattr(composer.wx, "GetKeyState", lambda key: False)
    monkeypatch.setattr(tk, "_windows_translate", abnt2)
    monkeypatch.setattr(tk.sys, "platform", "win32")
    # the hook reports the virtual key (0xC1) as its unicode key
    assert ConversationsPanel._typed_char(_Event(VK_ABNT_C1, VK_ABNT_C1)) == "/"
    assert ConversationsPanel._typed_char(_Event(VK_OEM_1, VK_OEM_1, shift=True)) == "Ç"


def test_the_slash_is_not_sent_to_the_message_field(monkeypatch):
    import core.typed_key as tk
    import ui.conversations as conversations_module
    monkeypatch.setattr(tk, "_windows_translate", abnt2)
    monkeypatch.setattr(tk.sys, "platform", "win32")
    monkeypatch.setattr(conversations_module.wx, "GetKeyState", lambda key: False)
    monkeypatch.setattr(conversations_module.wx.Window, "FindFocus", staticmethod(lambda: None))

    class Shown:
        def IsShown(self):
            return True

        def IsEnabled(self):
            return True

    class Stub:
        _should_redirect_char_to_message = ConversationsPanel._should_redirect_char_to_message
        _is_phantom_nvda_char = staticmethod(ConversationsPanel._is_phantom_nvda_char)
        _typed_char = staticmethod(ConversationsPanel._typed_char)

        def __init__(self):
            self.conversation = object()
            self.conversation_panel = self.message_field = Shown()
            self._is_recording = False

    def event(vk):
        e = _Event(vk, vk)
        e.ControlDown = e.AltDown = lambda: False
        return e
    assert Stub()._should_redirect_char_to_message(event(VK_ABNT_C1)) is False
    assert Stub()._should_redirect_char_to_message(event(VK_OEM_1)) is True


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows layout API")
def test_the_key_nvda_injects_types_nothing_on_any_layout():
    # VK 0xFF, injected so the Start menu does not open after a Windows-key
    # gesture: its char-hook "unicode key" was 0xFF, a "ÿ" in the field
    assert typed_char(0xFF, 0, False, False) == ""
    assert typed_char(0xFF, 0, True, False) == ""
