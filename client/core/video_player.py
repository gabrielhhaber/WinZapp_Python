"""Minimal audio+video playback for status/message video files.

BASS (via sound_lib, already used everywhere else in WinZapp for audio) has
no video-rendering capability at all — it's an audio-only engine. Two
things follow:

1. Audio: BASS decodes WhatsApp's AAC track straight out of the .mp4
   container natively, via the bass_aac plugin bundled with the `sound-lib`
   pip package itself (sound_lib/lib/x64/bass_aac.dll) and copied into
   client/lib/ (same as bassopus.dll already was) — SoundSystem.start()
   already calls _load_bass_plugin('bass_aac.dll') at startup; it just had
   no DLL to find there until now. No ffmpeg/extraction step needed for
   audio at all — sl_stream.FileStream(file=video_path) opens and decodes
   it directly, exactly like every other audio playback in this app.
2. Video: still has to be decoded and rendered separately, since BASS truly
   has no video output. Frames are decoded by the ffmpeg binary WinZapp
   already bundles for voice-message conversion (see
   MainWindow._find_api_ffmpeg()), piped out as a plain low-rate MJPEG
   image sequence (not meant to be high-fidelity — the goal is "see what's
   happening" on the same machine as the rest of a screen-reader-focused
   desktop app) and rendered into a wx.StaticBitmap.

Audio/video sync is deliberately simple rather than exact: ffmpeg decodes
into a bounded queue and wx.Timer controls when frames are presented. The
timer follows the same 1x/1.5x/2x speed selected for the BASS Tempo channel,
so picture and audio advance together. Pause stops both the BASS channel and
frame timer; queue backpressure then prevents ffmpeg from running far ahead.
Seeking restarts only the ffmpeg picture pipe at the matching audio offset.

Shared by client/ui/conversations.py (video MESSAGES) and
client/status_panel.py (video STATUSES) — see each caller's own
integration for how the wx.StaticBitmap target is wired into their layout.
"""

import io
import logging
import os
import queue
import subprocess
import sys
import threading

import wx
import sound_lib.stream as sl_stream
from sound_lib.effects import Tempo

_CREATE_NO_WINDOW = 0x08000000

# Mirrors ConversationsPanel._audio_tempo_map / StatusPanel's own copy — the
# same 3 speed steps exposed everywhere else audio plays in this app.
# Audio speed is handled by BASS Tempo FX. The frame timer follows the same
# speed step so 1.5x/2x keeps the picture aligned instead of letting it lag.
_TEMPO_MAP = {1.0: 0, 1.5: 50, 2.0: 100}

# Frames are capped small and slow on purpose: this runs on the same machine
# as the rest of a screen-reader-focused desktop app, and the goal is "see
# what's happening", not full-fidelity playback.
_FRAME_WIDTH  = 480
_FRAME_FPS    = 12
_FRAME_MAX_QUEUE = 8

_JPEG_SOI = b"\xff\xd8"  # Start Of Image marker
_JPEG_EOI = b"\xff\xd9"  # End Of Image marker


def fit_frame_size(frame_w: int, frame_h: int, target_w: int, target_h: int):
    """Size a decoded frame must be scaled to so it fits *entirely* inside
    the wx.StaticBitmap it is drawn into, or ``None`` when it already fits
    (or there is nothing sensible to fit it to).

    wx.StaticBitmap does NOT scale what it is given: a bitmap larger than
    the control is simply clipped to the control's rectangle, so only its
    top-left corner is ever visible. Frames come out of ffmpeg at a fixed
    _FRAME_WIDTH (480) with whatever height the source aspect ratio implies,
    while both callers hand this module a much smaller control —
    StatusPanel's is created at a fixed (320, 240), and ConversationsPanel's
    is sized by its sizer from the last thumbnail it showed (capped at
    200 px, or nothing at all when the video had no embedded thumbnail).
    A 480x270 landscape frame therefore lost its right third and a 480x854
    portrait one showed barely its top corner — reported live as "os videos
    so abrem pela metade nos status e conversas".

    Pure function (no wx calls) so the fitting arithmetic is unit-testable
    without a display, same as extract_jpeg_frames() below.
    """
    if frame_w <= 0 or frame_h <= 0:
        return None
    # A control that was never laid out with a bitmap in it reports 0/1 —
    # there is no box to fit into, so draw at natural size and let the
    # caller's own layout give it room.
    if target_w <= 1 or target_h <= 1:
        return None
    if frame_w <= target_w and frame_h <= target_h:
        return None
    ratio = min(target_w / frame_w, target_h / frame_h)
    return max(1, int(frame_w * ratio)), max(1, int(frame_h * ratio))


def extract_jpeg_frames(buf: bytes) -> tuple:
    """Pull every complete JPEG frame out of an accumulated MJPEG byte
    buffer (ffmpeg's `-f image2pipe -vcodec mjpeg` output is just JPEG
    images concatenated back to back, each delimited by its own SOI/EOI
    marker pair — no separate framing/length header).

    Returns ``(frames, remainder)``: *frames* is every frame fully found in
    *buf* (SOI through the matching EOI, inclusive), in order; *remainder*
    is whatever's left after the last complete frame — either empty, or a
    partial frame still waiting on more bytes from the pipe.

    Pure function (no I/O) so the MJPEG-parsing logic is unit-testable
    without a real ffmpeg process.
    """
    frames = []
    pos = 0
    while True:
        start = buf.find(_JPEG_SOI, pos)
        if start == -1:
            # No new frame start in what's left — keep only bytes from the
            # last processed position onward (drop anything before a stray
            # SOI-less prefix, which can't ever become a frame).
            return frames, buf[pos:]
        end = buf.find(_JPEG_EOI, start + 2)
        if end == -1:
            # Frame started but hasn't finished arriving yet.
            return frames, buf[start:]
        frames.append(buf[start:end + 2])
        pos = end + 2


class VideoPlayer:
    """Plays one video file's audio (via BASS, directly on the file) and
    video (via a decoded MJPEG frame sequence rendered into a
    wx.StaticBitmap) at a time.

    Usage:
        player = VideoPlayer(main_window, bitmap_ctrl)
        player.load_and_play(video_path)
        ...
        player.toggle_pause()
        ...
        player.stop()   # always call when the viewer is closed/torn down
    """

    def __init__(self, main_window, bitmap_ctrl, on_frame_size=None):
        self.main_window = main_window
        self.bitmap_ctrl  = bitmap_ctrl
        # Called once per playback, the moment the first frame's actual
        # on-screen size is known, as on_frame_size(width, height). Lets a
        # caller shrink-wrap its box to that exact size (the same thing
        # already done for still images/thumbnails) instead of leaving it at
        # a generic placeholder box for the whole video — see _on_timer()'s
        # own comment for why this fires once, not every frame.
        self._on_frame_size = on_frame_size
        self._box_sized = False

        self._audio_stream = None
        self._tempo_ctrl   = None
        self._ffmpeg_proc  = None
        self._frame_thread = None
        self._frame_queue: "queue.Queue[bytes]" = queue.Queue(maxsize=_FRAME_MAX_QUEUE)
        self._stop_event = threading.Event()
        # Seeking replaces the ffmpeg process while its reader thread may still
        # be winding down; serialize those swaps so an old reader can never
        # kill or overwrite the new process handle.
        self._pipe_lock = threading.RLock()

        self._timer = wx.Timer(bitmap_ctrl)
        bitmap_ctrl.Bind(wx.EVT_TIMER, self._on_timer, self._timer)

        self._video_path = None
        self._is_active  = False
        self.is_paused   = False
        # Set by _read_frames() once ffmpeg's output ends; consumed by
        # _on_timer() to know when it's safe to actually stop (once the
        # queue it's still draining goes empty) — see _read_frames()'s
        # comment for why this can't just stop the timer directly.
        self._eof_reached = False
        # Bumped on every load_and_play() so a reader thread from an OLD
        # (stopped/replaced) video that's still winding down can't set
        # _eof_reached on a NEW playback it has nothing to do with — its
        # generation number will no longer match by the time it checks.
        self._generation = 0
        self._speed = 1.0
        self._volume = 1.0

    @property
    def is_playing(self) -> bool:
        """True once playback has started and stop() hasn't run since.
        Backed by _is_active rather than a plain attribute so a caller
        that only checked the flag (not _stop_event) couldn't observe a
        stale "still playing" during the brief window stop() is actively
        tearing things down in — e.g. two rapid toggle_pause() calls
        racing a stop() from a status/conversation switch."""
        return self._is_active and not self._stop_event.is_set()

    @is_playing.setter
    def is_playing(self, value: bool):
        self._is_active = bool(value)

    def _timer_interval(self) -> int:
        """Frame presentation interval matching the active playback speed."""
        return max(1, int(1000 / (_FRAME_FPS * max(0.25, self._speed))))

    # ── Public API ───────────────────────────────────────────────────────

    def load_and_play(self, video_path: str, speed: float = 1.0):
        """Start playback from the beginning, replacing any active media."""
        self.stop()
        self._stop_event.clear()
        self._eof_reached = False
        self._box_sized = False
        self._generation += 1
        self._video_path = video_path
        self._speed = speed if speed in _TEMPO_MAP else 1.0

        # Audio is cheap enough to open on the caller thread. Mark the player
        # active immediately: ffmpeg may take a moment to spawn, and a viewer
        # progress timer must not mistake that startup window for EOF.
        self._start_audio(video_path, self._speed)
        self.is_playing = True
        threading.Thread(target=self._start_video_pipe, daemon=True).start()

    def toggle_pause(self):
        if not self.is_playing:
            return
        if self.is_paused:
            self._resume()
        else:
            self._pause()

    def get_position(self) -> int:
        """Current audio-track position (BASS bytes), 0 if nothing is open."""
        ctrl = self._audio_ctrl()
        if ctrl is None:
            return 0
        try:
            return ctrl.get_position()
        except Exception:
            return 0

    def get_length(self) -> int:
        """Total audio-track length (BASS bytes), 0 if nothing is open."""
        ctrl = self._audio_ctrl()
        if ctrl is None:
            return 0
        try:
            return ctrl.get_length()
        except Exception:
            return 0

    def bytes_to_seconds(self, position: int) -> float:
        ctrl = self._audio_ctrl()
        if ctrl is None:
            return 0.0
        try:
            return float(ctrl.bytes_to_seconds(position))
        except Exception:
            return 0.0

    def seconds_to_bytes(self, seconds: float) -> int:
        """Convert seconds to BASS byte units, 0 if nothing is open."""
        ctrl = self._audio_ctrl()
        if ctrl is None:
            return 0
        try:
            return int(ctrl.seconds_to_bytes(seconds))
        except Exception:
            return 0

    def get_position_seconds(self) -> float:
        return self.bytes_to_seconds(self.get_position())

    def get_length_seconds(self) -> float:
        return self.bytes_to_seconds(self.get_length())

    def set_position(self, pos: int):
        """Seek both the BASS audio channel and ffmpeg picture."""
        ctrl = self._audio_ctrl()
        if ctrl is None:
            return
        try:
            ctrl.set_position(pos)
        except Exception:
            return
        seconds = self.bytes_to_seconds(pos)
        if self._video_path and self.is_playing:
            self._restart_video_pipe(seconds)

    def set_speed(self, speed: float):
        """Change audio tempo and frame presentation speed together."""
        if speed not in _TEMPO_MAP:
            return
        self._speed = speed
        if self._tempo_ctrl is not None:
            try:
                self._tempo_ctrl.tempo = _TEMPO_MAP[speed]
            except Exception:
                pass
        if self._timer.IsRunning():
            self._timer.Start(self._timer_interval())

    def set_volume(self, volume: float):
        self._volume = max(0.0, min(1.0, float(volume)))
        ctrl = self._audio_ctrl()
        if ctrl is not None:
            try:
                ctrl.volume = self._volume
            except Exception:
                pass

    def get_volume(self) -> float:
        return self._volume

    def stop(self):
        """Stop playback and release everything. Always safe to call even
        if nothing is playing."""
        self._stop_event.set()
        self._eof_reached = False
        try:
            self._timer.Stop()
        except Exception:
            pass
        # Tempo FX (when active) owns the audio output channel — stop it
        # before the decode stream it wraps, mirroring ConversationsPanel's
        # own _stop_audio().
        if self._tempo_ctrl is not None:
            try:
                self._tempo_ctrl.stop()
            except Exception:
                pass
            self._tempo_ctrl = None
        if self._audio_stream is not None:
            try:
                self._audio_stream.stop()
            except Exception:
                pass
            self._audio_stream = None
        self._kill_ffmpeg()
        # Drain anything left in the queue so a stale frame can't render
        # after a subsequent load_and_play() starts a new video.
        self._drain_frames()
        self.is_playing = False
        self.is_paused  = False

    # ── Internals: audio (BASS, directly on the video file) ─────────────

    def _audio_ctrl(self):
        return self._tempo_ctrl if self._tempo_ctrl is not None else self._audio_stream

    def _start_audio(self, video_path: str, speed: float = 1.0):
        # Always open a decoded stream wrapped in Tempo FX, exactly like
        # ConversationsPanel._play_audio()'s _open_stream() helper — a plain
        # (decode=False) stream has no way to change tempo after opening, so
        # keeping that shortcut at 1x would make set_speed() a no-op for any
        # video started at the default speed (the common case). The fallback
        # to a plain stream below only fires if Tempo FX itself is
        # unavailable or the format can't be opened decoded.
        def _open():
            try:
                s = sl_stream.FileStream(file=video_path, decode=True)
                tempo = Tempo(s)
                tempo.tempo = _TEMPO_MAP.get(speed, 0)
                return s, tempo
            except Exception:
                return sl_stream.FileStream(file=video_path, decode=False), None

        try:
            self._audio_stream, self._tempo_ctrl = _open()
            ctrl = self._audio_ctrl()
            ctrl.volume = self._volume
            ctrl.play()
        except Exception:
            # Same device-switch recovery pattern used throughout the app
            # (see ConversationsPanel/StatusPanel's own audio playback) — a
            # stream created just after a BASS device switch can be
            # invalid; reopening after handle_playback_failure() usually
            # recovers it.
            try:
                if self.main_window.sound_system.handle_playback_failure():
                    self._audio_stream, self._tempo_ctrl = _open()
                    ctrl = self._audio_ctrl()
                    ctrl.volume = self._volume
                    ctrl.play()
                else:
                    self._audio_stream = None
                    self._tempo_ctrl = None
            except Exception as exc:
                logging.warning("[video_player] audio playback failed: %s", exc)
                self._audio_stream = None
                self._tempo_ctrl = None

    # ── Internals: video (ffmpeg frame pipe, background thread) ─────────

    def _ffmpeg_bin(self) -> str:
        try:
            return self.main_window._find_api_ffmpeg()
        except Exception:
            return ""

    def _start_video_pipe(self, start_seconds: float = 0.0):
        # Capture the generation before spawning ffmpeg. If a seek/stop wins
        # the race while Popen is starting, this process is stale and must
        # never replace the newer pipe.
        request_generation = self._generation
        ffmpeg = self._ffmpeg_bin()
        if not ffmpeg or not os.path.isfile(ffmpeg):
            logging.warning("[video_player] ffmpeg not found — playing audio only, no picture.")
            self.is_playing = True
            return
        if self._stop_event.is_set() or not self._video_path:
            return

        creationflags = _CREATE_NO_WINDOW if sys.platform == "win32" else 0
        cmd = [ffmpeg, "-y"]
        if start_seconds > 0.01:
            cmd += ["-ss", f"{start_seconds:.3f}"]
        # Deliberately no -re: the bounded frame queue provides backpressure
        # while wx.Timer determines presentation speed. This lets 1.5x/2x keep
        # video in step with the Tempo-adjusted audio.
        cmd += [
            "-i", self._video_path,
            "-an", "-vf", f"scale={_FRAME_WIDTH}:-2,fps={_FRAME_FPS}",
            "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "6", "-",
        ]
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
        except Exception as exc:
            logging.warning("[video_player] failed to start ffmpeg frame pipe: %s", exc)
            self.is_playing = True  # audio can still play without a picture
            return

        with self._pipe_lock:
            if self._stop_event.is_set() or request_generation != self._generation:
                try:
                    proc.kill()
                except Exception:
                    pass
                return
            self._ffmpeg_proc = proc
            generation = request_generation

        self.is_playing = True
        self._frame_thread = threading.Thread(
            target=self._read_frames, args=(generation, proc), daemon=True
        )
        self._frame_thread.start()
        if not self.is_paused:
            def _safe_start_timer():
                if (
                    not self.is_paused
                    and not self._stop_event.is_set()
                    and getattr(self, "bitmap_ctrl", None)
                    and bool(self.bitmap_ctrl)
                ):
                    try:
                        self._timer.Start(self._timer_interval())
                    except Exception:
                        pass
            wx.CallAfter(_safe_start_timer)

    def _restart_video_pipe(self, seconds: float):
        """Replace only the ffmpeg picture pipe after an audio seek.

        set_position() calls this straight from wx seek-slider/shortcut
        handlers on the UI thread. The generation bump has to happen right
        here, synchronously, so _read_frames()'s already-running loop
        notices the mismatch and stops feeding stale frames on its very
        next iteration — but _kill_ffmpeg_locked() below it blocks on
        proc.wait(timeout=2), which used to run on that same UI thread and
        could freeze the whole window (NVDA/JAWS included) for up to two
        seconds on a single seek if the killed process was slow to reap.
        Everything past the generation bump is safe to defer to a
        background thread instead — _pipe_lock still serializes it against
        any other in-flight restart.
        """
        with self._pipe_lock:
            self._generation += 1
            self._eof_reached = False

        def _finish_restart():
            with self._pipe_lock:
                self._kill_ffmpeg_locked()
                self._drain_frames()
            self._start_video_pipe(max(0.0, seconds))

        threading.Thread(target=_finish_restart, daemon=True).start()

    def _read_frames(self, generation: int, proc):
        if proc is None or proc.stdout is None:
            return
        buf = b""
        try:
            while not self._stop_event.is_set() and generation == self._generation:
                chunk = proc.stdout.read(8192)
                if not chunk:
                    break
                buf += chunk
                frames, buf = extract_jpeg_frames(buf)
                for frame in frames:
                    # Queue backpressure keeps ffmpeg bounded while paused and
                    # prevents it from decoding the whole file ahead of the UI.
                    while not self._stop_event.is_set() and generation == self._generation:
                        try:
                            self._frame_queue.put(frame, timeout=0.5)
                            break
                        except queue.Full:
                            continue
        except Exception:
            pass
        finally:
            # Only the reader that still belongs to the active generation can
            # declare EOF; a reader killed by seek must not finish the new pipe.
            if generation == self._generation and not self._stop_event.is_set():
                self._eof_reached = True

    def _on_playback_finished(self):
        self.is_playing = False
        try:
            self._timer.Stop()
        except Exception:
            pass

    def _audio_still_active(self) -> bool:
        """True if the BASS channel reports anything other than fully
        stopped (playing, paused, or stalled) — used by _on_timer() to
        decide whether ffmpeg's frame pipe reaching EOF actually means
        "done" or not. Deliberately NOT Channel.is_playing (True only for
        BASS_ACTIVE_PLAYING specifically) — a channel the USER just
        paused would then read as "not active" too, which would make this
        wrongly finish/reset a deliberately-paused player."""
        ctrl = self._audio_ctrl()
        if ctrl is None:
            return False
        try:
            from sound_lib.external.pybass import BASS_ACTIVE_STOPPED
            return ctrl.is_active() != BASS_ACTIVE_STOPPED
        except Exception:
            return False

    # ── Internals: frame rendering (UI thread, via wx.Timer) ────────────

    def _on_timer(self, event):
        if not self.is_playing or self._stop_event.is_set() or not getattr(self, "bitmap_ctrl", None) or not bool(self.bitmap_ctrl):
            try:
                self._timer.Stop()
            except Exception:
                pass
            return
        try:
            frame_bytes = self._frame_queue.get_nowait()
        except queue.Empty:
            if self._eof_reached:
                # ffmpeg's frame pipe reaching EOF means "no more VIDEO to
                # show" — for a real video that's also roughly when the
                # audio ends, but for an audio-only status/message (no
                # video stream at all) ffmpeg has nothing to output and
                # hits EOF almost immediately, long before BASS is
                # actually done playing. Only really finish once the
                # audio itself has stopped too, instead of ending
                # (and dropping is_playing back to False) while the
                # audio the user is still listening to keeps going —
                # reported live as "pause always restarts instead of
                # pausing" for audio statuses, since is_playing had
                # already gone False on its own moments after starting.
                if self._audio_still_active():
                    return
                self._on_playback_finished()
            return
        try:
            if not getattr(self, "bitmap_ctrl", None) or not bool(self.bitmap_ctrl):
                self.stop()
                return
            img = wx.Image(io.BytesIO(frame_bytes), wx.BITMAP_TYPE_JPEG)
            if img.IsOk():
                # Scale to fit the control instead of letting wx clip the
                # frame to its top-left corner — see fit_frame_size().
                target_w, target_h = self.bitmap_ctrl.GetSize()
                fitted = fit_frame_size(
                    img.GetWidth(), img.GetHeight(), target_w, target_h
                )
                shown_w, shown_h = fitted if fitted is not None else (img.GetWidth(), img.GetHeight())
                if fitted is not None:
                    img = img.Scale(shown_w, shown_h, wx.IMAGE_QUALITY_NORMAL)
                # One-time box resize, on the first frame only: the caller's
                # box starts out a generic placeholder size (see e.g.
                # ConversationsPanel._VIDEO_BITMAP_SIZE), which almost never
                # matches this video's own aspect ratio — a portrait video in
                # a 4:3 box, say, ends up small and left-aligned with a big
                # blank gap filling the rest of the box, which reads as "the
                # video doesn't show completely" even though every pixel of
                # it is actually there (reported live, still, after the
                # scale-to-fit change above stopped the literal clipping).
                # Shrinking the box to the frame's own fitted size the first
                # time makes video match how a still photo is shown — sized
                # exactly to its own content, no leftover space. Done once,
                # not every frame, to avoid relaying out the panel at 12fps.
                if not self._box_sized:
                    self._box_sized = True
                    if self._on_frame_size is not None and (shown_w, shown_h) != (target_w, target_h):
                        try:
                            self._on_frame_size(shown_w, shown_h)
                        except Exception:
                            pass
                if bool(self.bitmap_ctrl):
                    self.bitmap_ctrl.SetBitmap(wx.Bitmap(img))
                    self.bitmap_ctrl.Refresh()
        except (RuntimeError, wx.wxAssertionError, Exception):
            pass

    # ── Internals: pause/resume ──────────────────────────────────────────

    def _pause(self):
        ctrl = self._audio_ctrl()
        if ctrl is not None:
            try:
                ctrl.pause()
            except Exception:
                pass
        self._timer.Stop()
        self.is_paused = True

    def _resume(self):
        ctrl = self._audio_ctrl()
        if ctrl is not None:
            try:
                ctrl.play()
            except Exception:
                pass
        self._timer.Start(self._timer_interval())
        self.is_paused = False

    def _drain_frames(self):
        while True:
            try:
                self._frame_queue.get_nowait()
            except queue.Empty:
                break

    def _kill_ffmpeg_locked(self):
        proc = self._ffmpeg_proc
        self._ffmpeg_proc = None
        if proc is not None:
            try:
                proc.kill()
            except Exception as exc:
                # Silently swallowed before this change — a report of
                # ffmpeg.exe processes piling up over a long session had
                # nothing in log.log to explain why. Usually benign (the
                # process had already exited on its own right as stop()
                # was called), but worth a trace either way.
                logging.warning(
                    "[video_player] failed to signal ffmpeg (pid=%s) to stop: %s",
                    proc.pid, exc,
                )
            try:
                proc.wait(timeout=2)
            except Exception as exc:
                # This one is the actual leak signal: kill() was sent but
                # the process did not exit within 2s, so it may still be
                # running after this call returns.
                logging.warning(
                    "[video_player] ffmpeg (pid=%s) did not exit within 2s "
                    "after being killed — it may still be running: %s",
                    proc.pid, exc,
                )

    def _kill_ffmpeg(self):
        with self._pipe_lock:
            self._kill_ffmpeg_locked()
