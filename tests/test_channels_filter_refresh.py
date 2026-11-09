"""Picking another category or list in the Channels window reads the directory
again. Reported: the lists loaded, but switching from one list to another kept
showing the same channels, since the choice was read only by Search or Enter;
measured over CDP, WhatsApp answers each list with different channels."""

import types

import pytest

import ui.dialogs.channels as dialogs
from ui.dialogs.channels import FIND, MINE, ChannelsDialog


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
        setattr(stub, name, types.MethodType(getattr(ChannelsDialog, name), stub))
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
