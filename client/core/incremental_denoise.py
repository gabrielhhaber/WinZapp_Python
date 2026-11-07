"""Incremental neural noise reduction while a voice message is being recorded.

The offline path (core.neural_noise_reduction) waits for the finished PCM and
then spends seconds of model time at send. This module runs the exact same
framing as the recording arrives: the recurrent state, the leading WINDOW of
zeros, the hop/overlap-add schedule and the MODEL_DELAY crop all mirror
NeuralDenoiser._enhance_48k, and the rate conversions feed the same
StreamingMonoResampler chunk by chunk (its output count and phase arithmetic
are packet-size independent by contract). The cleaned PCM16 is therefore
bit-exact against the offline path — covered by
tests/test_incremental_denoise.py against the real upstream code.

``spool_path`` streams the cleaned audio to a temporary file as it is produced
(the raw capture is retained by the caller exactly as before, so recording-time
RAM matches the current profile; the engine itself retains no per-recording
growth). Without ``spool_path`` the cleaned PCM accumulates in memory. The
effect on send-time peak and wait is a design goal, not a measured result.

``drain_capture_frames`` is the wx-free capture-pump controller: it feeds a
recording's chunk list to an IncrementalDenoiser as the chunks arrive, with
cooperative cancel, a bounded-lag abandonment policy and spool cleanup, so
the UI wiring in voice_recording.py stays a thin adapter and the policy is
testable without wx, audio hardware or a model file.

``IncrementalDenoiseController`` is the per-process owner of the single
incremental slot and of every session's send-authority state
(PREPARING/CANCELLED), held under one lock so that a user cancel
(message_actions.py) and the send thread's transfer to the MessageQueue are
mutually exclusive. One process hosts one account in WinZapp (main.py takes
the instance lock per account and background accounts run as separate
processes), so "one slot per process" and "one slot per account" coincide;
there is deliberately no inter-process coordination.

No user-facing behaviour lives here: engine fallback, announcements and the
original/raw retention strategy stay in the wiring (voice_recording.py).
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time

import numpy as np

from core import neural_noise_reduction as nn
from core.call_audio_resampling import StreamingMonoResampler

WINDOW = nn.WINDOW
HOP = nn.HOP
MODEL_DELAY = nn.MODEL_DELAY
MODEL_RATE = nn.MODEL_RATE


def _delay_skip(source: int, target: int) -> tuple[int, int]:
    """Same convention as nn._convert: FIR delay and initial samples to skip."""
    delay = math.ceil(32 * max(1, source / target))
    skip = round(delay * target / source)
    return delay, skip


class _ChannelStream:
    """One channel: input resample -> persistent-state hops -> output resample."""

    def __init__(self, denoiser: nn.NeuralDenoiser, rate: int):
        self.d = denoiser
        self.rate = rate
        self.state = denoiser._initial_state.copy()
        self.pending = np.zeros(WINDOW, np.float32)
        self.ola = np.zeros(WINDOW, np.float32)
        self.pos = 0
        self.r_in = StreamingMonoResampler() if rate != MODEL_RATE else None
        self.r_out = StreamingMonoResampler() if rate != MODEL_RATE else None
        self.delay_in, self.skip_in = _delay_skip(rate, MODEL_RATE)
        self.delay_out, self.skip_out = _delay_skip(MODEL_RATE, rate)
        self.fed48 = 0           # 48 kHz samples poured into the hop stream
        self.passed48 = 0        # enhanced 48 kHz samples emitted downstream
        self.out_pending = np.empty(0, np.float32)

    def _hop(self) -> None:
        si, sti, so, sto = self.d._names
        spec = np.fft.rfft(self.pending[:WINDOW] * nn._WINDOW_SHAPE)
        frame = np.stack((spec.real, spec.imag), -1).astype(np.float32)[None, None]
        enhanced, self.state = self.d._session.run([so, sto], {si: frame, sti: self.state})
        bins = enhanced[0, 0, :, 0] + 1j * enhanced[0, 0, :, 1]
        self.ola += (np.fft.irfft(bins, WINDOW) * nn._WINDOW_SHAPE).astype(np.float32)
        left = max(0, WINDOW + MODEL_DELAY - self.pos)
        if left < HOP:
            self._emit48(self.ola[left:HOP].copy())
        self.ola[:HOP] = self.ola[HOP:]
        self.ola[HOP:] = 0
        self.pending = self.pending[HOP:]
        self.pos += HOP

    def feed(self, x: np.ndarray, wanted48: int | None = None) -> None:
        if self.r_in is not None:
            y = self.r_in.process(np.asarray(x, np.float32), self.rate, MODEL_RATE)
            if self.skip_in:
                # Consume only what this chunk covers: a fragment smaller than
                # the pending skip must not drop the rest of the skip.
                k = min(self.skip_in, y.size)
                y = y[k:]
                self.skip_in -= k
        else:
            y = np.asarray(x, np.float32)
        if wanted48 is not None:
            y = y[: max(0, wanted48 - self.fed48)]
        self.fed48 += y.size
        if y.size:
            self.pending = np.concatenate((self.pending, y))
        while self.pending.size >= WINDOW:
            self._hop()

    def _emit48(self, e: np.ndarray) -> None:
        self.passed48 += e.size
        if self.r_out is not None:
            o = self.r_out.process(e, MODEL_RATE, self.rate)
            if self.skip_out:
                k = min(self.skip_out, o.size)
                o = o[k:]
                self.skip_out -= k
        else:
            o = e
        if o.size:
            self.out_pending = np.concatenate((self.out_pending, o))

    def finish(self, frames_in: int) -> None:
        wanted48 = frames_in * MODEL_RATE // self.rate
        # 1. Input resampler tail: the same zeros nn._convert appends.
        if self.r_in is not None:
            self.feed(np.zeros(self.delay_in + 1, np.float32), wanted48)
        # 2. Hop-stream flush with the offline path's exact tail
        #    (MODEL_DELAY + 2*WINDOW zeros); _enhance_48k crops the overrun
        #    with out[first:first+count], the caller crops the same way.
        self.pending = np.concatenate((self.pending, np.zeros(MODEL_DELAY + 2 * WINDOW, np.float32)))
        while self.pending.size >= WINDOW and self.passed48 < wanted48:
            self._hop()
        # 3. Output resampler tail.
        if self.r_out is not None:
            o = self.r_out.process(np.zeros(self.delay_out + 1, np.float32), MODEL_RATE, self.rate)
            if self.skip_out:
                k = min(self.skip_out, o.size)
                o = o[k:]
                self.skip_out -= k
            if o.size:
                self.out_pending = np.concatenate((self.out_pending, o))


class IncrementalDenoiser:
    """feed() interleaved PCM16 chunks as they are captured; finish() at send.

    Returns (spool_path | cleaned_bytes, "neural") from finish(). Raw-capture
    retention, engine fallback and announcements are the wiring's decisions.
    """

    def __init__(self, rate: int, channels: int, denoiser: nn.NeuralDenoiser,
                 spool_path: str | None = None):
        self.rate = rate
        self.channels = channels
        self.d = denoiser
        self._ch = [_ChannelStream(denoiser, rate) for _ in range(channels)]
        self._frames = 0
        self._leftover = b""
        self._file = open(spool_path, "wb") if spool_path else None
        self._retain = bytearray() if spool_path is None else None
        self._drained = 0

    def feed(self, pcm16: bytes) -> None:
        data = self._leftover + pcm16
        frame_bytes = 2 * self.channels
        frames = len(data) // frame_bytes
        self._leftover = data[frames * frame_bytes:]
        if not frames:
            return
        x = np.frombuffer(data[: frames * frame_bytes], dtype="<i2").reshape(frames, self.channels)
        x = x.astype(np.float32) / 32768.0
        for i, ch in enumerate(self._ch):
            ch.feed(x[:, i])
        self._frames += frames
        self._drain()

    def _drain(self) -> None:
        ready = min(c.out_pending.size for c in self._ch)
        if ready <= 0:
            return
        cols = []
        for c in self._ch:
            cols.append(c.out_pending[:ready])
            c.out_pending = c.out_pending[ready:]
        y = np.stack(cols, axis=1)
        if not np.isfinite(y).all():
            # Same guard as the offline path: never clip a blown-up output
            # into a full-scale spike.
            raise ValueError("the model produced non-finite samples")
        block = (np.clip(y, -1.0, 1.0) * 32767.0).round().astype("<i2").tobytes()
        if self._file is not None:
            self._file.write(block)
        else:
            self._retain += block
        self._drained += ready

    def finish(self) -> tuple[bytes | str, str]:
        for c in self._ch:
            c.finish(self._frames)
        remaining = self._frames - self._drained
        if remaining < 0:
            raise AssertionError("incremental denoise emitted past the recording length")
        for c in self._ch:
            c.out_pending = c.out_pending[:remaining]
        self._drain()
        if self._drained != self._frames:
            raise AssertionError(
                f"incremental denoise emitted {self._drained} of {self._frames} frames")
        if self._file is not None:
            if self._leftover:
                self._file.write(self._leftover)
            self._file.close()
            return self._file.name, "neural"
        return bytes(self._retain) + self._leftover, "neural"

    def abort(self) -> None:
        """Cancel without finishing (recording discarded or drain failed)."""
        if self._file is not None:
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None


#: Cap on how far the cleaner may fall behind capture before the session is
#: abandoned to the offline path, in seconds of captured audio. NOT validated
#: on any target machine: a conservative placeholder to be set from the
#: Windows integration measurements. It bounds pending audio, never time.
DEFAULT_MAX_LAG_SECONDS = 30


def drain_capture_frames(session: dict, worker: IncrementalDenoiser,
                         transform=None, poll_seconds: float = 0.01) -> None:
    """Feed a capture session's chunk list to *worker* until stop or cancel.

    *session* is the wiring's IPC dict: ``frames`` (the list the capture
    callback appends to), ``rate``, ``ch``, ``spool``, ``stop``, ``cancel``,
    ``failed``, ``worker``. The list is read-only here: the callback owns
    appends and this function never mutates or rebinds it.

    Bounded lag: bytes are counted as chunks become visible in the list, so
    an append between polls is counted at the next poll (at most
    *poll_seconds* late — the threshold is never evaluated against stale
    state older than that). When counted-but-unfed bytes pass the lag cap,
    the session is marked failed and abandoned. Abandonment and cancel are
    cooperative: a worker blocked inside a native model or disk call is not
    preempted — the loop notices once that call returns, exactly as the
    offline path's own model call cannot be interrupted either.

    On cancel or failure the worker is aborted (its file handle closed) and
    the spool deleted; the send path then falls back to the offline pass,
    which reprocesses the retained raw frames — no captured audio is lost.
    On normal completion (``stop`` with every chunk fed) the worker and its
    spool stay live and ownership passes to the send thread, which joins
    this loop before touching them.
    """
    frames = session["frames"]
    max_lag = (session["rate"] * session["ch"] * 2
               * session.get("max_lag_seconds", DEFAULT_MAX_LAG_SECONDS))
    bytes_per_second = session["rate"] * session["ch"] * 2
    i = 0
    seen_index = 0
    seen_bytes = 0
    fed_bytes = 0
    try:
        while not session["cancel"]:
            n = len(frames)
            while seen_index < n:
                seen_bytes += len(frames[seen_index])
                seen_index += 1
            if seen_bytes - fed_bytes > max_lag:
                logging.warning(
                    "[noise_reduction] incremental session abandoned: cleaner fell "
                    "%.1fs of audio behind capture; offline path takes over at send",
                    (seen_bytes - fed_bytes) / bytes_per_second)
                session["failed"] = True
                break
            while i < n and not session["cancel"]:
                chunk = frames[i]
                if transform is not None:
                    chunk = transform(chunk)
                worker.feed(chunk)
                fed_bytes += len(frames[i])
                i += 1
            if session["stop"] and i >= len(frames):
                break
            time.sleep(poll_seconds)
    except Exception:
        logging.exception("[noise_reduction] incremental drain failed; offline path remains")
        session["failed"] = True
    finally:
        if session["cancel"] or session["failed"]:
            try:
                worker.abort()
            except Exception:
                pass
            try:
                os.unlink(session["spool"])
            except OSError:
                pass
            session["worker"] = None
            # The drain OWNED this cleanup: a send-time reaper (spawned when
            # the send went basic while the drain was still alive) must
            # never repeat it. A normal stop exit keeps worker+spool for
            # the send and leaves the flag unset.
            session["drain_cleaned"] = True


# ── Per-process slot + send-authority registry ────────────────────────────

PREPARING = "preparing"
CANCELLED = "cancelled"


class IncrementalDenoiseController:
    """Single incremental slot and per-session send authority, one per process.

    The wiring acquires the slot BEFORE loading a model or starting a drain
    thread; a busy slot is never waited on — the recording simply uses the
    current offline path at send. The slot is released only by the party
    that can confirm the worker is dead and the spool cleaned: the drain
    itself after a cancel/failure exit, or the send thread after finish()
    and spool consumption. A drain blocked in a native call never gets
    there, so the slot stays taken and every later recording goes offline
    until the process ends — declared, not hidden.

    Send authority: a session is PREPARING from recording start until the
    send thread's transfer() succeeds (the moment the note is handed to the
    MessageQueue, whose own cancel machinery takes over). revoke() — the
    user deleting the pending row — wins atomically against transfer()
    under the same lock and flags the session dict itself (``revoked`` /
    ``cancel``), so the send thread never needs to re-read the registry
    after the drain's cleanup may have popped the entry. A revoked session
    is never enqueued: the late worker cannot send.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._sessions = {}   # local_id -> drain IPC session dict
        self._states = {}     # local_id -> PREPARING | CANCELLED
        self._slot_owner = None

    def register(self, local_id, session) -> None:
        """Send authority + capture identity for EVERY note that enters the
        incremental system — with or without the neural slot. A note whose
        slot request fails still gets confirmed-close tracking, revoke /
        cancel semantics and the echo-matcher exclusion."""
        with self._lock:
            self._sessions[local_id] = session
            self._states[local_id] = PREPARING

    def acquire_slot(self, local_id, session) -> bool:
        """Register the note AND take the single slot without waiting.
        False = occupied."""
        with self._lock:
            if self._slot_owner is not None:
                return False
            self._slot_owner = local_id
            self._sessions[local_id] = session
            self._states[local_id] = PREPARING
            return True

    def revoke(self, local_id) -> bool:
        """User deleted the pending note before transfer. True = the session
        was PREPARING and is now revoked: the caller removes the row plainly
        (no echo anchor — nothing was ever enqueued) and the send thread
        will observe the flags and never enqueue. False = unknown id or
        already transferred (the queue's own cancel path applies)."""
        with self._lock:
            if self._states.get(local_id) != PREPARING:
                return False
            self._states[local_id] = CANCELLED
            session = self._sessions.get(local_id)
            if session is not None:
                session["revoked"] = True
                session["cancel"] = True
            return True

    def discard(self, local_id) -> None:
        """The recording was dropped before send (chat switch, panel destroy,
        empty recording): cancel the session; the drain cleans up and
        releases the slot on exit. If the drain already exited (session
        reference gone), the entry is forgotten here — no one else will."""
        with self._lock:
            if self._states.get(local_id) == PREPARING:
                self._states[local_id] = CANCELLED
                session = self._sessions.get(local_id)
                if session is not None:
                    session["cancel"] = True
                    if session.get("thread") is None:
                        # No drain thread will ever run for this session
                        # (basic-only note, no slot): nobody else cleans the
                        # entry up — forget it right here.
                        self._sessions.pop(local_id, None)
                        self._states.pop(local_id, None)
                else:
                    self._states.pop(local_id, None)

    def is_preparing(self, local_id) -> bool:
        with self._lock:
            return self._states.get(local_id) == PREPARING

    def transfer(self, local_id, enqueue=None) -> bool:
        """Hand the note to the MessageQueue. Atomic with revoke().

        The *enqueue* callable runs UNDER this lock, so a user cancel cannot
        slip between the authority check and the real hand-off: when True is
        returned the message IS in the queue and the queue's own cancel
        machinery gives a definitive answer. False = revoked or unknown,
        nothing was enqueued."""
        with self._lock:
            if self._states.get(local_id) != PREPARING:
                return False
            if enqueue is not None:
                enqueue()
            self._states.pop(local_id, None)
            self._sessions.pop(local_id, None)
            return True

    def release_slot(self, local_id) -> None:
        """Free the SLOT — nothing else. Only called by the party that
        confirmed worker death + spool cleanup (see class docstring).

        Resource lifetime and send-authority lifetime are DIFFERENT: the
        session reference and the PREPARING state are KEPT so that a
        revoke() after release still reaches the session's flags (the send
        thread may be waiting on them) and still wins atomically against
        transfer(). The entry leaves only through transfer() or forget().
        """
        with self._lock:
            if self._slot_owner == local_id:
                self._slot_owner = None

    def forget(self, local_id) -> None:
        """Terminal cleanup of the send-authority entry: the send thread
        calls it once the outcome is decided (revoked, or failed before
        enqueue). Never touches slot ownership — the slot is freed only by
        release_slot() after confirmed cleanup, so a hung drain keeps its
        slot even when its registry entry is gone."""
        with self._lock:
            self._sessions.pop(local_id, None)
            self._states.pop(local_id, None)

    def shutdown(self) -> None:
        """real_exit: cancel every live session AND every outstanding send
        authority — including released-but-still-PREPARING entries (an
        abandoned-to-offline send in flight), so no late transfer can
        enqueue while the process is dying. The threads are daemonic and
        die with the process; this only lets a healthy drain clean its own
        spool on the way out and makes the send threads abort."""
        with self._lock:
            sessions = list(self._sessions.values())
            for local_id, state in self._states.items():
                if state == PREPARING:
                    self._states[local_id] = CANCELLED
        for session in sessions:
            session["revoked"] = True
            session["cancel"] = True
