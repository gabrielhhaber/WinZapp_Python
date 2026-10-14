"""BackgroundWork: an answer reaches a dialog only while it is open, on the
same connection, and not while WinZapp is shutting down."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ui.dialogs import background_work
from ui.dialogs.background_work import BackgroundWork


class _Now:
    def __init__(self, target=None, daemon=None):
        self.target = target

    def start(self):
        self.target()


class _Dialog(BackgroundWork):
    def __init__(self):
        self.mw = SimpleNamespace(i18n=SimpleNamespace(t=str), wpp_server="http://127.0.0.1",
                                  wpp_port=6300, token="session:token")
        self._init_background(self.mw)
        self.EndModal = Mock()


@pytest.fixture
def later(monkeypatch):
    queued = []
    monkeypatch.setattr(background_work.threading, "Thread", _Now)
    monkeypatch.setattr(background_work.wx, "CallAfter", lambda fn, *args: queued.append((fn, args)))
    return queued


def _run_queue(queued):
    while queued:
        fn, args = queued.pop(0)
        fn(*args)


def test_an_open_dialog_hears_the_answer(later):
    dialog, heard = _Dialog(), []
    dialog._run_async(lambda: 42, heard.append)
    _run_queue(later)
    assert heard == [42]


def test_work_that_raises_answers_none(later):
    dialog, heard = _Dialog(), []

    def boom():
        raise RuntimeError("x")

    dialog._run_async(boom, heard.append)
    _run_queue(later)
    assert heard == [None]


def test_a_dialog_closed_before_the_answer_arrives_hears_nothing(later):
    dialog, heard = _Dialog(), []
    dialog._run_async(lambda: 42, heard.append)
    dialog._on_close(None)          # closed while the answer waits for the wx thread
    _run_queue(later)
    assert heard == []
    dialog.EndModal.assert_called_once()


def test_a_dialog_closed_before_the_work_ends_queues_nothing(later):
    dialog, heard = _Dialog(), []

    def work():
        dialog._closed.set()
        return 42

    dialog._run_async(work, heard.append)
    assert later == [] and heard == []


def test_the_window_destroyed_counts_as_closed():
    dialog = _Dialog()
    event = SimpleNamespace(GetEventObject=lambda: dialog, Skip=Mock())
    dialog._on_destroy(event)
    assert dialog._closed.is_set()
    event.Skip.assert_called_once()
    other = _Dialog()
    other._on_destroy(SimpleNamespace(GetEventObject=lambda: object(), Skip=Mock()))
    assert not other._closed.is_set()   # a child's destruction is not the dialog's


def test_current_only_on_the_same_connection_and_not_while_shutting_down():
    dialog = _Dialog()
    assert dialog._current()
    dialog.mw.token = "another-session:token"
    assert not dialog._current()
    dialog.mw.token = "session:token"
    dialog.mw._shutting_down = True
    assert not dialog._current()


def test_a_thread_that_cannot_start_answers_none_at_once(monkeypatch):
    dialog, heard = _Dialog(), []

    def refuse(*args, **kwargs):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(background_work.threading, "Thread", refuse)
    dialog._run_async(lambda: 42, heard.append)
    assert heard == [None]
