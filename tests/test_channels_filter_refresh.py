"""Picking another category or list in the Channels window reads the directory
again. Reported: the lists loaded, but switching from one list to another kept
showing the same channels, since the choice was read only by Search or Enter;
measured over CDP, WhatsApp answers each list with different channels."""

import types

import pytest

import ui.dialogs.channels as dialogs
from ui.dialogs.channels import FIND, MINE, ChannelsPanel


class _Timer:
    made = []

    def __init__(self, delay, call):
        self.delay, self.call, self.running = delay, call, True
        _Timer.made.append(self)

    def IsRunning(self):
        return self.running

    def Stop(self):
        self.running = False

    def fire(self):
        self.running = False
        self.call()


@pytest.fixture
def window(monkeypatch):
    _Timer.made = []
    monkeypatch.setattr(dialogs.wx, "CallLater", _Timer)
    stub = types.SimpleNamespace(_mode=FIND, _filter_timer=None, _search_again=False,
                                 _busy=False, searches=0, FILTER_DELAY_MS=700)
    stub._current = lambda: True
    stub._on_search = lambda: setattr(stub, "searches", stub.searches + 1)
    for name in ("_on_filter_changed", "_filter_settled"):
        setattr(stub, name, types.MethodType(getattr(ChannelsPanel, name), stub))
    return stub


def test_a_new_choice_reads_the_directory_once_it_settles(window):
    window._on_filter_changed()
    window._on_filter_changed()          # arrowing on to the next item
    assert len(_Timer.made) == 2 and not _Timer.made[0].running
    _Timer.made[-1].fire()
    assert window.searches == 1


def test_my_channels_ignore_the_directory_filters(window):
    window._mode = MINE
    window._on_filter_changed()
    assert _Timer.made == []


def test_a_change_during_a_read_reads_again_after_it(window):
    window._busy = True
    window._on_filter_changed()
    _Timer.made[-1].fire()
    assert window.searches == 0 and window._search_again is True


def test_a_closed_window_reads_nothing(window):
    window._current = lambda: False
    window._on_filter_changed()
    _Timer.made[-1].fire()
    assert window.searches == 0


# ------------------------------------------- what waits for a busy window
def _busy_stub(monkeypatch, mode=FIND, found=()):
    monkeypatch.setattr(dialogs.wx, "CallAfter", lambda fn, *a: fn(*a))
    stub = types.SimpleNamespace(_busy=False, _reload_again=False, _search_again=False,
                                 _mode=mode, _found=list(found), reads=[])
    stub._current = lambda: True
    stub._update_buttons = lambda: None
    stub._show = lambda channels, keep="": None
    stub._sync_mode = lambda: None
    stub._mode_radio = types.SimpleNamespace(GetSelection=lambda: stub._mode)

    def read(kind):
        def start(*args, **kwargs):
            stub.reads.append(kind)
            stub._busy = True
        return start
    stub._reload = read("reload")
    stub._on_search = read("search")
    for name in ("_set_busy", "_run_pending", "_on_mode"):
        setattr(stub, name, types.MethodType(getattr(ChannelsPanel, name), stub))
    return stub


def test_a_category_picked_during_a_quiet_reload_is_read_after_it(monkeypatch):
    """A channels-changed reload was in flight when the filter timer fired:
    the directory kept showing the old category (only _finish_search looked
    at the flag)."""
    stub = _busy_stub(monkeypatch)
    stub._busy, stub._search_again = True, True
    stub._set_busy(False)                 # the reload answered
    assert stub.reads == ["search"] and stub._search_again is False


def test_a_change_announced_during_a_search_reloads_after_it_then_searches(monkeypatch):
    stub = _busy_stub(monkeypatch)
    stub._busy, stub._reload_again, stub._search_again = True, True, True
    stub._set_busy(False)
    assert stub.reads == ["reload"]       # one at a time
    stub._set_busy(False)                 # the reload answered
    assert stub.reads == ["reload", "search"]


def test_a_search_asked_for_in_my_channels_is_dropped(monkeypatch):
    stub = _busy_stub(monkeypatch, mode=MINE)
    stub._busy, stub._search_again = True, True
    stub._set_busy(False)
    assert stub.reads == []


def test_find_chosen_during_a_read_searches_after_it(monkeypatch):
    stub = _busy_stub(monkeypatch)
    stub._busy = True
    stub._on_mode()
    assert stub.reads == [] and stub._search_again is True
    stub._set_busy(False)
    assert stub.reads == ["search"]


# ---------------------------------------------------- channels-changed
def _dialog_stub():
    stub = types.SimpleNamespace(_busy=False, _reload_again=False, reloads=[])
    stub._current = lambda: True
    stub._reload = lambda keep="", quiet=False: stub.reloads.append(quiet)
    stub.on_channels_changed = types.MethodType(ChannelsPanel.on_channels_changed, stub)
    return stub


def test_a_change_announced_by_whatsapp_reloads_the_list_quietly():
    stub = _dialog_stub()
    stub.on_channels_changed()
    assert stub.reloads == [True]


def test_a_change_during_a_read_reloads_after_it():
    stub = _dialog_stub()
    stub._busy = True
    stub.on_channels_changed()
    assert stub.reloads == [] and stub._reload_again is True


def test_the_socket_hands_a_channel_change_to_the_panel_on_screen(monkeypatch):
    from core import websocket_client as wsc
    calls = []
    monkeypatch.setattr(wsc.wx, "CallAfter", lambda fn, *a: fn(*a))
    shown = [True]
    panel = types.SimpleNamespace(on_channels_changed=lambda: calls.append("reload"),
                                  IsShown=lambda: shown[0])
    people = types.SimpleNamespace(on_channels_changed=lambda jid: calls.append(("people", jid)))
    client = types.SimpleNamespace(main_window=types.SimpleNamespace(channels_panel=panel,
                                                                     _channel_people_dialog=people),
                                   _belongs_to_this_session=lambda data: data.get("session") == "me")
    for name in ("on_channels_changed", "_channels_changed_on_ui"):
        setattr(client, name, types.MethodType(getattr(wsc.WebSocketClient, name), client))
    client.on_channels_changed({"session": "me", "kind": "left", "id": "1@newsletter"})
    client.on_channels_changed({"session": "other", "kind": "left", "id": "1@newsletter"})
    client.on_channels_changed({"session": "me", "kind": "left", "id": "5511@c.us"})
    assert calls == ["reload", ("people", "1@newsletter")]
    # hidden: it reads the list again when shown (on_show), nothing now
    shown[0] = False
    client.main_window._channel_people_dialog = None
    client.on_channels_changed({"session": "me", "kind": "left", "id": "1@newsletter"})
    assert len(calls) == 2


def test_the_people_window_reloads_only_for_its_own_channel():
    from ui.dialogs.channel_admins import ChannelAdminsDialog
    loads = []
    stub = types.SimpleNamespace(_channel=types.SimpleNamespace(jid="1@newsletter"), _busy=False,
                                 _reload_again=False, _current=lambda: True,
                                 _load=lambda quiet=False: loads.append(quiet))
    stub.on_channels_changed = types.MethodType(ChannelAdminsDialog.on_channels_changed, stub)
    stub.on_channels_changed("2@newsletter")
    stub.on_channels_changed("1@newsletter")
    assert loads == [True]
    stub._busy = True
    stub.on_channels_changed("1@newsletter")
    assert loads == [True] and stub._reload_again is True


# ----------------------------------------- Esc goes back to the Channels panel
def test_esc_in_a_channel_opened_from_the_panel_shows_the_panel_again(monkeypatch):
    from main_window import content_panels
    shown = []
    monkeypatch.setattr(dialogs.wx, "CallAfter", lambda fn, *a: shown.append((fn, a)))
    mw = types.SimpleNamespace(_channel_opened_from_window="1@newsletter")
    assert dialogs.channel_closed(mw, "1@newsletter") is True
    # the same panel, as it was left, on that channel
    assert shown == [(content_panels.show_channels, (mw, "1@newsletter"))]
    assert mw._channel_opened_from_window == ""
    # used once: the next Esc in it goes to the chat list as usual
    assert dialogs.channel_closed(mw, "1@newsletter") is False


def test_any_other_conversation_closes_as_usual_and_forgets_the_mark(monkeypatch):
    monkeypatch.setattr(dialogs.wx, "CallAfter", lambda fn, *a: None)
    mw = types.SimpleNamespace(_channel_opened_from_window="1@newsletter")
    assert dialogs.channel_closed(mw, "5511@s.whatsapp.net") is False
    assert mw._channel_opened_from_window == ""
    assert dialogs.channel_closed(types.SimpleNamespace(), "1@newsletter") is False
