"""Communities (Alt+8) and Channels (Alt+9) are panels of the main window
since 2026-11-12, like Status and Calls (main_window/content_panels.py).

What changed with that: one content panel on screen at a time, through one
list of the side panels; a panel lives as long as the window, so it adopts a
new connection and drops answers asked on the old one
(BackgroundWork._renew_connection), and says its labels again in a new
language (ui/panel_labels.py). No window is created here.
"""
import threading
import types
from unittest.mock import Mock

from main_window import content_panels
from ui.dialogs.background_work import BackgroundWork
from ui.panel_labels import TranslatedLabels


class _Shown:
    def __init__(self, shown=False):
        self.shown = shown
        self.on_show = Mock()

    def Show(self, on=True):
        self.shown = on

    def Hide(self):
        self.shown = False


def _window(open_conversation=None):
    mw = types.SimpleNamespace(
        status_panel=_Shown(True), calls_panel=_Shown(), communities_panel=_Shown(),
        channels_panel=_Shown(), archived_conversations_panel=_Shown(True),
        locked_conversations_panel=_Shown(), lock_chat_vault=Mock(),
        content_panel=types.SimpleNamespace(Layout=Mock()),
        _channel_opened_from_window="1@newsletter")
    closed = []
    mw.conversations_panel = types.SimpleNamespace(
        conversation=open_conversation, Hide=Mock(),
        close_conversation_for_panel_switch=lambda: closed.append(1))
    return mw, closed


def test_every_side_panel_is_hidden_but_the_one_kept():
    mw, _closed = _window()
    mw.channels_panel.shown = True
    content_panels.hide_side_panels(mw, keep=mw.calls_panel)
    assert [p.shown for p in content_panels.side_panels(mw)] == [False, False, False, False]
    assert [name for name in content_panels.SIDE_PANELS] == [
        "status_panel", "calls_panel", "communities_panel", "channels_panel"]


def test_alt_8_shows_communities_alone_and_closes_the_conversation():
    mw, closed = _window(open_conversation={"remoteJid": "1@newsletter"})
    panel = content_panels.show_communities(mw)
    assert panel is mw.communities_panel and panel.shown and panel.on_show.called
    others = [mw.status_panel, mw.calls_panel, mw.channels_panel,
              mw.archived_conversations_panel, mw.locked_conversations_panel]
    assert not any(p.shown for p in others)
    mw.conversations_panel.Hide.assert_called_once()
    mw.lock_chat_vault.assert_called_once_with(silent=True, show_conversations=False)
    assert closed == [1] and mw.content_panel.Layout.called
    # a channel left for a panel does not come back to Channels on a later Esc
    assert mw._channel_opened_from_window == ""


def test_alt_9_shows_channels_on_the_channel_coming_back():
    mw, closed = _window()
    content_panels.show_channels(mw, keep="1@newsletter")
    mw.channels_panel.on_show.assert_called_once_with(keep="1@newsletter")
    assert closed == [] and mw._channel_opened_from_window == "1@newsletter"


def test_a_window_without_the_panel_does_nothing():
    mw, _closed = _window()
    del mw.channels_panel
    assert content_panels.show_channels(mw) is None
    mw.conversations_panel.Hide.assert_not_called()


# ------------------------------------------------ a panel and its connection
class _Worker(BackgroundWork):
    def __init__(self):
        self.mw = types.SimpleNamespace(i18n=None, wpp_server="http://127.0.0.1", wpp_port=6300,
                                        token="old")
        self._init_background(self.mw)


def test_a_new_connection_is_adopted_and_old_answers_dropped(monkeypatch):
    import ui.dialogs.background_work as bw
    queued = []
    monkeypatch.setattr(bw.wx, "CallAfter", lambda fn, *a: queued.append((fn, a)))
    worker, heard = _Worker(), []
    gate = threading.Event()

    def work():
        gate.wait(5)
        return "old answer"
    monkeypatch.setattr(bw.threading, "Thread", lambda target, daemon: types.SimpleNamespace(
        start=lambda: queued.append(("run", target))))
    worker._run_async(work, heard.append)
    assert worker._renew_connection() is False          # nothing changed yet
    worker.mw.token = "new"
    worker._busy = True
    assert worker._renew_connection() is True
    assert worker._connection[2] == "new" and worker._busy is False
    gate.set()
    _kind, run = queued.pop(0)
    run()                                               # the old request answers now
    for fn, args in queued:
        fn(*args)
    assert heard == []                                  # asked on the old connection


# ------------------------------------------------------- a new language
class _Control:
    def __init__(self):
        self.label, self.name, self.items = "", "", {}

    def SetLabel(self, text):
        self.label = text

    def SetName(self, text):
        self.name = text

    def SetString(self, index, text):
        self.items[index] = text


def test_every_kept_label_is_read_again():
    strings = {"a": "&Alpha", "b": "Beta", "c": "Gamma", "list": "&Channels"}
    panel = TranslatedLabels()
    panel._mw = types.SimpleNamespace(i18n=types.SimpleNamespace(t=lambda key: strings[key]))
    panel._init_labels()
    button, radio, listbox = _Control(), _Control(), _Control()
    listbox._label_control = _Control()
    panel._keep_label(button, "a")
    panel._keep_items(radio, ("b", "c"))
    panel._keep_labelled(listbox, "list")
    panel._relabel()
    assert button.label == "&Alpha" and radio.items == {0: "Beta", 1: "Gamma"}
    assert listbox._label_control.label == "&Channels" and listbox.name == "Channels"


# ------------------------------------------------ Communities when shown
def test_communities_say_their_count_the_first_time_only():
    from ui.dialogs.communities import CommunitiesPanel
    reloads = []
    panel = types.SimpleNamespace(_loaded=False, _refreshed={"x"},
                                  _list=types.SimpleNamespace(SetFocus=Mock()),
                                  _renew_connection=lambda: False,
                                  _reload=lambda quiet=False: reloads.append(quiet))
    CommunitiesPanel.on_show(panel)
    panel._loaded = True
    CommunitiesPanel.on_show(panel)
    assert reloads == [False, True] and panel._refreshed == {"x"}
    panel._renew_connection = lambda: True              # a new pairing
    CommunitiesPanel.on_show(panel)
    assert reloads[-1] is False and panel._refreshed == set()
