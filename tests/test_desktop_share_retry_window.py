"""The mixin's side of the desktop share's one automatic retry: when it is
scheduled, and what the delayed re-share may say (called on a plain stub)."""
import types

from core.call_desktop_audio import NVDA_EXITED_REASON
from main_window import call_desktop_audio as ui
from main_window.call_desktop_audio import DesktopCallAudioMixin


class Window(DesktopCallAudioMixin):
    def __init__(self, call, audio, allowed=True):
        self._active_voice_call, self._call_audio_session = call, audio
        self._shutting_down = False
        self._allowed = allowed
        self.i18n = types.SimpleNamespace(t=lambda key: key)
        self.spoken, self.started = [], []

    def _call_desktop_audio_allowed(self, call, audio):
        return self._allowed

    def _start_call_desktop_audio(self, audio, settings, *, announce):
        self.started.append((settings, announce))

    def _sync_voice_call_bar(self):
        pass

    def output(self, text, interrupt=False):
        self.spoken.append(text)


def share():
    call = {"call_id": "C"}
    audio = types.SimpleNamespace(running=True, desktop_audio=None)
    return call, audio


def test_the_delayed_re_share_starts_quietly_while_the_call_is_ours():
    call, audio = share()
    window = Window(call, audio)
    window._retry_desktop_share(call, audio, "S")
    assert window.started == [("S", False)] and window.spoken == []


def test_the_delayed_re_share_says_failed_when_the_call_cannot_share_now():
    call, audio = share()
    window = Window(call, audio, allowed=False)
    window._retry_desktop_share(call, audio, "S")
    assert window.started == [] and window.spoken == ["call_desktop_failed"]


def test_the_delayed_re_share_is_silent_after_hang_up_shutdown_or_a_manual_share():
    call, audio = share()
    gone = Window(None, audio, allowed=False)
    closing = Window(call, audio, allowed=False)
    closing._shutting_down = True
    other_session = Window(call, types.SimpleNamespace(desktop_audio=None), allowed=False)
    manual_audio = types.SimpleNamespace(running=True, desktop_audio=object())  # works already
    manual = Window(call, manual_audio)
    for window, shared in ((gone, audio), (closing, audio), (other_session, audio),
                           (manual, manual_audio)):
        window._retry_desktop_share(call, shared, "S")
        assert window.spoken == [] and window.started == []


def test_the_pin_death_schedules_one_delayed_retry_and_only_one(monkeypatch):
    call, audio = share()
    window = Window(call, audio)
    scheduled = []
    monkeypatch.setattr(ui.wx, "CallLater", lambda ms, fn, *args: scheduled.append((ms, fn, args)))
    worker = types.SimpleNamespace(failure_reason=f"WasapiError: {NVDA_EXITED_REASON}", settings="S")
    assert window._maybe_retry_desktop_share(audio, worker) is True
    assert scheduled[0][1] == window._retry_desktop_share and scheduled[0][2] == (call, audio, "S")
    assert window._maybe_retry_desktop_share(audio, worker) is False     # one per manual share
    assert len(scheduled) == 1
