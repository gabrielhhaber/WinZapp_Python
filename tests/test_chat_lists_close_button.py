"""The lists manager's closing button speaks the user's language.

It came from CreateStdDialogButtonSizer(wx.CANCEL): wx's stock "Cancel", in
English in every locale and with no Alt+letter. The manager applies every
action at once, so the button closes the window (wa_lists_close); it is still
ID_CANCEL, so Escape keeps closing it. The letter is checked against the
other buttons in tests/test_chat_lists_mnemonics.py.
"""

import pytest
import wx

from core.i18n import I18n
from tests.conftest import destroy_now, hidden_frame
from ui.dialogs.chat_lists import WhatsAppListsDialog

# Creates a REAL top-level wx dialog - see the wxgui marker in pytest.ini.
pytestmark = pytest.mark.wxgui


class _Snapshot:
    lists = ()
    can_edit = True

    def find(self, list_id):
        return None


def _main_window():
    frame = hidden_frame()
    frame.settings = {}
    frame.i18n = I18n(frame)
    frame.i18n.get_language()
    frame._wa_lists_state = lambda: _Snapshot()
    frame._request_wa_lists = lambda complete, command=None: None
    frame.output = lambda *a, **k: None
    return frame


def test_the_close_button_is_translated_and_still_closes_with_escape(wx_app):
    frame = _main_window()
    dialog = WhatsAppListsDialog(frame)
    try:
        button = dialog._close_button
        assert button.GetId() == wx.ID_CANCEL
        assert button.GetLabel() == frame.i18n.t("wa_lists_close")
        assert button.GetLabel() != "wa_lists_close"
        assert "&" in button.GetLabel()
    finally:
        destroy_now(dialog)
