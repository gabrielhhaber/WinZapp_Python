"""Esc closes WhatsNewDialog (Help > What's new, Help > What's new since you
last looked, and the updater's "What's new?" button).

The dialog's only button is wx.ID_CLOSE, and wx.Dialog's built-in Esc handling
looks for a wx.ID_CANCEL one, so Esc did nothing. WhatsNewDialog is a real
wx.Dialog and a plain pytest must never open a window, so the key handler is
called as an unbound method on a stub, with a fake key event.
"""

import wx

from updater import WhatsNewDialog


class _Event:
    def __init__(self, key):
        self._key = key
        self.skipped = False

    def GetKeyCode(self):
        return self._key

    def Skip(self):
        self.skipped = True


class _Stub:
    _on_char_hook = WhatsNewDialog._on_char_hook

    def __init__(self):
        self.ended_with = []

    def EndModal(self, code):
        self.ended_with.append(code)


class TestWhatsNewDialogEscape:
    def test_escape_ends_the_dialog_with_close(self):
        stub, event = _Stub(), _Event(wx.WXK_ESCAPE)

        stub._on_char_hook(event)

        assert stub.ended_with == [wx.ID_CLOSE]
        assert not event.skipped

    def test_other_keys_are_left_to_the_text(self):
        stub = _Stub()
        for key in (wx.WXK_TAB, wx.WXK_DOWN, wx.WXK_RETURN, ord("A")):
            event = _Event(key)
            stub._on_char_hook(event)
            assert event.skipped

        assert stub.ended_with == []
