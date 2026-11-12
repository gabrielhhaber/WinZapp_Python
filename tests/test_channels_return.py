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
    fetched = []
    monkeypatch.setattr(dialogs, "fetch_channel_updates", lambda mw, jid: fetched.append(jid))
    mw = types.SimpleNamespace(i18n=types.SimpleNamespace(t=lambda key: key))
    assert dialogs.show_channels(mw, None) is True
    assert mw._channels_return_state is kept and mw._channel_opened_from_window == "2@newsletter"
    # its updates are read at once, not by the next sync round
    assert fetched == ["2@newsletter"]


# ------------------------------------- only the user's close goes back
def _closing_panel(monkeypatch, shown):
    import ui.conversation_panel.conversation_navigation as nav
    from ui.conversations import ConversationsPanel
    # one wx module for both: what was scheduled tells them apart
    monkeypatch.setattr(nav.wx, "CallAfter", lambda fn, *a: shown.append(
        ("channels" if fn is dialogs.show_channels else "list", a)))
    mw = types.SimpleNamespace(_channel_opened_from_window="1@newsletter",
                               _channels_return_state=object())
    panel = types.SimpleNamespace(main_window=mw, _conversation_origin=None,
                                  _close_conversation_core=lambda: (True, "1@newsletter"),
                                  _restore_conversation_selection=lambda: None)
    panel.close_conversation = types.MethodType(ConversationsPanel.close_conversation, panel)
    return panel, mw


def test_esc_or_ctrl_w_on_the_channel_goes_back_to_the_channels_window(monkeypatch):
    shown = []
    panel, _mw = _closing_panel(monkeypatch, shown)
    panel.close_conversation(None, from_user=True)
    assert [kind for kind, _a in shown] == ["channels"]


def test_winzapp_closing_the_channel_never_brings_the_window_back(monkeypatch):
    """A full resync (F5), an account switch, or the channel archived or
    deleted close the conversation too: the Channels window popped up,
    modal, while the database was being wiped."""
    shown = []
    panel, mw = _closing_panel(monkeypatch, shown)
    panel.close_conversation()
    assert [kind for kind, _a in shown] == ["list"]
    assert mw._channel_opened_from_window == "" and mw._channels_return_state is None


def test_ctrl_w_in_the_conversation_is_the_users_close(monkeypatch):
    """The conversation's own Ctrl+W called close_conversation(event) with
    no from_user, and landed on the chat list."""
    from ui.conversations import ConversationsPanel
    shown = []
    panel, _mw = _closing_panel(monkeypatch, shown)
    ConversationsPanel._on_ctrl_w_conversation(panel, None)
    assert [kind for kind, _a in shown] == ["channels"]
