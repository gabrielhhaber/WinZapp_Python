"""What WinZapp says before a recording on macOS. See winzapp_mac/mic_permission_mac.py."""

import types

from winzapp_mac import mic_permission_mac as mp


def _press(monkeypatch, status, presses=1):
    monkeypatch.setattr(mp, "authorization_status", lambda: status)
    monkeypatch.setattr(mp, "_told_about_prompt", False)
    events = []
    panel = types.SimpleNamespace(main_window=types.SimpleNamespace(
        output=events.append, i18n=types.SimpleNamespace(t=lambda key: key)))
    wrapped = mp._announced(lambda self: events.append("open"))
    for _ in range(presses):
        wrapped(panel)
    return events


def test_an_unanswered_prompt_is_announced_before_the_open_raises_it(monkeypatch):
    assert _press(monkeypatch, 0) == ["mac_mic_permission_waiting", "open"]


def test_a_stale_status_is_said_only_once_per_launch(monkeypatch):
    assert _press(monkeypatch, 0, presses=3) == [
        "mac_mic_permission_waiting", "open", "open", "open"]


def test_a_refusal_warns_and_never_stops_the_recording(monkeypatch):
    for status in (1, 2):
        assert _press(monkeypatch, status) == ["mac_mic_permission_denied", "open"]


def test_a_granted_permission_is_silent(monkeypatch):
    assert _press(monkeypatch, 3) == ["open"]


def test_an_unreadable_status_is_silent(monkeypatch):
    assert _press(monkeypatch, None) == ["open"]


def test_the_real_status_is_readable_on_this_mac():
    assert mp.authorization_status() in (0, 1, 2, 3)
