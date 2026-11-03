"""Simulated worker-stop/UI-delivery interleavings; no actual threads or windows."""
from types import SimpleNamespace
import pytest
import wx

from main_window.typing_feedback import stop_typing_sound
from tests.test_typing_feedback_integration import rig  # noqa: F401


@pytest.fixture
def worker_stop(rig, monkeypatch):
    queue, on_ui = [], [True]
    monkeypatch.setattr(wx, "IsMainThread", lambda: on_ui[0])
    monkeypatch.setattr(wx, "CallAfter", lambda f, *a, **k: queue.append(lambda: f(*a, **k)))
    rig.presence("composing")
    controller = rig.window._typing_sound_cadence
    timer = controller.timer
    original_stop = timer.Stop
    def ui_stop():
        assert on_ui[0], "native wx.Timer.Stop must execute on the UI thread"
        original_stop()
    timer.Stop = ui_stop
    rig.queue = queue
    rig.on_ui = on_ui
    rig.old_timer = timer
    return rig


def test_worker_stop_fences_callbacks_immediately_but_retires_native_timer_on_ui(worker_stop):
    r = worker_stop
    r.on_ui[0] = False
    stop_typing_sound(r.window)
    assert r.window._typing_sound_cadence.timer is None
    assert not r.old_timer.stopped
    assert len(r.queue) == 1
    r.on_ui[0] = True
    r.old_timer.callback()
    assert r.played == [0]
    r.queue.pop(0)()
    assert r.old_timer.stopped


def test_delayed_native_cleanup_does_not_stop_a_successor_using_the_same_sound(worker_stop):
    r = worker_stop
    r.on_ui[0] = False
    stop_typing_sound(r.window)
    r.on_ui[0] = True
    r.presence("composing")
    successor = r.window._typing_sound_cadence.timer
    stopped_before = len(r.stopped)
    r.queue.pop(0)()
    assert successor is r.window._typing_sound_cadence.timer
    assert not successor.stopped
    assert len(r.stopped) == stopped_before
    assert r.old_timer.stopped
