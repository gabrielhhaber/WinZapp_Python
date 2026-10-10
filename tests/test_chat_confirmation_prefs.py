"""The clear/delete chat confirmations and their "don't show again" boxes.

The dialog class is replaced by a fake, so nothing is ever shown on the
desktop (see tests/test_no_desktop_visible_windows.py).
"""

import pytest
import wx

from ui.dialogs import checkbox_confirm, clear_chat_confirm, delete_chat_confirm


class _FakeDialog:
    result = wx.ID_YES
    keep_on_close = True
    extra_on_close = False
    instances = []

    def __init__(self, parent, message, title, checkbox_label, yes_label, no_label,
                 *, checked, default_yes, extra_label=None):
        self.extra_label = extra_label
        self.checked = checked
        self.default_yes = default_yes
        _FakeDialog.instances.append(self)

    def ShowModal(self):
        return self.result

    def is_checked(self):
        return self.keep_on_close

    def is_extra_checked(self):
        return self.extra_on_close

    def Destroy(self):
        pass


class _Window:
    def __init__(self, **ui):
        self.settings = {"user_interface": dict(ui)}
        self.saved = 0

    def save_settings(self):
        self.saved += 1


@pytest.fixture
def fake_dialog(monkeypatch):
    monkeypatch.setattr(_FakeDialog, "instances", [])
    monkeypatch.setattr(_FakeDialog, "result", wx.ID_YES)
    monkeypatch.setattr(_FakeDialog, "extra_on_close", False)
    monkeypatch.setattr(_FakeDialog, "keep_on_close", True)
    monkeypatch.setattr(checkbox_confirm, "CheckboxConfirmDialog", _FakeDialog)
    return _FakeDialog


def _clear(window):
    return clear_chat_confirm.confirm_clear_chat(
        None, "m", "t", "keep", yes_label="y", no_label="n",
        main_window=window, dont_ask_label="never",
    )


def _delete(window):
    return delete_chat_confirm.confirm_delete_chat(
        None, window, "m", "t", "never", yes_label="y", no_label="n",
    )


class TestClearChat:
    def test_dialog_offers_an_unticked_dont_ask_box(self, fake_dialog):
        _clear(_Window())

        dlg, = fake_dialog.instances
        assert dlg.extra_label == "never"
        assert dlg.checked is True and dlg.default_yes is True

    def test_dont_ask_with_yes_turns_the_question_off_and_saves(self, fake_dialog, monkeypatch):
        monkeypatch.setattr(fake_dialog, "extra_on_close", True)
        window = _Window()

        assert _clear(window) == (True, True)

        assert window.settings["user_interface"]["confirm_clear_chat"] is False
        assert window.saved == 1

    def test_dont_ask_with_no_changes_nothing(self, fake_dialog, monkeypatch):
        monkeypatch.setattr(fake_dialog, "result", wx.ID_NO)
        monkeypatch.setattr(fake_dialog, "extra_on_close", True)
        window = _Window()

        assert _clear(window)[0] is False

        assert "confirm_clear_chat" not in window.settings["user_interface"]
        assert window.saved == 0

    def test_when_off_it_clears_without_a_dialog_keeping_starred(self, fake_dialog):
        assert _clear(_Window(confirm_clear_chat=False)) == (True, True)
        assert fake_dialog.instances == []

    def test_without_a_window_it_is_the_plain_keep_starred_dialog(self, fake_dialog):
        clear_chat_confirm.confirm_clear_chat(
            None, "m", "t", "keep", yes_label="y", no_label="n",
        )
        dlg, = fake_dialog.instances
        assert dlg.extra_label is None


class TestDeleteChat:
    def test_yes_deletes_and_remembers_nothing_when_unticked(self, fake_dialog, monkeypatch):
        monkeypatch.setattr(fake_dialog, "keep_on_close", False)
        window = _Window()

        assert _delete(window) is True

        assert window.saved == 0
        dlg, = fake_dialog.instances
        assert dlg.checked is False and dlg.default_yes is True

    def test_dont_ask_with_yes_turns_the_question_off(self, fake_dialog, monkeypatch):
        monkeypatch.setattr(fake_dialog, "keep_on_close", True)
        window = _Window()

        assert _delete(window) is True

        assert window.settings["user_interface"]["confirm_delete_chat"] is False
        assert window.saved == 1

    def test_dont_ask_with_no_does_not_delete_or_remember(self, fake_dialog, monkeypatch):
        monkeypatch.setattr(fake_dialog, "result", wx.ID_NO)
        monkeypatch.setattr(fake_dialog, "keep_on_close", True)
        window = _Window()

        assert _delete(window) is False

        assert window.saved == 0

    def test_when_off_it_deletes_without_a_dialog(self, fake_dialog):
        assert _delete(_Window(confirm_delete_chat=False)) is True
        assert fake_dialog.instances == []

    def test_missing_setting_means_ask(self, fake_dialog):
        _delete(_Window())
        assert len(fake_dialog.instances) == 1
