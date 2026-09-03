"""ConversationsPanel._toggle_playback()'s "same item" branch (toggling
play/pause on the message that is already loaded) had no recovery path for
a dead BASS channel — unlike _play_audio() (opening a brand new message),
which already reopens a fresh stream on a play() failure.

An output device switch in Settings (SoundSystem.apply_output_device())
frees + reinits the single process-wide BASS device, invalidating every
stream created before it — including whatever voice/video message was
already loaded. Toggling play/pause on THAT SAME message afterwards used
to either silently swallow the pause() failure (state flips to "paused"
but nothing was actually controlled) or give up entirely on a play()
failure (_stop_audio()) — reported live as "doesn't play the first time
after switching output device while something was playing, only the
second attempt works" (the second attempt only succeeded because
_current_audio_id had by then been reset, landing on the fully-recovering
_play_audio() path for a fresh message instead).

Fixed by trying _recover_audio_stream_after_device_switch() (reopen from
the still-valid decrypted temp file) whenever pause()/play() raises here,
falling back to _stop_audio() only if that recovery itself fails.

ConversationsPanel is a wx.Panel and can't be instantiated without a
running wx.App, so _toggle_playback() is bound onto a plain stub — same
approach as tests/test_toggle_current_audio_playback.py.
"""

from ui.conversations import ConversationsPanel


class _FakeTimer:
    def __init__(self):
        self.started = []
        self.stopped = 0

    def Start(self, interval):
        self.started.append(interval)

    def Stop(self):
        self.stopped += 1


class _FakeStreamCtrl:
    def __init__(self, raise_on_play=False, raise_on_pause=False):
        self.paused = 0
        self.played = 0
        self._raise_on_play = raise_on_play
        self._raise_on_pause = raise_on_pause

    def pause(self):
        self.paused += 1
        if self._raise_on_pause:
            raise RuntimeError("stream is gone")

    def play(self):
        self.played += 1
        if self._raise_on_play:
            raise RuntimeError("stream is gone")


class _Stub:
    _toggle_playback = ConversationsPanel._toggle_playback

    def __init__(self, current_audio_id, is_playing, stream_raises,
                 recovery_succeeds=False):
        self._current_audio_id = current_audio_id
        self._audio_stream = _FakeStreamCtrl(
            raise_on_play=stream_raises, raise_on_pause=stream_raises,
        )
        self._audio_tempo_ctrl = None
        self._is_audio_playing = is_playing
        self._audio_timer = _FakeTimer()
        self.stop_audio_calls = 0
        self.recovery_calls = 0
        self._recovery_succeeds = recovery_succeeds

    def _stop_audio(self):
        self.stop_audio_calls += 1

    def _recover_audio_stream_after_device_switch(self):
        self.recovery_calls += 1
        return self._recovery_succeeds


MSG_ID = "m1"


class TestSameItemToggleRecoversFromADeadDeviceSwitchChannel:
    def test_failed_resume_recovers_instead_of_doing_nothing(self):
        stub = _Stub(MSG_ID, is_playing=False, stream_raises=True, recovery_succeeds=True)

        stub._toggle_playback(MSG_ID, 5, {}, "/tmp/x.ogg", ".ogg")

        assert stub.recovery_calls == 1
        assert stub.stop_audio_calls == 0
        assert stub._is_audio_playing is True
        assert stub._audio_timer.started == [30]

    def test_failed_resume_stops_audio_when_recovery_also_fails(self):
        stub = _Stub(MSG_ID, is_playing=False, stream_raises=True, recovery_succeeds=False)

        stub._toggle_playback(MSG_ID, 5, {}, "/tmp/x.ogg", ".ogg")

        assert stub.recovery_calls == 1
        assert stub.stop_audio_calls == 1
        assert stub._is_audio_playing is False

    def test_failed_pause_recovers_and_ends_up_playing(self):
        """pause() raising means the channel died, not "already paused" —
        the previous behaviour silently swallowed this and reported
        _is_audio_playing=False despite nothing having actually paused."""
        stub = _Stub(MSG_ID, is_playing=True, stream_raises=True, recovery_succeeds=True)

        stub._toggle_playback(MSG_ID, 5, {}, "/tmp/x.ogg", ".ogg")

        assert stub.recovery_calls == 1
        assert stub._is_audio_playing is True
        assert stub._audio_timer.started == [30]

    def test_ordinary_pause_still_works_without_touching_recovery(self):
        stub = _Stub(MSG_ID, is_playing=True, stream_raises=False)

        stub._toggle_playback(MSG_ID, 5, {}, "/tmp/x.ogg", ".ogg")

        assert stub.recovery_calls == 0
        assert stub._audio_stream.paused == 1
        assert stub._is_audio_playing is False
        assert stub._audio_timer.stopped == 1

    def test_ordinary_resume_still_works_without_touching_recovery(self):
        stub = _Stub(MSG_ID, is_playing=False, stream_raises=False)

        stub._toggle_playback(MSG_ID, 5, {}, "/tmp/x.ogg", ".ogg")

        assert stub.recovery_calls == 0
        assert stub._audio_stream.played == 1
        assert stub._is_audio_playing is True
