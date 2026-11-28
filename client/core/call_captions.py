"""The pure half of live captions in calls (ACC-04): no wx, no hardware.

The captions window shows what the other person says, as whole phrases, on a
list the user reads when they choose to. What is here decides *what* a phrase
is and *what* may be shown or said; the threads are ``call_captions_engine.py``,
the Whisper run is ``call_captions_transcriber.py`` and the window is
``ui/call_captions_window.py``.

Decisions this file carries:

* **The call's audio path pays one append.** ``RemoteTap.offer()`` is called
  from ``CallAudioSession.enqueue_remote_audio()``, i.e. on the thread that
  feeds the call's own jitter buffer. It copies nothing the caller still owns,
  takes no lock, never blocks, never raises and drops the oldest chunk when the
  consumer is slow. Everything else (resampling, voice detection, Whisper) runs
  on the engine's own threads.
* **Phrases, never words.** ``PhraseSegmenter`` cuts the speech at a pause
  (``END_SILENCE_MS``) or at ``MAX_PHRASE_SECONDS``; only a whole phrase is
  transcribed, listed or said. A phrase cut by the length cap is joined to its
  continuation by ``CaptionLog`` *before* it becomes a row, so a list row is
  never rewritten afterwards (a screen reader reads a rewritten focused row
  aloud, docs/traps/screen-reader-speech.md).
* **Speech is opt-in and rationed.** ``SpeechCoalescer`` holds what to say so
  that a burst of phrases is one utterance at most every ``MIN_SPEECH_GAP``
  seconds; the list always has everything.
* **The call comes first.** ``AudioHealthGuard`` compares the call's own
  underrun rate before the captions started with the rate while they run, and
  asks the engine to stop when the captions made it worse.
* **Content is memory only** and is never in a log line.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass

import numpy as np

from core import dictation

TARGET_RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = TARGET_RATE * FRAME_MS // 1000

#: Chunks the tap keeps for a slow consumer (~21-60 ms each: a few seconds).
TAP_MAX_CHUNKS = 200

#: Voice detection (energy based, on the 16 kHz copy).
ABS_MIN_RMS = 250.0
NOISE_FACTOR = 2.5
NOISE_ALPHA = 0.05
START_FRAMES = 3
PREROLL_FRAMES = 8
END_SILENCE_MS = 700
TAIL_KEEP_FRAMES = 6
MIN_VOICED_MS = 300
MAX_PHRASE_SECONDS = 12.0

#: Phrases waiting for Whisper; a longer wait drops the oldest.
QUEUE_LIMIT = 3

#: At most this many pieces are joined into one row.
MAX_JOIN = 3

MIN_SPEECH_GAP = 1.5
SPEECH_LIMIT = dictation.SPOKEN_LIMIT

REASON_NO_CALL = "call_captions_no_call"
REASON_BUSY = "call_captions_busy"

I18N_KEYS = (
    "call_captions_button",
    "call_captions_title",
    "call_captions_list_label",
    "call_captions_status_listening",
    "call_captions_status_dropped",
    "call_captions_status_ended",
    "call_captions_status_stopped",
    "call_captions_started",
    "call_captions_stopped",
    "call_captions_protected",
    "call_captions_failed",
    "call_captions_speak",
    "call_captions_empty",
    REASON_NO_CALL,
    REASON_BUSY,
)


# ── The tap ──────────────────────────────────────────────────────────────────


class RemoteTap:
    """Where the call's received PCM is copied for the captions.

    ``offer`` runs on the call's audio thread and must stay O(1) and silent.
    ``take`` runs on the engine's thread. ``deque.append``/``popleft`` are
    atomic, so no lock is needed (and none may be added: a lock is a way for
    this module to delay the call).
    """

    def __init__(self, max_chunks: int = TAP_MAX_CHUNKS):
        self._chunks: deque = deque(maxlen=max_chunks)
        self.active = True
        self.dropped_chunks = 0

    def offer(self, pcm, rate) -> None:
        try:
            if not self.active:
                return
            if len(self._chunks) == self._chunks.maxlen:
                self.dropped_chunks += 1
            self._chunks.append((bytes(pcm), int(rate)))
        except Exception:
            # Nothing here may ever reach the call.
            pass

    def take(self) -> list:
        out = []
        for _ in range(len(self._chunks)):
            try:
                out.append(self._chunks.popleft())
            except IndexError:
                break
        return out

    def close(self) -> None:
        self.active = False
        self._chunks.clear()


# ── 16 kHz mono ──────────────────────────────────────────────────────────────


class To16k:
    """Streaming s16le mono at any rate -> int16 at 16 kHz.

    Integer ratios (48 kHz, the page's rate) average blocks and keep the
    remainder for the next chunk; other rates use linear interpolation per
    chunk. Good enough for speech recognition, and nothing like the call's own
    playback resampler: this copy is never heard.
    """

    def __init__(self):
        self._carry = np.zeros(0, dtype=np.int16)
        self._carry_rate = 0

    def process(self, pcm: bytes, rate: int) -> np.ndarray:
        rate = int(rate)
        if rate < 8000 or rate > 384000:
            return np.zeros(0, dtype=np.int16)
        samples = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2")
        if rate == TARGET_RATE:
            self._carry = np.zeros(0, dtype=np.int16)
            return samples.astype(np.int16)
        if rate % TARGET_RATE == 0:
            if rate != self._carry_rate:
                self._carry = np.zeros(0, dtype=np.int16)
            self._carry_rate = rate
            ratio = rate // TARGET_RATE
            data = np.concatenate([self._carry, samples])
            usable = len(data) // ratio * ratio
            self._carry = data[usable:].astype(np.int16)
            if not usable:
                return np.zeros(0, dtype=np.int16)
            return data[:usable].reshape(-1, ratio).mean(axis=1).astype(np.int16)
        if not len(samples):
            return np.zeros(0, dtype=np.int16)
        count = max(1, int(len(samples) * TARGET_RATE / rate))
        positions = np.linspace(0, len(samples) - 1, count)
        return np.interp(positions, np.arange(len(samples)), samples).astype(np.int16)


# ── Phrases ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Phrase:
    pcm: bytes
    seconds: float
    #: Ended by the length cap, not by a pause: the next one continues it.
    cut: bool = False


def _rms(frame: np.ndarray) -> float:
    if not len(frame):
        return 0.0
    data = frame.astype(np.float64)
    return float(np.sqrt(np.mean(data * data)))


class PhraseSegmenter:
    """Cut a stream of 16 kHz int16 into phrases at the pauses."""

    def __init__(self, max_phrase_seconds: float = MAX_PHRASE_SECONDS):
        self._rest = np.zeros(0, dtype=np.int16)
        self._lead: deque = deque(maxlen=PREROLL_FRAMES + START_FRAMES)
        self._frames: list = []
        self._active = False
        self._run = 0
        self._silence = 0
        self._voiced = 0
        self._floor = 0.0
        self._max_frames = max(1, int(max_phrase_seconds * 1000 / FRAME_MS))
        self._end_frames = max(1, END_SILENCE_MS // FRAME_MS)
        self._min_voiced = max(1, MIN_VOICED_MS // FRAME_MS)

    @property
    def in_speech(self) -> bool:
        return self._active

    def feed(self, samples: np.ndarray) -> list:
        out = []
        data = np.concatenate([self._rest, samples]) if len(self._rest) else samples
        usable = len(data) // FRAME_SAMPLES * FRAME_SAMPLES
        self._rest = data[usable:]
        for start in range(0, usable, FRAME_SAMPLES):
            phrase = self._frame(data[start:start + FRAME_SAMPLES])
            if phrase is not None:
                out.append(phrase)
        return out

    def flush(self):
        """The phrase in progress, if it holds enough speech."""
        phrase = self._emit(cut=False, trim=True) if self._active else None
        self._reset()
        return phrase

    def _threshold(self) -> float:
        return max(ABS_MIN_RMS, self._floor * NOISE_FACTOR)

    def _frame(self, frame: np.ndarray):
        loud = _rms(frame) > self._threshold()
        if not self._active:
            self._lead.append(frame)
            if loud:
                self._run += 1
                if self._run >= START_FRAMES:
                    self._active = True
                    self._frames = list(self._lead)
                    self._voiced = self._run
                    self._silence = 0
                    self._lead.clear()
            else:
                self._run = 0
                self._floor = (1 - NOISE_ALPHA) * self._floor + NOISE_ALPHA * _rms(frame)
            return None
        self._frames.append(frame)
        if loud:
            self._voiced += 1
            self._silence = 0
        else:
            self._silence += 1
        if self._silence >= self._end_frames:
            phrase = self._emit(cut=False, trim=True)
            self._reset()
            return phrase
        if len(self._frames) >= self._max_frames:
            phrase = self._emit(cut=True, trim=False)
            self._frames = []
            self._voiced = 0
            self._silence = 0
            return phrase
        return None

    def _emit(self, *, cut: bool, trim: bool):
        frames = self._frames
        if trim and self._silence > TAIL_KEEP_FRAMES:
            frames = frames[:len(frames) - (self._silence - TAIL_KEEP_FRAMES)]
        if self._voiced < self._min_voiced or not frames:
            return None
        pcm = np.concatenate(frames).astype("<i2").tobytes()
        return Phrase(pcm, len(pcm) / 2 / TARGET_RATE, cut)

    def _reset(self) -> None:
        self._active = False
        self._frames = []
        self._lead.clear()
        self._run = self._silence = self._voiced = 0


class PhraseQueue:
    """The phrases waiting for Whisper: bounded, oldest dropped, never blocking
    the producer."""

    def __init__(self, limit: int = QUEUE_LIMIT):
        self._limit = max(1, limit)
        self._items: deque = deque()
        self._cond = threading.Condition()
        self._closed = False
        self.dropped = 0

    def put(self, phrase) -> int:
        """Queue a phrase; how many older ones were dropped for it."""
        with self._cond:
            if self._closed:
                return 0
            dropped = 0
            while len(self._items) >= self._limit:
                self._items.popleft()
                dropped += 1
            self._items.append(phrase)
            self.dropped += dropped
            self._cond.notify()
            return dropped

    def get(self, timeout: float):
        with self._cond:
            if not self._items and not self._closed:
                self._cond.wait(timeout)
            if self._items:
                return self._items.popleft()
            return None

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._items.clear()
            self._cond.notify_all()

    def __len__(self) -> int:
        with self._cond:
            return len(self._items)


# ── Rows ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CaptionEntry:
    text: str
    speaker: str = ""
    #: Seconds since the captions started.
    at: float = 0.0


def has_content(text: str) -> bool:
    """Whether `text` has a letter or digit: Whisper answers noise with "..."
    or a lone symbol."""
    return any(ch.isalnum() for ch in text)


def format_row(entry: CaptionEntry) -> str:
    """The row as shown and read: "Name: text", or the text alone."""
    return f"{entry.speaker}: {entry.text}" if entry.speaker else entry.text


def transcript_text(entries) -> str:
    """The whole transcript, one row per line (copy and save)."""
    return "\n".join(format_row(entry) for entry in entries)


class CaptionLog:
    """Turns transcribed phrases into rows: joins the pieces of a phrase that
    the length cap split, drops empties and an immediate repeat."""

    def __init__(self, max_join: int = MAX_JOIN):
        self._max_join = max_join
        self._pending = None  # [text, speaker, at, pieces]
        self._last = ""

    def add(self, text, speaker: str = "", cut: bool = False, at: float = 0.0) -> list:
        """Entries completed by this phrase (0, 1 or 2)."""
        out = []
        text = dictation.normalize_transcript(text)
        if not has_content(text):
            return out
        if self._pending is not None and self._pending[1] != speaker:
            out.append(self._complete())
        if self._pending is not None:
            self._pending[0] = f"{self._pending[0]} {text}"
            self._pending[3] += 1
        else:
            if text.casefold() == self._last:
                return out
            self._pending = [text, speaker, at, 1]
        if not cut or self._pending[3] >= self._max_join:
            out.append(self._complete())
        return out

    def flush(self):
        """The row still waiting for its continuation, or None."""
        return self._complete() if self._pending is not None else None

    def _complete(self) -> CaptionEntry:
        text, speaker, at, _pieces = self._pending
        self._pending = None
        self._last = text.casefold()
        return CaptionEntry(text, speaker, at)


# ── Speech ───────────────────────────────────────────────────────────────────


class SpeechCoalescer:
    """What to say, at most once per `min_gap`: a burst of phrases is one
    utterance, and an utterance longer than `limit` keeps its latest words."""

    def __init__(self, min_gap: float = MIN_SPEECH_GAP, limit: int = SPEECH_LIMIT):
        self._min_gap = min_gap
        self._limit = limit
        self._pending: list = []
        self._last = None

    def add(self, text: str, now: float):
        self._pending.append(text)
        return self.due(now)

    def due(self, now: float):
        if not self._pending:
            return None
        if self._last is not None and now - self._last < self._min_gap:
            return None
        text = " ".join(self._pending)
        self._pending = []
        self._last = now
        if len(text) > self._limit:
            text = text[-self._limit:]
            if " " in text:
                text = text[text.index(" ") + 1:]
        return text

    @property
    def has_pending(self) -> bool:
        return bool(self._pending)

    def clear(self) -> None:
        self._pending = []


# ── The call's health ────────────────────────────────────────────────────────


class AudioHealthGuard:
    """Stops the captions when the call's audio got worse while they ran.

    The jitter buffer counts underruns. A call may underrun on its own (a slow
    remote tap measured ~2.8/s), so the guard first measures that rate with the
    captions not yet transcribing (`CALIBRATION_SECONDS`) and only then asks
    for a stop if a window holds clearly more than the baseline predicts.
    """

    CALIBRATION_SECONDS = 6.0
    WINDOW_SECONDS = 10.0
    ALLOWANCE = 6

    def __init__(self, calibration: float = CALIBRATION_SECONDS,
                 window: float = WINDOW_SECONDS, allowance: int = ALLOWANCE):
        self._calibration = calibration
        self._window = window
        self._allowance = allowance
        self._samples: deque = deque()
        self._start = None
        self._first = 0
        self._baseline = None

    def update(self, underruns: int, now: float) -> bool:
        """True when the captions must stop."""
        if self._start is None:
            self._start = now
            self._first = underruns
        self._samples.append((now, underruns))
        while self._samples and now - self._samples[0][0] > self._window:
            self._samples.popleft()
        if self._baseline is None:
            if now - self._start >= self._calibration:
                span = max(now - self._start, 1e-6)
                self._baseline = max(0.0, (underruns - self._first) / span)
            return False
        grown = underruns - self._samples[0][1]
        covered = now - self._samples[0][0]
        return grown > self._baseline * covered * 2 + self._allowance


# ── Whether it may start ─────────────────────────────────────────────────────


def refusal_key(in_call: bool, call_active: bool, transcription_busy: bool,
                dictating: bool):
    """The i18n key of why live captions may not start now, or None."""
    if not in_call or not call_active:
        return REASON_NO_CALL
    if transcription_busy or dictating:
        return REASON_BUSY
    return None
