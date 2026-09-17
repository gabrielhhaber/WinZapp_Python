"""Tests for ui.dialogs.checkbox_confirm and the two confirmations built on it.

CheckboxConfirmDialog is replaced by a fake, so nothing is ever shown on the
desktop (see tests/test_no_desktop_visible_windows.py for why that matters).
The real dialog is covered by test_checkbox_confirm_dialog_gui.py, in CI only.
"""

import ast

import pytest
import wx

from ui.dialogs import checkbox_confirm, clear_chat_confirm


class _FakeDialog:
    # Per-test behaviour, overridden with monkeypatch.setattr on the class.
    result = wx.ID_YES
    checked_on_close = True
    raise_on_show = False
    instances = []

    def __init__(self, parent, message, title, checkbox_label, yes_label, no_label,
                 *, checked, default_yes):
        self.args = (parent, message, title, checkbox_label, yes_label, no_label)
        self.checked = checked
        self.default_yes = default_yes
        self.destroyed = False
        _FakeDialog.instances.append(self)

    def ShowModal(self):
        if self.raise_on_show:
            raise RuntimeError("boom")
        return self.result

    def is_checked(self):
        assert not self.destroyed, "checkbox read after Destroy()"
        return self.checked_on_close

    def Destroy(self):
        self.destroyed = True


@pytest.fixture
def fake_dialog(monkeypatch):
    monkeypatch.setattr(_FakeDialog, "instances", [])
    monkeypatch.setattr(checkbox_confirm, "CheckboxConfirmDialog", _FakeDialog)
    return _FakeDialog


def _ask(**kw):
    kw.setdefault("checked", False)
    kw.setdefault("default_yes", False)
    return checkbox_confirm.confirm_with_checkbox(
        None, "m", "t", "l", yes_label="y", no_label="n", **kw,
    )


class TestConfirmWithCheckbox:
    def test_every_label_and_option_reaches_the_dialog(self, fake_dialog):
        checkbox_confirm.confirm_with_checkbox(
            None, "msg", "title", "box", yes_label="&Sim", no_label="&Não",
            checked=True, default_yes=False,
        )

        dlg, = fake_dialog.instances
        assert dlg.args[1:] == ("msg", "title", "box", "&Sim", "&Não")
        assert dlg.checked is True
        assert dlg.default_yes is False
        assert dlg.destroyed

    def test_yes_returns_the_checkbox_state(self, fake_dialog, monkeypatch):
        assert _ask() == (True, True)
        monkeypatch.setattr(fake_dialog, "checked_on_close", False)
        assert _ask() == (True, False)

    def test_no_is_not_confirmed(self, fake_dialog, monkeypatch):
        monkeypatch.setattr(fake_dialog, "result", wx.ID_NO)

        confirmed, _ = _ask()

        assert confirmed is False

    def test_the_dialog_is_destroyed_even_when_showing_it_raises(self, fake_dialog, monkeypatch):
        monkeypatch.setattr(fake_dialog, "raise_on_show", True)

        with pytest.raises(RuntimeError):
            _ask()

        dlg, = fake_dialog.instances
        assert dlg.destroyed

    def test_it_is_not_the_task_dialog_checkbox_again(self):
        """RichMessageDialog's checkbox was read by NVDA as read-only and
        unchecked; the confirmation must stay on a plain wx.CheckBox."""
        with open(checkbox_confirm.__file__, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        called = {
            getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        }
        assert "CheckBox" in called
        assert "RichMessageDialog" not in called
        assert "ShowCheckBox" not in called


class TestClearChatConfirmation:
    def test_keep_starred_starts_ticked_and_yes_is_the_default(self, fake_dialog):
        result = clear_chat_confirm.confirm_clear_chat(
            None, "m", "t", "keep", yes_label="y", no_label="n",
        )

        dlg, = fake_dialog.instances
        assert dlg.checked is True
        assert dlg.default_yes is True
        assert dlg.args[3] == "keep"
        assert result == (True, True)
