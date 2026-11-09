"""The Channels window comes back as it was left to open a channel."""
import types

import ui.dialogs.channels as dialogs
from ui.dialogs import channels_return
from ui.dialogs.channels_return import WindowState, restore, snapshot


class _Text:
    def __init__(self, value=""):
        self.value, self.events = value, 0

    def GetValue(self):
        return self.value

    def ChangeValue(self, value):       # no event, unlike SetValue
        self.value = value


class _Choice:
    def __init__(self, count, selection=0):
        self.count, self.selection = count, selection

    def GetSelection(self):
        return self.selection

    def SetSelection(self, index):
        self.selection = index

    def GetCount(self):
        return self.count


def _channel(jid):
    return types.SimpleNamespace(jid=jid, name=jid)


def _window(mode=dialogs.MINE, selected=None):
    shown = []
    window = types.SimpleNamespace(
        _mode=mode, _search=_Text(), _category=_Choice(8), _view=_Choice(4),
        _mine=[], _found=[], _loaded=False, _searched=False, shown=shown, synced=[])
    window._selected = lambda: selected
    window._sync_mode = lambda: window.synced.append(window._mode)
    window._show = lambda channels, keep="": shown.append(([c.jid for c in channels], keep))
    return window


def test_the_directory_comes_back_with_its_search_filters_and_selection():
    left = _window(dialogs.FIND, selected=_channel("2@newsletter"))
    left._search.value, left._category.selection, left._view.selection = "news", 4, 2
    left._mine = [_channel("1@newsletter")]
    left._found = [_channel("2@newsletter"), _channel("3@newsletter")]
    left._loaded = left._searched = True
    state = snapshot(left)

    back = _window()
    restore(back, state)
    assert back._mode == dialogs.FIND and back.synced == [dialogs.FIND]
    assert (back._search.value, back._category.selection, back._view.selection) == ("news", 4, 2)
    assert back._loaded and back._searched
    # the directory read before, at once, on the channel that was opened
    assert back.shown == [(["2@newsletter", "3@newsletter"], "2@newsletter")]
    assert back._keep_first == "2@newsletter" and back._restored is True


def test_my_channels_come_back_on_the_selected_one():
    left = _window(selected=_channel("1@newsletter"))
    left._mine = [_channel("0@newsletter"), _channel("1@newsletter")]
    back = _window()
    restore(back, snapshot(left))
    assert back._mode == dialogs.MINE
    assert back.shown == [(["0@newsletter", "1@newsletter"], "1@newsletter")]


def test_a_choice_out_of_range_is_left_as_it_is():
    back = _window()
    restore(back, WindowState(mode=dialogs.FIND, category=99, view=-1))
    assert (back._category.selection, back._view.selection) == (0, 0)


def test_the_module_agrees_with_the_window_on_the_directory_mode():
    assert channels_return.FIND == dialogs.FIND


def test_the_state_is_kept_when_a_channel_opens(monkeypatch):
    kept = WindowState(mode=dialogs.FIND, selected="2@newsletter")

    class Dialog:
        def __init__(self, parent, mw):
            self.result = _channel("2@newsletter")

        def ShowModal(self):
            return dialogs.wx.ID_OK

        def Destroy(self):
            pass
    monkeypatch.setattr(dialogs, "ChannelsDialog", Dialog)
    monkeypatch.setattr(dialogs, "snapshot", lambda dialog: kept)
    monkeypatch.setattr(dialogs, "open_group_chat", lambda mw, jid, name: None)
    mw = types.SimpleNamespace(i18n=types.SimpleNamespace(t=lambda key: key))
    assert dialogs.show_channels(mw, None) is True
    assert mw._channels_return_state is kept and mw._channel_opened_from_window == "2@newsletter"
