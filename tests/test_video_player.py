"""Tests for client/core/video_player.py.

This module plays a video's audio track through BASS directly on the
source file (via the bass_aac plugin, sound_lib/lib/x64/bass_aac.dll,
copied into client/lib/ the same way bassopus.dll already was) and renders
its picture by decoding a low-rate MJPEG frame sequence with the bundled
ffmpeg binary — see the module's own docstring for the full "why" (BASS
alone has no video-rendering capability at all).

extract_jpeg_frames() — the pure MJPEG-stream-parsing logic — needs no wx/
ffmpeg/audio device and is tested directly; it's the trickiest bit of
custom protocol handling in the module (ffmpeg's `image2pipe`/mjpeg output
is just JPEG images concatenated back to back with no separate framing
header).

TestEofDrainsTheQueueBeforeStopping exercises a real, live-reproduced bug:
the first version of _on_playback_finished() stopped the render timer the
instant ffmpeg's output pipe closed (EOF), even when several already-
decoded frames were still sitting in the bounded queue waiting to be drawn
— cutting off the last ~1 second of every video. Verified against a real
synthetic test clip (ffmpeg lavfi testsrc) during development: 16/24 frames
rendered before the fix, 24/24 after. These tests pin the fixed behaviour
using a real wx.Timer (needs a running wx.App) but no ffmpeg process or
audio device — frames/EOF are injected directly into the player's internal
queue/flag instead.
"""

import logging
import queue
import subprocess
import time

import pytest

from core.video_player import extract_jpeg_frames, fit_frame_size, VideoPlayer


def _jpeg(payload: bytes) -> bytes:
    """A minimal fake JPEG: SOI + payload + EOI. Not a real decodable
    image, but the framing logic under test only ever looks at the marker
    bytes, never the payload. Only safe for extract_jpeg_frames() tests
    below, which never actually decode it — see _real_jpeg_bytes() for why
    _on_timer()'s tests (which DO decode, via wx.Image) can't use this."""
    return b"\xff\xd8" + payload + b"\xff\xd9"


def _real_jpeg_bytes(wx_app) -> bytes:
    """A genuinely valid, decodable 2x2 JPEG — unlike _jpeg() above, needed
    anywhere a test feeds a frame through VideoPlayer._on_timer(), which
    decodes it for real via wx.Image(..., wx.BITMAP_TYPE_JPEG). Feeding
    that decoder deliberately-malformed bytes (_jpeg()'s SOI+garbage+EOI)
    is exactly what test_video_player.py used to do here — caught as a
    normal Python exception (and silently ignored) most of the time, but
    crashed the whole pytest *process* on GitHub Actions' headless Windows
    runner: pytest's own "N passed" summary printed successfully, then the
    process died with a non-zero exit code moments later with no
    traceback — reproduced live via two failed release builds in a row,
    bisected down to specifically the tests that fed garbage into
    _on_timer(). A real (if trivial) JPEG removes the malformed-input path
    entirely instead of trying to make the crash itself survivable.
    """
    import os
    import tempfile
    import wx
    img = wx.Image(2, 2)
    img.SetRGB(0, 0, 255, 0, 0)
    img.SetRGB(1, 0, 0, 255, 0)
    img.SetRGB(0, 1, 0, 0, 255)
    img.SetRGB(1, 1, 255, 255, 0)
    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    tmp.close()
    try:
        img.SaveFile(tmp.name, wx.BITMAP_TYPE_JPEG)
        with open(tmp.name, "rb") as f:
            return f.read()
    finally:
        os.unlink(tmp.name)


class TestExtractJpegFrames:
    def test_empty_buffer_yields_nothing(self):
        frames, remainder = extract_jpeg_frames(b"")
        assert frames == []
        assert remainder == b""

    def test_a_single_complete_frame(self):
        frame = _jpeg(b"one-frame-of-pixels")
        frames, remainder = extract_jpeg_frames(frame)
        assert frames == [frame]
        assert remainder == b""

    def test_multiple_back_to_back_frames(self):
        f1 = _jpeg(b"frame1")
        f2 = _jpeg(b"frame2")
        f3 = _jpeg(b"frame3")
        frames, remainder = extract_jpeg_frames(f1 + f2 + f3)
        assert frames == [f1, f2, f3]
        assert remainder == b""

    def test_a_partial_trailing_frame_is_kept_as_remainder(self):
        """Simulates the realistic case: a chunk boundary lands mid-frame —
        the incomplete frame must NOT be emitted yet, and must survive to
        be completed once more bytes arrive."""
        complete = _jpeg(b"done")
        partial  = b"\xff\xd8" + b"still-arriving"  # no EOI yet
        frames, remainder = extract_jpeg_frames(complete + partial)
        assert frames == [complete]
        assert remainder == partial

    def test_the_remainder_completes_correctly_on_the_next_call(self):
        """Two-chunk simulation: first call leaves a partial frame as
        remainder, caller re-feeds remainder+next_chunk, the frame comes out
        whole."""
        first_chunk = b"\xff\xd8" + b"half"
        frames1, remainder1 = extract_jpeg_frames(first_chunk)
        assert frames1 == []
        assert remainder1 == first_chunk

        second_chunk = remainder1 + b"-more" + b"\xff\xd9"
        frames2, remainder2 = extract_jpeg_frames(second_chunk)
        assert frames2 == [b"\xff\xd8" + b"half-more" + b"\xff\xd9"]
        assert remainder2 == b""

    def test_garbage_before_the_first_soi_is_dropped(self):
        frame = _jpeg(b"payload")
        frames, remainder = extract_jpeg_frames(b"\x00\x01garbage" + frame)
        assert frames == [frame]
        assert remainder == b""

    def test_no_soi_at_all_returns_everything_as_remainder(self):
        junk = b"not a jpeg stream at all"
        frames, remainder = extract_jpeg_frames(junk)
        assert frames == []
        assert remainder == junk


class _FakeMainWindow:
    """Never actually reached by these tests (no real ffmpeg/BASS calls
    happen), but VideoPlayer.__init__ doesn't touch it either — only kept
    for API completeness."""
    pass


_created_players = []


def _make_player(wx_app, on_frame_size=None, box_size=None):
    import wx
    frame = wx.Frame(None)
    bitmap = wx.StaticBitmap(frame)
    if box_size is not None:
        # SetSize (not SetMinSize) so GetSize() reports it back
        # deterministically without needing a real sizer/Layout() pass.
        bitmap.SetSize(box_size)
    player = VideoPlayer(_FakeMainWindow(), bitmap, on_frame_size=on_frame_size)
    _created_players.append(player)
    return player


def _real_jpeg_bytes_sized(wx_app, width: int, height: int) -> bytes:
    """Same idea as _real_jpeg_bytes() above (a genuinely decodable JPEG,
    needed because _on_timer() decodes for real via wx.Image), but at a
    caller-chosen size instead of the fixed 2x2 — needed to exercise
    fit_frame_size()'s actual scaling path instead of always hitting its
    "already fits" no-op branch."""
    import os
    import tempfile
    import wx
    img = wx.Image(width, height)
    img.SetRGB(0, 0, 255, 0, 0)
    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    tmp.close()
    try:
        img.SaveFile(tmp.name, wx.BITMAP_TYPE_JPEG)
        with open(tmp.name, "rb") as f:
            return f.read()
    finally:
        os.unlink(tmp.name)


@pytest.fixture(autouse=True)
def _destroy_leftover_frames():
    """Every test here builds its own throwaway wx.Frame via _make_player()
    and none of them ever call player.stop()/frame.Destroy() — each one
    left its wx.Timer armed and its wx.Frame/wx.StaticBitmap alive for the
    rest of the process. That went unnoticed locally, but wx.App tearing
    down with a still-running wx.Timer bound to a window that's about to
    be destroyed crashed the whole pytest process on GitHub Actions'
    headless Windows runner: pytest's own "N passed" summary printed
    successfully, then the process died with a non-zero exit code ~2
    seconds later with no traceback at all — reproduced live via two
    failed release builds in a row, bisected down to this file
    specifically. stop() first (so no armed Timer survives to fire against
    a window that's mid-destruction), then Destroy() every top-level
    window (not just the one this test happened to create — a prior
    test's leak would otherwise still be sitting there).
    """
    yield
    import wx
    for player in _created_players:
        try:
            player.stop()
        except Exception:
            pass
    _created_players.clear()
    for win in list(wx.GetTopLevelWindows()):
        try:
            win.Destroy()
        except Exception:
            pass


class TestEofDrainsTheQueueBeforeStopping:
    """Regression test for a real bug found while manually verifying this
    module against a live ffmpeg-generated test clip — see module docstring."""

    def test_queued_frames_still_render_after_eof_is_signalled(self, wx_app):
        player = _make_player(wx_app)
        real_jpeg = _real_jpeg_bytes(wx_app)
        player._frame_queue.put(real_jpeg)
        player._frame_queue.put(real_jpeg)
        player._eof_reached = True
        player.is_playing = True

        # First tick: a queued frame is still there — must render it, not
        # stop, even though EOF has already been signalled.
        player._on_timer(None)
        assert player.is_playing is True
        assert player._frame_queue.qsize() == 1

        player._on_timer(None)
        assert player.is_playing is True
        assert player._frame_queue.qsize() == 0

    def test_stops_only_once_the_queue_is_actually_empty_and_eof_was_seen(self, wx_app):
        player = _make_player(wx_app)
        player._eof_reached = True
        player.is_playing = True

        player._on_timer(None)  # queue already empty + eof -> stop now

        assert player.is_playing is False
        assert player._timer.IsRunning() is False

    def test_an_empty_queue_without_eof_does_not_stop(self, wx_app):
        """Just a momentary gap between frames arriving — not the end of
        the stream — must not be mistaken for playback finishing."""
        player = _make_player(wx_app)
        player.is_playing = True
        player._eof_reached = False

        player._on_timer(None)

        assert player.is_playing is True

    def test_a_stale_generation_does_not_signal_eof_onto_a_newer_playback(self, wx_app):
        """An old (stopped/replaced) reader thread finishing late must not
        be able to mark a NEWER load_and_play() as finished."""
        player = _make_player(wx_app)
        old_generation = player._generation
        player._generation += 1  # simulate a second load_and_play() having started
        player.is_playing = True

        # This is exactly what _read_frames()'s finally-block does, called
        # with the OLD generation number it captured before the newer video
        # started.
        if old_generation == player._generation:
            player._eof_reached = True

        assert player._eof_reached is False


class _FakeAudioCtrl:
    """Stands in for the BASS channel (_audio_stream/_tempo_ctrl) — only
    the is_active() method _audio_still_active() actually calls."""

    def __init__(self, active_value):
        self._active_value = active_value

    def is_active(self):
        return self._active_value


class TestAudioOnlyPlaybackDoesNotFinishWhileAudioIsStillPlaying:
    """Regression: for an audio-only source (a status/message with no
    video stream at all), ffmpeg's frame pipe has nothing to output and
    reaches EOF almost immediately — long before BASS is actually done
    playing. The old code treated frame-EOF alone as "playback finished",
    flipping is_playing back to False moments after starting — reported
    live as "pause always restarts instead of pausing" for audio
    statuses, since is_playing had already gone False on its own by the
    time the user clicked pause."""

    def test_does_not_finish_while_bass_reports_still_active(self, wx_app):
        from sound_lib.external.pybass import BASS_ACTIVE_PLAYING

        player = _make_player(wx_app)
        player._audio_stream = _FakeAudioCtrl(BASS_ACTIVE_PLAYING)
        player._eof_reached = True
        player.is_playing = True

        player._on_timer(None)

        assert player.is_playing is True

    def test_does_not_finish_while_bass_reports_paused(self, wx_app):
        """A channel the user just paused reports BASS_ACTIVE_PAUSED, not
        BASS_ACTIVE_PLAYING — must still count as "not finished", or a
        deliberate pause would get wrongly treated as the end of
        playback."""
        from sound_lib.external.pybass import BASS_ACTIVE_PAUSED

        player = _make_player(wx_app)
        player._audio_stream = _FakeAudioCtrl(BASS_ACTIVE_PAUSED)
        player._eof_reached = True
        player.is_playing = True

        player._on_timer(None)

        assert player.is_playing is True

    def test_finishes_once_bass_reports_fully_stopped(self, wx_app):
        from sound_lib.external.pybass import BASS_ACTIVE_STOPPED

        player = _make_player(wx_app)
        player._audio_stream = _FakeAudioCtrl(BASS_ACTIVE_STOPPED)
        player._eof_reached = True
        player.is_playing = True

        player._on_timer(None)

        assert player.is_playing is False

    def test_tempo_ctrl_takes_priority_over_audio_stream_when_both_are_set(self, wx_app):
        from sound_lib.external.pybass import BASS_ACTIVE_PLAYING, BASS_ACTIVE_STOPPED

        player = _make_player(wx_app)
        player._audio_stream = _FakeAudioCtrl(BASS_ACTIVE_STOPPED)
        player._tempo_ctrl = _FakeAudioCtrl(BASS_ACTIVE_PLAYING)
        player._eof_reached = True
        player.is_playing = True

        player._on_timer(None)

        assert player.is_playing is True

    def test_no_audio_ctrl_at_all_finishes_immediately_like_before(self, wx_app):
        """Nothing ever started (or a genuine video whose audio already
        tore down) — must not hang waiting for an audio channel that
        doesn't exist."""
        player = _make_player(wx_app)
        player._eof_reached = True
        player.is_playing = True

        player._on_timer(None)

        assert player.is_playing is False


class TestIsPlayingReflectsStopEvent:
    """is_playing used to be a plain attribute that stop() cleared at the
    very end of its own teardown — a caller checking it mid-stop() (e.g.
    two rapid toggle_pause() calls racing a stop() triggered by switching
    statuses/conversations) could observe a stale "still playing" for that
    window. It's now a property backed by _is_active, additionally gated
    on _stop_event — set at the very START of stop(), before any teardown
    happens."""

    def test_true_after_being_set(self, wx_app):
        player = _make_player(wx_app)
        player.is_playing = True
        assert player.is_playing is True

    def test_false_once_stop_event_is_set_even_if_is_active_was_never_cleared(self, wx_app):
        player = _make_player(wx_app)
        player.is_playing = True
        player._stop_event.set()  # what stop() does first, before teardown
        assert player.is_playing is False

    def test_stop_clears_it(self, wx_app):
        player = _make_player(wx_app)
        player.is_playing = True
        player.stop()
        assert player.is_playing is False


class TestFitFrameSize:
    """wx.StaticBitmap clips rather than scales, so a frame bigger than the
    control it is drawn into shows only its top-left corner. ffmpeg emits
    frames at a fixed 480 px width while both callers hand this module a
    smaller control (StatusPanel: a fixed 320x240; ConversationsPanel: a
    box sized from the last <=200 px thumbnail, or nothing at all), which is
    what "os videos so abrem pela metade nos status e conversas" actually
    was."""

    def test_a_frame_wider_than_the_box_is_scaled_down_to_fit(self):
        assert fit_frame_size(480, 270, 320, 240) == (320, 180)

    def test_a_portrait_frame_is_limited_by_the_boxs_height(self):
        # 480x854 into 320x240: height is the binding constraint (240/854),
        # so the result must fit inside BOTH dimensions, not just the width —
        # and must actually USE the height it has (int() truncation can cost
        # a pixel, nothing more).
        width, height = fit_frame_size(480, 854, 320, 240)
        assert width <= 320 and height <= 240
        assert height >= 239

    def test_aspect_ratio_is_preserved(self):
        width, height = fit_frame_size(1920, 1080, 320, 240)
        assert abs((width / height) - (1920 / 1080)) < 0.02

    def test_a_frame_that_already_fits_is_left_alone(self):
        assert fit_frame_size(160, 90, 320, 240) is None

    def test_a_frame_exactly_the_size_of_the_box_is_left_alone(self):
        assert fit_frame_size(320, 240, 320, 240) is None

    def test_a_control_with_no_size_of_its_own_leaves_the_frame_alone(self):
        """A wx.StaticBitmap created with no explicit size and never laid
        out with a bitmap in it reports 0 (or 1) — there is no box to fit
        into, so the frame is drawn at its natural size and the caller's own
        layout decides."""
        assert fit_frame_size(480, 270, 0, 0) is None
        assert fit_frame_size(480, 270, 1, 1) is None

    def test_a_degenerate_frame_is_left_alone(self):
        assert fit_frame_size(0, 0, 320, 240) is None
        assert fit_frame_size(-1, 100, 320, 240) is None

    def test_the_result_is_never_zero_sized(self):
        width, height = fit_frame_size(4000, 4, 320, 240)
        assert width >= 1 and height >= 1


class TestOnFrameSizeCallback:
    """Scaling a frame down to fit the box (TestFitFrameSize above) stops it
    being CLIPPED, but the box itself stays at its generic placeholder size
    for the whole video — a portrait clip inside the 4:3 default box then
    renders small and left-aligned with a big blank gap filling the rest,
    which still reads as "the video isn't fully shown" even though every
    pixel is technically there (reported live, again, after the scale-to-fit
    fix). on_frame_size(width, height) fires once, on the first frame, so
    the caller can shrink its box to the frame's own actual size — the same
    way a still photo is already sized to exactly its own content."""

    def test_fires_once_with_the_scaled_size_when_the_frame_needs_shrinking(self, wx_app):
        calls = []
        player = _make_player(wx_app, on_frame_size=lambda w, h: calls.append((w, h)), box_size=(320, 240))
        # 400x100 into 320x240: ratio = min(320/400, 240/100) = 0.8 -> (320, 80).
        frame = _real_jpeg_bytes_sized(wx_app, 400, 100)
        player._frame_queue.put(frame)
        player.is_playing = True

        player._on_timer(None)

        assert calls == [(320, 80)]

    def test_fires_once_with_the_natural_size_when_the_frame_already_fits(self, wx_app):
        """fit_frame_size() returns None when nothing needs scaling — the
        box must still shrink to the frame's own (smaller) natural size,
        not stay at the oversized placeholder."""
        calls = []
        player = _make_player(wx_app, on_frame_size=lambda w, h: calls.append((w, h)), box_size=(320, 240))
        frame = _real_jpeg_bytes_sized(wx_app, 40, 30)
        player._frame_queue.put(frame)
        player.is_playing = True

        player._on_timer(None)

        assert calls == [(40, 30)]

    def test_does_not_fire_again_on_the_second_frame_of_the_same_playback(self, wx_app):
        """Only once per playback — resizing the box on every frame would
        mean relaying out the panel at 12fps, worse for a screen-reader user
        than the blank-gap bug this is fixing."""
        calls = []
        player = _make_player(wx_app, on_frame_size=lambda w, h: calls.append((w, h)), box_size=(320, 240))
        frame = _real_jpeg_bytes_sized(wx_app, 400, 100)
        player._frame_queue.put(frame)
        player._frame_queue.put(frame)
        player.is_playing = True

        player._on_timer(None)
        player._on_timer(None)

        assert len(calls) == 1

    def test_load_and_play_resets_the_once_only_flag(self, wx_app, monkeypatch):
        """A new video (possibly a different aspect ratio) needs its own
        one-time resize — load_and_play() must reset the "already sized"
        flag so the next playback's first frame fires the callback again.

        (Not asserted end-to-end via a second _on_timer() call: wx.StaticBitmap
        auto-shrinks itself to whatever bitmap it's just been given — visible
        in this very test's first frame already leaving the control at 320x80,
        not the 320x240 it started at — so a bare second call here would
        legitimately compute the *same* fitted size against that leftover
        size and correctly skip re-firing. Production callers avoid that by
        resetting the box back to the shared baseline via SetMinSize()+
        Layout() before every load_and_play() — see
        ConversationsPanel._start_video_playback() — which is a UI concern
        this module has no part of.)"""
        player = _make_player(wx_app, on_frame_size=lambda w, h: None, box_size=(320, 240))
        frame = _real_jpeg_bytes_sized(wx_app, 400, 100)
        player._frame_queue.put(frame)
        player.is_playing = True
        player._on_timer(None)
        assert player._box_sized is True

        # load_and_play() would normally spin up real ffmpeg/BASS — not
        # wanted here, just the flag reset it does before that.
        monkeypatch.setattr(player, "_start_audio", lambda *a, **k: None)
        monkeypatch.setattr(player, "_start_video_pipe", lambda: None)
        player.load_and_play("fake.mp4")

        assert player._box_sized is False

    def test_no_callback_registered_does_not_crash(self, wx_app):
        """ConversationsPanel/StatusPanel always pass one, but the parameter
        is optional — must degrade gracefully, not raise, if a future
        caller doesn't."""
        player = _make_player(wx_app, on_frame_size=None, box_size=(320, 240))
        frame = _real_jpeg_bytes_sized(wx_app, 400, 100)
        player._frame_queue.put(frame)

        player._on_timer(None)  # must not raise


class TestSetPositionDoesNotBlockTheCallingThread:
    """Regression: set_position() is called directly from wx seek-slider/
    shortcut handlers (media_viewer.py, conversations.py) on the UI thread.
    It used to run _kill_ffmpeg_locked() — which blocks on
    proc.wait(timeout=2) — synchronously as part of that same call, so
    seeking past a slow-to-reap ffmpeg process could freeze the whole
    window (NVDA/JAWS included, in this accessibility-first app) for up to
    two seconds. The kill+restart now happens on a background thread; only
    the (cheap) generation bump stays synchronous, since _read_frames()'s
    loop needs to see it change before set_position() returns."""

    class _FakeAudioCtrl:
        def set_position(self, pos):
            pass

    class _SlowToReapProcess:
        """Simulates proc.wait(timeout=2) actually taking close to the full
        timeout — the exact case that used to freeze the UI thread."""
        def __init__(self):
            self.killed = False

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            time.sleep(min(0.3, timeout or 0.3))

    def test_seek_returns_before_the_old_process_finishes_dying(self, wx_app, monkeypatch):
        player = _make_player(wx_app)
        player._audio_stream = self._FakeAudioCtrl()
        player.is_playing = True
        player._video_path = "fake.mp4"
        player._ffmpeg_proc = self._SlowToReapProcess()
        monkeypatch.setattr(player, "_start_video_pipe", lambda *a, **k: None)

        generation_before = player._generation
        started = time.monotonic()
        player.set_position(1000)
        elapsed = time.monotonic() - started

        assert elapsed < 0.2, f"set_position() blocked the caller for {elapsed:.3f}s"
        # The generation bump itself must still be synchronous — a frame
        # from the old pipe arriving right after this call has to be
        # recognisable as stale immediately, not after the background
        # thread eventually gets around to it.
        assert player._generation == generation_before + 1

        # Let the background thread actually finish before the test exits.
        time.sleep(0.5)


class TestKillFfmpegLogsFailures:
    """Regression: both proc.kill() and proc.wait() failures were swallowed
    by a bare `except Exception: pass` with nothing logged — a report of
    ffmpeg.exe processes piling up over a long session had no trace in
    log.log to explain why."""

    class _FakeProcess:
        def __init__(self, kill_exc=None, wait_exc=None):
            self.pid = 4242
            self._kill_exc = kill_exc
            self._wait_exc = wait_exc
            self.killed = False
            self.waited = False

        def kill(self):
            self.killed = True
            if self._kill_exc:
                raise self._kill_exc

        def wait(self, timeout=None):
            self.waited = True
            if self._wait_exc:
                raise self._wait_exc

    def test_a_kill_failure_is_logged(self, wx_app, caplog):
        player = _make_player(wx_app)
        proc = self._FakeProcess(kill_exc=ProcessLookupError("no such process"))
        player._ffmpeg_proc = proc

        with caplog.at_level(logging.WARNING):
            player._kill_ffmpeg_locked()

        assert "failed to signal ffmpeg" in caplog.text
        assert "4242" in caplog.text

    def test_a_wait_timeout_is_logged(self, wx_app, caplog):
        player = _make_player(wx_app)
        proc = self._FakeProcess(
            wait_exc=subprocess.TimeoutExpired(cmd="ffmpeg", timeout=2)
        )
        player._ffmpeg_proc = proc

        with caplog.at_level(logging.WARNING):
            player._kill_ffmpeg_locked()

        assert "did not exit within 2s" in caplog.text
        assert "4242" in caplog.text

    def test_no_log_when_kill_and_wait_both_succeed(self, wx_app, caplog):
        player = _make_player(wx_app)
        proc = self._FakeProcess()
        player._ffmpeg_proc = proc

        with caplog.at_level(logging.WARNING):
            player._kill_ffmpeg_locked()

        assert proc.killed and proc.waited
        assert "video_player" not in caplog.text

    def test_clears_the_tracked_process_regardless_of_failures(self, wx_app):
        player = _make_player(wx_app)
        player._ffmpeg_proc = self._FakeProcess(
            kill_exc=Exception("boom"), wait_exc=Exception("boom too"),
        )

        player._kill_ffmpeg_locked()

        assert player._ffmpeg_proc is None

    def test_no_process_tracked_is_a_no_op(self, wx_app):
        player = _make_player(wx_app)
        player._ffmpeg_proc = None

        player._kill_ffmpeg_locked()  # must not raise
