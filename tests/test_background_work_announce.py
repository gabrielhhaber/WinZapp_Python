"""A panel of the main window (channels, communities) can be hidden when its
read returns: the user opened a chat while "Loading" was on. Its result then
goes to the status line only; it used to be spoken over the chat."""

import types
from unittest.mock import Mock

import ui.dialogs.channels as channels
from ui.dialogs.background_work import BackgroundWork
from ui.dialogs.channels import MINE, ChannelsPanel


def _panel(shown):
    spoken = []
    panel = types.SimpleNamespace(
        _status=types.SimpleNamespace(SetLabel=Mock()),
        _mw=types.SimpleNamespace(speak_output=types.SimpleNamespace(output=spoken.append)),
        IsShownOnScreen=lambda: shown)
    return panel, spoken


def test_the_result_is_spoken_only_while_the_panel_is_on_screen():
    shown, spoken = _panel(True)
    BackgroundWork._announce_if_shown(shown, "3 channels")
    assert spoken == ["3 channels"]
    shown._status.SetLabel.assert_called_once_with("3 channels")
    hidden, spoken = _panel(False)
    BackgroundWork._announce_if_shown(hidden, "3 channels")
    assert spoken == []
    hidden._status.SetLabel.assert_called_once_with("3 channels")


def test_a_channels_read_that_returns_while_hidden_is_not_spoken(monkeypatch):
    monkeypatch.setattr(channels, "remember_channel_roles", lambda mw, found: None)
    monkeypatch.setattr(channels.ch, "sorted_channels", lambda found: list(found))
    panel, spoken = _panel(False)
    panel._i18n = types.SimpleNamespace(t=lambda key: key)
    panel._current = lambda: True
    panel._set_busy = lambda busy: None
    panel._reload_again = False
    panel._mode = MINE
    panel._show = Mock()
    panel._announce_if_shown = types.MethodType(BackgroundWork._announce_if_shown, panel)
    ChannelsPanel._finish_reload(panel, [object()])
    assert spoken == []
    panel._status.SetLabel.assert_called_with("channels_count")
