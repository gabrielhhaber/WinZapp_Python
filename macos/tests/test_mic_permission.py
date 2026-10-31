"""What WinZapp says before a recording on macOS. See winzapp_mac/mic_permission_mac.py."""

import sys
import types

import pytest

from winzapp_mac import mic_permission_mac as mp


def _press(monkeypatch, answers, permissions=(mp.MICROPHONE, mp.COMPUTER_AUDIO), presses=1):
    monkeypatch.setattr(mp, "status", answers.get)
    events = []
    panel = types.SimpleNamespace(main_window=types.SimpleNamespace(
        output=events.append, i18n=types.SimpleNamespace(t=lambda key: key)))
    wrapped = mp._announced(lambda self: events.append("open"), permissions)
    for _ in range(presses):
        wrapped(panel)
    return events


MIC, AUDIO = mp.MICROPHONE[0], mp.COMPUTER_AUDIO[0]


def test_an_unanswered_prompt_is_announced_before_the_open_raises_it(monkeypatch):
    assert _press(monkeypatch, {MIC: 2}) == ["mac_mic_permission_waiting", "open"]


def test_computer_audio_has_its_own_sentences(monkeypatch):
    assert _press(monkeypatch, {MIC: 0, AUDIO: 2}) == ["mac_system_audio_permission_waiting", "open"]
    assert _press(monkeypatch, {MIC: 0, AUDIO: 1}) == ["mac_system_audio_permission_denied", "open"]


def test_only_the_first_problem_is_spoken(monkeypatch):
    assert _press(monkeypatch, {MIC: 1, AUDIO: 1}) == ["mac_mic_permission_denied", "open"]


def test_a_refusal_never_stops_the_recording(monkeypatch):
    assert _press(monkeypatch, {MIC: 1}, (mp.MICROPHONE,))[-1] == "open"


def test_granted_permissions_are_silent(monkeypatch):
    assert _press(monkeypatch, {MIC: 0, AUDIO: 0}, presses=2) == ["open", "open"]


def test_an_unreadable_status_is_silent(monkeypatch):
    assert _press(monkeypatch, {}) == ["open"]


@pytest.mark.skipif(sys.platform != "darwin", reason="reads the real macOS permission records")
@pytest.mark.parametrize("service", [MIC, AUDIO])
def test_the_real_status_is_readable_on_this_mac(service):
    assert isinstance(mp.status(service), int)
