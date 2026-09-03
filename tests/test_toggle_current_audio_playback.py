"""Tests for ConversationsPanel.toggle_current_audio_playback() — the
Ctrl+Alt+Shift+P global shortcut that pauses/resumes whichever voice or
audio message is currently loaded in the player, from anywhere in the
window, regardless of which conversation is open or focused.

Unlike _toggle_playback() (bound to a specific row/message), this needs no
target — it only flips play/pause on whatever _audio_stream already holds,
so it's a no-op when nothing is loaded.

ConversationsPanel is a wx.Panel and can't be instantiated without a running
wx.App, so the method under test is bound onto a plain stub — same approach
as tests/test_audio_finish_marks_played.py.
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
    toggle_current_audio_playback = ConversationsPanel.toggle_current_audio_playback

    def __init__(self, current_audio_id=None, is_playing=False, tempo_ctrl=None,
                 stream_raises=False, recovery_succeeds=False):
        self._current_audio_id = current_audio_id
        self._audio_stream = (
            _FakeStreamCtrl(raise_on_play=stream_raises, raise_on_pause=stream_raises)
            if current_audio_id else None
        )
        self._audio_tempo_ctrl = tempo_ctrl
        self._is_audio_playing = is_playing
        self._audio_timer = _FakeTimer()
        self.stop_audio_calls = 0
        self._recovery_succeeds = recovery_succeeds
        self.recovery_calls = 0

    def _stop_audio(self):
        self.stop_audio_calls += 1

    def _recover_audio_stream_after_device_switch(self):
        self.recovery_calls += 1
        return self._recovery_succeeds


class TestNothingLoaded:
    def test_no_current_audio_id_is_a_no_op(self):
        stub = _Stub(current_audio_id=None)
        stub.toggle_current_audio_playback()  # must not raise
        assert stub.stop_audio_calls == 0

    def test_no_audio_stream_is_a_no_op(self):
        stub = _Stub(current_audio_id="m1")
        stub._audio_stream = None
        stub.toggle_current_audio_playback()  # must not raise


class TestPauseAndResume:
    def test_playing_gets_paused(self):
        stub = _Stub(current_audio_id="m1", is_playing=True)

        stub.toggle_current_audio_playback()

        assert stub._audio_stream.paused == 1
        assert stub._is_audio_playing is False
        assert stub._audio_timer.stopped == 1

    def test_paused_gets_resumed(self):
        stub = _Stub(current_audio_id="m1", is_playing=False)

        stub.toggle_current_audio_playback()

        assert stub._audio_stream.played == 1
        assert stub._is_audio_playing is True
        assert stub._audio_timer.started == [30]

    def test_a_tempo_controller_is_preferred_over_the_raw_stream(self):
        """Same convention _toggle_playback() itself uses: whichever one is
        currently driving playback speed."""
        tempo = _FakeStreamCtrl()
        stub = _Stub(current_audio_id="m1", is_playing=True, tempo_ctrl=tempo)

        stub.toggle_current_audio_playback()

        assert tempo.paused == 1
        assert stub._audio_stream.paused == 0

    def test_a_failed_resume_recovers_the_stream_instead_of_giving_up(self):
        """The channel raising here almost always means the output device
        was switched in Settings while this was loaded (BASS_Free()/
        BASS_Init() invalidates it) — reopening fresh and playing that is
        the useful outcome, not silently doing nothing."""
        stub = _Stub(current_audio_id="m1", is_playing=False, stream_raises=True,
                      recovery_succeeds=True)

        stub.toggle_current_audio_playback()  # must not raise

        assert stub.recovery_calls == 1
        assert stub.stop_audio_calls == 0
        assert stub._is_audio_playing is True
        assert stub._audio_timer.started == [30]

    def test_a_failed_resume_stops_audio_when_recovery_also_fails(self):
        stub = _Stub(current_audio_id="m1", is_playing=False, stream_raises=True,
                      recovery_succeeds=False)

        stub.toggle_current_audio_playback()  # must not raise

        assert stub.recovery_calls == 1
        assert stub.stop_audio_calls == 1
        assert stub._is_audio_playing is False

    def test_a_failed_pause_recovers_and_keeps_playing(self):
        """pause() raising doesn't mean "already paused" — the channel is
        dead. Recovering must land on "playing", not silently pausing
        nothing."""
        stub = _Stub(current_audio_id="m1", is_playing=True, stream_raises=True,
                      recovery_succeeds=True)

        stub.toggle_current_audio_playback()  # must not raise

        assert stub.recovery_calls == 1
        assert stub._is_audio_playing is True
        assert stub._audio_timer.started == [30]
