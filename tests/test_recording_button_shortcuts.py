"""The recording buttons announce their own shortcut to the screen reader.

All three used to look the configured key up under the Ctrl+R ids, so with a
main window to ask, the
computer-audio one (Ctrl+Shift+H) both announced Ctrl+R, although their own
keys worked.
"""

import pytest
import wx

from ui import shortcut_bindings, shortcut_names
from ui.accessible import AccessibleRecordVoiceMessage

_NAMES = {"shortcut_key_control": "Ctrl", "shortcut_key_shift": "Shift",
          "shortcut_key_alt": "Alt"}


class _I18n:
    @staticmethod
    def t(key):
        return _NAMES.get(key, key)


class _Window:
    def __init__(self, configured=None):
        self.settings = {shortcut_bindings.SECTION: configured or {}}
        self.i18n = _I18n()


@pytest.fixture
def window(monkeypatch):
    window = _Window()
    monkeypatch.setattr(shortcut_bindings, "main_window_for", lambda owner: window)
    # The OS keyboard layout names the letter otherwise; the test wants "G".
    monkeypatch.setattr(shortcut_names, "layout_key_name", lambda key: None)
    return window


def _announced(accessible):
    status, text = accessible.GetKeyboardShortcut(0)
    assert status == wx.ACC_OK
    return text


def test_the_computer_audio_button_says_its_own_key(window):
    assert _announced(AccessibleRecordVoiceMessage(
        "Ctrl+Shift+H", ("messages.ID_CTRL_SHIFT_H",))) == "Ctrl+Shift+H"


def test_the_plain_recording_button_still_says_ctrl_r(window):
    assert _announced(AccessibleRecordVoiceMessage("Ctrl+R")) == "Ctrl+R"


def test_a_key_the_user_changed_is_the_one_announced(window):
    window.settings[shortcut_bindings.SECTION] = {
        "messages.ID_CTRL_SHIFT_H": [wx.ACCEL_CTRL | wx.ACCEL_ALT, ord("J")]}
    assert _announced(AccessibleRecordVoiceMessage(
        "Ctrl+Shift+H", ("messages.ID_CTRL_SHIFT_H",))) == "Ctrl+Alt+J"


def test_without_a_main_window_the_written_key_is_said(monkeypatch):
    monkeypatch.setattr(shortcut_bindings, "main_window_for", lambda owner: None)
    assert _announced(AccessibleRecordVoiceMessage(
        "Ctrl+Shift+H", ("messages.ID_CTRL_SHIFT_H",))) == "Ctrl+Shift+H"
