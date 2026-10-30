"""Wake from sleep on the Mac (wake_mac), without AppKit."""

import os
import sys
from types import SimpleNamespace

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path[:0] = [os.path.join(ROOT, "macos"), os.path.join(ROOT, "client"), os.path.join(ROOT, ".pydeps")]

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS layer")

from winzapp_mac import wake_mac  # noqa: E402


def _window(**flags):
    calls = []
    return SimpleNamespace(
        _reset_connection_state_for_resume=lambda: calls.append("reset"),
        _recover_from_suspend=lambda: calls.append("recover"),
        calls=calls, **flags)


def test_wake_resets_the_state_before_recovering():
    window = _window()
    wake_mac.on_wake(window)
    assert window.calls == ["reset", "recover"]


@pytest.mark.parametrize("flag", ["_user_offline", "_wpp_updating", "_shutting_down"])
def test_wake_leaves_a_paused_health_check_alone(flag):
    window = _window(**{flag: True})
    wake_mac.on_wake(window)
    assert window.calls == []


def test_a_failed_recovery_does_not_escape():
    window = _window()

    def boom():
        raise RuntimeError("no")
    window._recover_from_suspend = boom
    wake_mac.on_wake(window)


def test_watch_observes_the_workspace_wake_only_once(monkeypatch):
    added = []
    center = SimpleNamespace(addObserverForName_object_queue_usingBlock_=lambda *a: added.append(a) or object())
    monkeypatch.setattr(wake_mac, "_center", lambda: center)
    monkeypatch.setattr(wake_mac, "_observer", None)
    wake_mac.watch(_window())
    wake_mac.watch(_window())
    assert len(added) == 1
    assert added[0][0] == "NSWorkspaceDidWakeNotification"
