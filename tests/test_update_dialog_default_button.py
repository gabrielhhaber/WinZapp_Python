"""Tests that the "update available" dialogs default their focused/Enter
button to No, not Yes.

Both dialogs can pop up unprompted while the user is in the middle of typing
a message — WinZapp's own UpdateDialog (updater.py) and WppUpdateChecker's
wppconnect-server "newer version available" prompt. Space is how
NVDA/JAWS/Narrator users activate whatever control is currently focused, so
defaulting to Yes risked installing/reinstalling something from an accidental
keystroke made while composing, instead of a deliberate choice.
"""

import wx

import updater
from updater import UpdateDialog, WppUpdateChecker
from tests.conftest import hidden_frame
import pytest

# Creates a REAL top-level wx dialog - see the wxgui marker in pytest.ini.
pytestmark = pytest.mark.wxgui


class _FakeI18n:
    def t(self, key):
        return key


class TestUpdateDialogDefaultButton:
    def test_no_button_is_the_default_item(self, wx_app):
        frame = hidden_frame()
        frame.i18n = _FakeI18n()
        try:
            dlg = UpdateDialog(frame, "2026.01.01.0000", changelog="")
            try:
                assert dlg.GetDefaultItem() is dlg._no_btn
            finally:
                dlg.Destroy()
        finally:
            frame.Destroy()


class TestWppUpdatePromptDefaultButton:
    def test_message_box_is_shown_with_no_default(self, monkeypatch):
        calls = []

        def _fake_message_box(*args, **kwargs):
            calls.append((args, kwargs))
            return wx.NO

        monkeypatch.setattr(updater.wx, "MessageBox", _fake_message_box)

        checker = WppUpdateChecker.__new__(WppUpdateChecker)
        checker._mw = type(
            "MW",
            (),
            {
                "i18n": _FakeI18n(),
                # The prompt is gated on this — see WppUpdateChecker._prompt_update.
                "wpp_update_may_run_now": lambda self: True,
            },
        )()
        checker._retry_timer = None
        checker._schedule_retry = lambda *a: calls.append("scheduled_retry")

        checker._prompt_update("2.10.1", "2.10.4", "v2.10.4")

        assert len(calls) == 2
        style = calls[0][0][2]
        assert style & wx.NO_DEFAULT
        assert style & wx.YES_NO
        assert calls[1] == "scheduled_retry"
