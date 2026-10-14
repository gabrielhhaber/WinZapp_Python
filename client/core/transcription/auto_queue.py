"""Transcribing received voice messages on their own: who is eligible, in what order.

The setting `transcription.auto_transcribe_received` (Settings > Local
transcription, off by default) makes WinZapp transcribe each voice message
(and each video, from its sound track) as it arrives and keep the text in the message's line, as if the user had pressed
the inline-transcription key (stored.record_from_result(..., inline=True)).

This module is the part with no wx and no MainWindow: the rule for which
messages qualify, and a serial queue so that a burst of voice notes is run one
after another, never in parallel (each run may load a model and decode audio —
two at once would double the memory the user's machine is already spending).
The glue that builds the run and stores the result is
main_window/auto_transcription.py.

Rules, each one a way this could otherwise annoy or harm:

* **Only what just arrived.** A message older than `MAX_AGE_SECONDS` (a replay
  after a reconnect, a history sync) is left alone: transcribing a backlog the
  user did not ask for would keep the machine busy for minutes.
* **Only incoming audio and video, only once.** Our own messages and messages that
  already carry a transcription decision (a text, or a deletion) are skipped.
* **Not the very long ones.** Past `MAX_AUDIO_SECONDS` the user can still ask
  for it by hand; doing it unasked would be an hour of CPU for an audiobook.
* **Bounded.** At most `MAX_PENDING` waiting; a burst beyond that is dropped
  (the newest), not queued without end.
* **Paused when the setup is wrong.** A run that fails because there is no
  usable model or program stops the queue for `PAUSE_SECONDS` instead of
  failing the same way for every message; the glue says so once.
* **Once is once, not once per pause.** The pause ends and the next message
  tries again; while the fault is still there that is a new pause every
  quarter of an hour, and a sentence each time is a notice that keeps coming
  back out of nowhere for a user who asked for nothing (`PauseNotices`). It is
  said again only after a run worked or the settings were applied.
* **A full memory is not a wrong setup.** "Automatic" finding no model that
  fits the memory free at that moment comes back as MODEL_NOT_INSTALLED too;
  it is about the moment, the next message may fit, and it neither pauses nor
  speaks (`pauses_the_queue(error, resolution)`).
"""

import logging
import queue
import threading
import time

from core.transcription import errors, message_audio, preferences
from core.transcription.stored import TRANSCRIPTION_KEY

#: The key inside the settings.json "transcription" section.
SETTING_AUTO_TRANSCRIBE = "auto_transcribe_received"

MAX_AGE_SECONDS = 300.0
MAX_AUDIO_SECONDS = 600
MAX_PENDING = 10
PAUSE_SECONDS = 900.0

ACCEPTED = "accepted"
FULL = "full"
DUPLICATE = "duplicate"
PAUSED = "paused"
STOPPED = "stopped"

#: Failures that say "this machine is not set up to transcribe": the next
#: message would fail the same way, so the queue pauses instead.
SETUP_FAILURES = frozenset((
    errors.MODEL_NOT_INSTALLED,
    errors.MODEL_CORRUPTED,
    errors.BACKEND_MISSING,
    errors.WHISPER_CPP_NOT_INSTALLED,
    errors.WHISPER_CPP_CORRUPTED,
    errors.EXTERNAL_MODEL_MISSING,
    errors.EXTERNAL_MODEL_CHANGED,
    errors.PRECISION_UNSUPPORTED,
))


def enabled(settings) -> bool:
    """The stored choice; only a real True turns it on."""
    section = settings.get("transcription") if isinstance(settings, dict) else None
    return isinstance(section, dict) and section.get(SETTING_AUTO_TRANSCRIBE) is True


#: The message types transcribed on arrival: voice notes and audio files, and
#: videos (their sound track). A document is left for the user to ask for.
AUTO_TYPES = ("audioMessage", "videoMessage")


def audio_seconds(msg) -> int:
    """The recording's length as WhatsApp states it, 0 when it does not."""
    return message_audio.media_seconds(msg)


def eligible(msg, now=None) -> bool:
    """Whether `msg` is one the setting should transcribe."""
    if not isinstance(msg, dict):
        return False
    key = msg.get("key") or {}
    if key.get("fromMe") or not key.get("id"):
        return False
    if msg.get("messageType") not in AUTO_TYPES or not message_audio.is_transcribable(msg):
        return False
    if TRANSCRIPTION_KEY in msg:
        return False
    if audio_seconds(msg) > MAX_AUDIO_SECONDS:
        return False
    try:
        arrived = float(msg.get("messageTimestamp") or 0)
    except (TypeError, ValueError):
        arrived = 0.0
    # A message with no timestamp has no age to judge: it just arrived live.
    if arrived and (time.time() if now is None else now) - arrived > MAX_AGE_SECONDS:
        return False
    return True


def pauses_the_queue(error, resolution=None) -> bool:
    """Whether a failure means "not set up" rather than "this message".

    `resolution` is the run's own (MessageTranscription.resolution), when the
    caller has it: with it, "nothing fits the memory free right now" is told
    apart from "nothing is installed", which carry the same code.
    """
    code = getattr(error, "code", None)
    if code not in SETUP_FAILURES:
        return False
    return not nothing_fits_right_now(code, resolution)


def nothing_fits_right_now(code, resolution) -> bool:
    """Whether MODEL_NOT_INSTALLED is "automatic" finding no model for the
    memory that was free when it looked (preferences.MODEL_NONE_NOTHING_FITS).
    Unmeasured memory with nothing installed is not this: that one is fixed
    by downloading a model."""
    return (
        code == errors.MODEL_NOT_INSTALLED
        and resolution is not None
        and getattr(resolution, "model_id", None) is None
        and getattr(resolution, "model_none_reason", None)
        == preferences.MODEL_NONE_NOTHING_FITS
    )


class PauseNotices:
    """Which setup faults were already said, so that each is said once."""

    def __init__(self):
        self._lock = threading.Lock()
        self._said = set()

    def first(self, code) -> bool:
        """True the first time `code` is asked about since `forget()`."""
        with self._lock:
            if code in self._said:
                return False
            self._said.add(code)
            return True

    def forget(self) -> None:
        """Something changed (a run worked, the settings were applied): the
        next fault is news again."""
        with self._lock:
            self._said.clear()


class AutoTranscriptionQueue:
    """Runs `run_one(item)` for submitted items, one at a time, in order."""

    def __init__(self, run_one, max_pending=MAX_PENDING, clock=time.monotonic,
                 start_worker=True):
        self._run_one = run_one
        self._max_pending = int(max_pending)
        self._clock = clock
        self._lock = threading.Lock()
        self._queue = queue.Queue()
        self._pending_keys = set()
        self._paused_until = 0.0
        self._stopped = False
        self._thread = None
        if start_worker:
            self._thread = threading.Thread(
                target=self._work, daemon=True, name="winzapp-auto-transcription")
            self._thread.start()

    # ── Submitting ───────────────────────────────────────────────────────────

    def submit(self, key, item) -> str:
        with self._lock:
            if self._stopped:
                return STOPPED
            if self._clock() < self._paused_until:
                return PAUSED
            if key in self._pending_keys:
                return DUPLICATE
            if len(self._pending_keys) >= self._max_pending:
                return FULL
            self._pending_keys.add(key)
        self._queue.put((key, item))
        return ACCEPTED

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._pending_keys)

    # ── Pausing ──────────────────────────────────────────────────────────────

    def pause(self, seconds=PAUSE_SECONDS) -> bool:
        """Stop accepting and running for `seconds`. True when this starts a
        pause (False when one is already running), so the caller says it once."""
        with self._lock:
            now = self._clock()
            already = now < self._paused_until
            self._paused_until = max(self._paused_until, now + float(seconds))
            return not already

    def resume(self) -> None:
        """End a pause now: what it was waiting for may have been fixed."""
        with self._lock:
            self._paused_until = 0.0

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._clock() < self._paused_until

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
        self._queue.put(None)

    @property
    def stopped(self) -> bool:
        with self._lock:
            return self._stopped

    # ── Worker ───────────────────────────────────────────────────────────────

    def _work(self):
        while True:
            entry = self._queue.get()
            if entry is None:
                return
            self.process(entry)

    def process(self, entry) -> None:
        """Run one queued entry. Public so that tests can drive the queue by
        hand; the worker thread is the only other caller."""
        key, item = entry
        try:
            if not self.paused and not self._stopped:
                self._run_one(item)
        except Exception as exc:
            # The type only: what failed here touched a message.
            logging.warning("[transcription] an automatic transcription failed: %s",
                            type(exc).__name__)
        finally:
            with self._lock:
                self._pending_keys.discard(key)

    def drain(self) -> None:
        """Process everything queued now (tests, with `start_worker=False`)."""
        while True:
            try:
                entry = self._queue.get_nowait()
            except queue.Empty:
                return
            if entry is not None:
                self.process(entry)
