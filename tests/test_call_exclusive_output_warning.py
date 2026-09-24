"""Ticking "exclusive speaker" asks first, and "No" unticks it.

Exclusive access to the speaker silences every other application on it for
the whole call, the screen reader included. The warning is a Yes/No box whose
default is No, so dismissing it by reflex leaves the option off. The handler
is bound onto a stub and wx.MessageBox is replaced, so no window is opened.
"""

import pytest
import wx

from ui import call_audio_options
from ui.call_audio_options import confirm_exclusive_output


class _I18n:
    def t(self, key):
        return key


class _MainWindow:
    i18n = _I18n()


class _Check:
    def __init__(self, value):
        self.value = value

    def SetValue(self, value):
        self.value = value


class _Event:
    def __init__(self, checked):
        self._checked = checked
        self.skipped = False

    def IsChecked(self):
        return self._checked

    def Skip(self):
        self.skipped = True


class _Dialog:
    def __init__(self, checked):
        self.main_window = _MainWindow()
        self._call_exclusive_output_check = _Check(checked)

    def _on_call_exclusive_output_toggle(self, event):
        confirm_exclusive_output(
            self, self.main_window.i18n, event, self._call_exclusive_output_check
        )


@pytest.fixture
def boxes(monkeypatch):
    shown = []

    def install(answer):
        def fake_box(message, caption, style, parent):
            shown.append((message, style))
            return answer
        monkeypatch.setattr(call_audio_options.wx, "MessageBox", fake_box)
        return shown

    return install


def test_answering_no_unticks_the_option(boxes):
    shown = boxes(wx.NO)
    dialog = _Dialog(checked=True)
    event = _Event(checked=True)

    dialog._on_call_exclusive_output_toggle(event)

    assert dialog._call_exclusive_output_check.value is False
    assert event.skipped is True
    message, style = shown[0]
    assert message == "calls_exclusive_output_warning"
    # No is the default button: dismissing by reflex is the safe outcome
    assert style & wx.YES_NO and style & wx.NO_DEFAULT


def test_answering_yes_keeps_the_option(boxes):
    boxes(wx.YES)
    dialog = _Dialog(checked=True)

    dialog._on_call_exclusive_output_toggle(_Event(checked=True))

    assert dialog._call_exclusive_output_check.value is True


def test_closing_the_box_any_other_way_unticks_it(boxes):
    boxes(wx.CANCEL)
    dialog = _Dialog(checked=True)

    dialog._on_call_exclusive_output_toggle(_Event(checked=True))

    assert dialog._call_exclusive_output_check.value is False


def test_unticking_asks_nothing(boxes):
    shown = boxes(wx.NO)
    dialog = _Dialog(checked=False)
    event = _Event(checked=False)

    dialog._on_call_exclusive_output_toggle(event)

    assert shown == []
    assert dialog._call_exclusive_output_check.value is False
    assert event.skipped is True
