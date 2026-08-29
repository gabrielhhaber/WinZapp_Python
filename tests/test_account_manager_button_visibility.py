"""Tests for AccountManagerDialog._update_button_visibility().

Reported live: "Abrir"/"Conectar" stayed visible for the account already
running this process (clicking either was a guaranteed no-op/error),
"Arquivar" stayed visible for an account already archived, and "Restaurar"
showed even for a normal paired account it can never apply to. Reuses the
same can_open/can_pair/can_archive guards the button handlers already
validate against, so a visible button is guaranteed to actually do
something instead of popping an error dialog after the click.

AccountManagerDialog is a plain object wrapping wx widgets (not a wx.Dialog
subclass) and needs a running wx.App for its buttons; the method under test
is bound onto a plain stub carrying real wx.Button instances — same
approach as the rest of this test suite (e.g.
tests/test_contact_action_buttons.py).
"""

import wx

from account_ui import AccountManagerDialog
from tests.conftest import hidden_frame


def _acc(aid, state="paired"):
    return {"id": aid, "state": state, "name": aid}


CURRENT = "current-account"
OTHER_PAIRED = "other-paired"
OTHER_PENDING = "other-pending"
OTHER_ARCHIVED = "other-archived"


class _Stub:
    _update_button_visibility = AccountManagerDialog._update_button_visibility
    _selected = AccountManagerDialog._selected

    def __init__(self, frame, rows, selected_index):
        self.current = CURRENT
        self._rows = rows
        self._selected_index = selected_index
        self.lst = type("List", (), {
            "GetFirstSelected": lambda self=None: selected_index,
        })()
        panel = wx.Panel(frame)
        sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_open = wx.Button(panel, label="open")
        self.btn_pair = wx.Button(panel, label="pair")
        self.btn_archive = wx.Button(panel, label="archive")
        self.btn_restore = wx.Button(panel, label="restore")
        for b in (self.btn_open, self.btn_pair, self.btn_archive, self.btn_restore):
            sizer.Add(b)
        panel.SetSizer(sizer)


class TestAccountManagerButtonVisibility:
    def test_current_account_hides_open_and_pair(self, wx_app):
        frame = hidden_frame()
        try:
            stub = _Stub(frame, [_acc(CURRENT)], 0)
            stub._update_button_visibility()
            assert stub.btn_open.IsShown() is False
            assert stub.btn_pair.IsShown() is False
        finally:
            frame.Destroy()

    def test_other_paired_account_shows_open_but_not_pair_or_restore(self, wx_app):
        frame = hidden_frame()
        try:
            stub = _Stub(frame, [_acc(OTHER_PAIRED)], 0)
            stub._update_button_visibility()
            assert stub.btn_open.IsShown() is True
            assert stub.btn_pair.IsShown() is False
            assert stub.btn_archive.IsShown() is True
            assert stub.btn_restore.IsShown() is False
        finally:
            frame.Destroy()

    def test_other_pending_account_shows_open_and_pair(self, wx_app):
        frame = hidden_frame()
        try:
            stub = _Stub(frame, [_acc(OTHER_PENDING, state="pending")], 0)
            stub._update_button_visibility()
            assert stub.btn_open.IsShown() is True
            assert stub.btn_pair.IsShown() is True
            assert stub.btn_archive.IsShown() is False  # archive is paired-only
        finally:
            frame.Destroy()

    def test_archived_account_shows_restore_but_not_archive(self, wx_app):
        frame = hidden_frame()
        try:
            stub = _Stub(frame, [_acc(OTHER_ARCHIVED, state="archived")], 0)
            stub._update_button_visibility()
            assert stub.btn_archive.IsShown() is False
            assert stub.btn_restore.IsShown() is True
        finally:
            frame.Destroy()

    def test_no_selection_hides_every_conditional_button(self, wx_app):
        frame = hidden_frame()
        try:
            stub = _Stub(frame, [_acc(OTHER_PAIRED)], -1)
            stub._update_button_visibility()
            for b in (stub.btn_open, stub.btn_pair, stub.btn_archive, stub.btn_restore):
                assert b.IsShown() is False
        finally:
            frame.Destroy()
