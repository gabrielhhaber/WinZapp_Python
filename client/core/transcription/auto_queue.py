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
* **Age counts while WinZapp was listening.** The time the computer slept is
  not age (`listening_gaps`, recorded by the wake detector): reported by a
  user, every voice note received while the computer hibernated reached
  WinZapp at once on waking, hours old by the clock, and none was
  transcribed. Those that arrive through the sync rather than as live events
  are handed over too (`caught_up_arrivals`).
* **Only incoming audio and video, only once.** Our own messages and messages that
  already carry a transcription decision (a text, or a deletion) are skipped.
* **Not the very long ones.** Past `MAX_AUDIO_SECONDS` the user can still ask
  for it by hand; doing it unasked would be an hour of CPU for an audiobook.
* **Bounded.** At most `MAX_PENDING` waiting; a burst beyond that is dropped
  (the newest), not queued without end. Enough for a night's voice notes
  arriving together on waking (it was 10, and the rest of that burst went
  untranscribed).
* **Once per message.** A message already run is not run again when the
  live event and the sync both bring it (`AutoTranscriptionQueue` remembers
  the last `REMEMBERED_KEYS`).
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
import math
import queue
import threading
import time

from core.transcription import errors, message_audio, preferences
from core.transcription.stored import TRANSCRIPTION_KEY

#: The key inside the settings.json "transcription" section.
SETTING_AUTO_TRANSCRIBE = "auto_transcribe_received"

MAX_AGE_SECONDS = 300.0
#: What the user may choose for how old a message can be (minutes), stored as
#: `transcription.auto_max_age_minutes`. The default is MAX_AGE_SECONDS.
AGE_CHOICES_MINUTES = (5, 15, 30, 60)
SETTING_MAX_AGE_MINUTES = "auto_max_age_minutes"
MAX_AUDIO_SECONDS = 600
MAX_PENDING = 100
#: How many finished keys the queue remembers, so a message is run once.
REMEMBERED_KEYS = 2000
#: How long a listening gap is kept (a message older than that is too old anyway).
GAP_MEMORY_SECONDS = 7 * 24 * 3600.0
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


def max_age_seconds(settings) -> float:
    """How old a message may be, from the settings; an unknown value reads as
    the default, so a hand-edited file cannot open the door to a backlog."""
    section = settings.get("transcription") if isinstance(settings, dict) else None
    value = section.get(SETTING_MAX_AGE_MINUTES) if isinstance(section, dict) else None
    if isinstance(value, bool) or value not in AGE_CHOICES_MINUTES:
        return MAX_AGE_SECONDS
    return float(value) * 60.0


#: settings.json's "transcription" key for "however old they are".
SETTING_ANY_AGE = "auto_any_age"


def local_max_age_seconds(settings) -> float:
    """How old a message may be for the LOCAL automatic transcription: the
    shared choice (max_age_seconds), or no limit at all when the user ticked
    Settings > Local transcription's box for it (only a real True). The
    online AI keeps the shared choice."""
    section = settings.get("transcription") if isinstance(settings, dict) else None
    if isinstance(section, dict) and section.get(SETTING_ANY_AGE) is True:
        return math.inf
    return max_age_seconds(settings)


def eligible(msg, now=None, max_age=None, gaps=None) -> bool:
    """Whether `msg` is one the setting should transcribe. `max_age` (seconds)
    defaults to MAX_AGE_SECONDS; statuses keep that. `gaps`: see too_old()."""
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
    return not too_old(msg, now=now, max_age=max_age, gaps=gaps)


class ListeningGaps:
    """Stretches of wall-clock time WinZapp could not receive anything: the
    computer was asleep. Thread-safe: the health-check thread adds, the wx
    thread reads."""

    def __init__(self, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._gaps = []
        self._online = False
        self._offline_since = None

    def add(self, start, end) -> None:
        with self._lock:
            self._add_locked(start, end)

    def _add_locked(self, start, end) -> None:
        start, end = float(start), float(end)
        if end <= start:
            return
        horizon = self._clock() - GAP_MEMORY_SECONDS
        self._gaps = [g for g in self._gaps if g[1] >= horizon]
        self._gaps.append((start, end))

    def connection_changed(self, connected, now=None) -> None:
        """WhatsApp went offline or came back (MainWindow._set_wa_connected).
        Only a drop after having been connected is a gap: the first connection
        of a session is not, or a startup would transcribe the backlog of the
        time WinZapp was closed. The reconnection after a sleep is part of the
        gap too, the slow part (WhatsApp Web restarts)."""
        when = self._clock() if now is None else float(now)
        with self._lock:
            if not connected:
                if self._online and self._offline_since is None:
                    self._offline_since = when
                self._online = False
                return
            if self._offline_since is not None:
                self._add_locked(self._offline_since, when)
            self._offline_since = None
            self._online = True

    def snapshot(self) -> tuple:
        with self._lock:
            return tuple(self._gaps)


#: The process's gaps (main_window/account_link.py records them on waking).
listening_gaps = ListeningGaps()


def listening_age(arrived, now, gaps=()) -> float:
    """Seconds from `arrived` to `now` while WinZapp was listening: the time
    inside `gaps` is not counted."""
    age = float(now) - float(arrived)
    for start, end in gaps or ():
        overlap = min(float(end), float(now)) - max(float(start), float(arrived))
        if overlap > 0:
            age -= overlap
    return max(age, 0.0)


def too_old(msg, now=None, max_age=None, gaps=None) -> bool:
    """Whether `msg` waited longer than `max_age` (default MAX_AGE_SECONDS)
    while WinZapp was listening. `gaps` defaults to `listening_gaps`. A
    message with no timestamp has no age to judge: it just arrived live."""
    try:
        arrived = float((msg or {}).get("messageTimestamp") or 0)
    except (TypeError, ValueError, AttributeError):
        arrived = 0.0
    if not arrived:
        return False
    limit = MAX_AGE_SECONDS if max_age is None else max_age
    when = time.time() if now is None else now
    return listening_age(arrived, when,
                         listening_gaps.snapshot() if gaps is None else gaps) > limit


def caught_up_arrivals(messages, known_ids, newest_cached) -> list:
    """The messages of a sync page that WinZapp should have received live:
    new to the cache and newer than anything it held. After a sleep the
    socket is often rebuilt after the sync has already fetched them, and the
    live event then finds them known and hands nothing over. A chat with no
    history yet (`newest_cached` 0) is a history load, never "caught up"."""
    if not newest_cached:
        return []
    found = []
    for msg in messages or ():
        key = (msg or {}).get("key") or {}
        mid = key.get("id")
        if not mid or mid in known_ids:
            continue
        try:
            ts = float(msg.get("messageTimestamp") or 0)
        except (TypeError, ValueError):
            continue
        if ts > newest_cached:
            found.append(msg)
    return found


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
        #: keys already run, oldest first (a dict keeps the order)
        self._done_keys = {}
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
            if key in self._pending_keys or key in self._done_keys:
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
                with self._lock:
                    self._done_keys[key] = None
                    while len(self._done_keys) > REMEMBERED_KEYS:
                        self._done_keys.pop(next(iter(self._done_keys)))
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
