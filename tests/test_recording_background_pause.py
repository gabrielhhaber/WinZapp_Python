"""Background work stands down while an audio capture is open.

A recording wants the machine to itself for the same reason a call does: a
full chat-sync pass landing mid-take starved the audio path — log 2026-11-17,
15:40 — voice was lost (mic 163 late packets / 1.6 s, 3 discontinuities) while
the sync ran straight through the recording. ``_voice_call_in_progress()``
covers both now so every call site (poll, backfill, deep backfill, media sweep,
media download, status announcements, background update) pauses during a take,
not only during a call.

Bounded like the call gate: a panel that forgets to clear its recording flag
can never pause the poll forever. The same 2 h clock bounds call and capture.
"""
import time

import pytest

wx = pytest.importorskip("wx", reason="MainWindow imports require wx")

from main import MainWindow


class Panel:
    def __init__(self, is_recording=False, starting=False, system_audio=False):
        self._is_recording = is_recording
        self._recording_starting = starting
        self._recording_system_audio = system_audio


class Stub:
    _VOICE_CALL_PAUSE_MAX_SECONDS = 2 * 3600
    _voice_call_pause_since = 0.0
    _active_voice_call = None

    # ── bound for real: the decisions under test ─────────────────────
    _VOICE_CALL_PAUSE_MAX_SECONDS = MainWindow._VOICE_CALL_PAUSE_MAX_SECONDS
    _audio_capture_in_progress = MainWindow._audio_capture_in_progress
    _voice_call_in_progress = MainWindow._voice_call_in_progress

    def __init__(self, panel=None):
        self.conversations_panel = panel
        self._voice_call_pause_since = 0.0
        self._active_voice_call = None
        self.archived_conversations_panel = None
        self.locked_conversations_panel = None


def test_no_capture_and_no_call_allows_background_work():
    stub = Stub(Panel())
    assert stub._voice_call_in_progress() is False


def test_open_voice_recording_stands_background_work_down():
    stub = Stub(Panel(is_recording=True))
    assert stub._voice_call_in_progress() is True


def test_starting_a_recording_stands_background_work_down():
    stub = Stub(Panel(starting=True))
    assert stub._voice_call_in_progress() is True


def test_mixed_system_audio_recording_stands_background_work_down():
    stub = Stub(Panel(system_audio=True))
    assert stub._voice_call_in_progress() is True


def test_recording_flag_from_another_panel_stands_background_work_down():
    stub = Stub(Panel())
    stub.archived_conversations_panel = Panel(is_recording=True)
    assert stub._voice_call_in_progress() is True
    stub = Stub(Panel())
    stub.locked_conversations_panel = Panel(starting=True)
    assert stub._voice_call_in_progress() is True


def test_capture_flag_still_bounds_after_two_hours():
    stub = Stub(Panel(is_recording=True))
    # Pretend the flag has been stuck for just over the bound — a leaked
    # recording flag must not pause the poll for the rest of the session.
    stub._voice_call_pause_since = time.monotonic() - 2 * 3600 - 1
    assert stub._voice_call_in_progress() is False


def test_recording_ending_restores_the_clock_zero():
    stub = Stub(Panel(is_recording=True))
    assert stub._voice_call_in_progress() is True
    stub.conversations_panel = Panel()
    stub._active_voice_call = None
    # Once nothing is capturing, the clock resets and the answer is False.
    assert stub._voice_call_in_progress() is False
    assert stub._voice_call_pause_since == 0.0


def test_an_active_call_still_stands_work_down_with_no_panel_recording():
    stub = Stub(Panel())
    stub._active_voice_call = {"callId": "x"}
    assert stub._voice_call_in_progress() is True
