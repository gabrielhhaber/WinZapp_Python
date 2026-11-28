"""The threads of live captions (ACC-04): no wx, nothing the call waits for.

Two worker threads of their own, fed by ``RemoteTap`` (see
``core/call_captions.py`` for why that is all the call's audio path does):

* **segmenter** drains the tap, resamples to 16 kHz, cuts phrases at the pauses
  and puts them in a bounded queue that drops its oldest phrase. It never waits
  for Whisper, so the tap is always being emptied.
* **transcriber** takes one phrase at a time, runs it through
  ``PhraseTranscriber`` and turns the text into rows.

Everything that comes out crosses through three callbacks, all called on a
worker thread; the caller hands them to ``wx.CallAfter``. A callback that
raises costs the engine nothing. ``stop()`` is safe from any thread, idempotent
and never joins (a thread asked to stop is a daemon that ends by itself, and
the Whisper run in flight is cancelled).

The engine stops itself, and says why through ``on_stopped``, when:

* the call's audio got worse while it ran (``AudioHealthGuard``): ``"protected"``;
* the model is not set up (a ``SETUP_FAILURES`` code): ``"setup"``;
* ``MAX_FAILURES`` phrases in a row failed: ``"failed"``.

Only error codes and counts are logged, never a phrase.
"""

from __future__ import annotations

import logging
import threading
import time

from core import call_captions as cc
from core.call_captions_transcriber import is_setup_failure
from core.transcription import errors

REASON_PROTECTED = "protected"
REASON_SETUP = "setup"
REASON_FAILED = "failed"

MAX_FAILURES = 3
IDLE_WAIT = 0.04
HEALTH_EVERY = 1.0


class CaptionEngine:
    def __init__(self, tap, transcriber, *, speaker="", on_entries=None, on_stopped=None,
                 on_dropped=None, health=None, underruns=None, clock=time.monotonic):
        self._tap = tap
        self._transcriber = transcriber
        self._speaker = speaker
        self._on_entries = on_entries
        self._on_stopped = on_stopped
        self._on_dropped = on_dropped
        self._health = health
        self._underruns = underruns
        self._clock = clock
        self._queue = cc.PhraseQueue()
        self._log = cc.CaptionLog()
        self._segmenter = cc.PhraseSegmenter()
        self._to16k = cc.To16k()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._finished = False
        self._started_at = None
        self._threads = []

    @property
    def running(self) -> bool:
        return bool(self._threads) and not self._stop.is_set()

    @property
    def dropped_phrases(self) -> int:
        return self._queue.dropped

    def start(self) -> None:
        self._started_at = self._clock()
        for target, name in ((self._segment_loop, "WinZappCaptionSegmenter"),
                             (self._transcribe_loop, "WinZappCaptionTranscriber")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            self._threads.append(thread)
            thread.start()

    def stop(self) -> None:
        """The user (or the call's end) turned the captions off."""
        self._shutdown()

    # ── Threads ──────────────────────────────────────────────────────────────

    def _segment_loop(self) -> None:
        next_health = self._clock() + HEALTH_EVERY
        try:
            while not self._stop.is_set():
                chunks = self._tap.take()
                if not chunks:
                    self._stop.wait(IDLE_WAIT)
                for pcm, rate in chunks:
                    for phrase in self._segmenter.feed(self._to16k.process(pcm, rate)):
                        self._offer(phrase)
                now = self._clock()
                if now >= next_health:
                    next_health = now + HEALTH_EVERY
                    if self._unhealthy(now):
                        self._finish(REASON_PROTECTED, None)
                        return
        except Exception as exc:
            logging.error("[call_captions] segmenter failed: %s", type(exc).__name__)
            self._finish(REASON_FAILED, None)
            return
        phrase = self._segmenter.flush()
        if phrase is not None and not self._finished:
            self._queue.put(phrase)

    def _unhealthy(self, now) -> bool:
        if self._health is None or self._underruns is None:
            return False
        try:
            return bool(self._health.update(int(self._underruns()), now))
        except Exception:
            return False

    def _offer(self, phrase) -> None:
        if self._queue.put(phrase):
            self._call(self._on_dropped, self._queue.dropped)

    def _transcribe_loop(self) -> None:
        failures = 0
        try:
            while not self._stop.is_set():
                phrase = self._queue.get(0.25)
                if phrase is None:
                    continue
                try:
                    text = self._transcriber.transcribe(phrase.pcm)
                except errors.TranscriptionError as exc:
                    if exc.code == errors.CANCELLED or self._stop.is_set():
                        break
                    if is_setup_failure(exc):
                        logging.info("[call_captions] model not set up: %s", exc.code)
                        self._finish(REASON_SETUP, exc)
                        return
                    failures += 1
                    logging.info("[call_captions] a phrase failed: %s", exc.code)
                    if failures >= MAX_FAILURES:
                        self._finish(REASON_FAILED, exc)
                        return
                    continue
                except Exception as exc:
                    failures += 1
                    logging.error("[call_captions] a phrase failed unexpectedly: %s",
                                  type(exc).__name__)
                    if failures >= MAX_FAILURES:
                        self._finish(REASON_FAILED, None)
                        return
                    continue
                failures = 0
                at = self._clock() - (self._started_at or self._clock())
                entries = self._log.add(text, self._speaker, phrase.cut, at)
                if entries:
                    self._call(self._on_entries, entries)
        finally:
            # A phrase still waiting for its continuation is a row too.
            entry = self._log.flush()
            if entry is not None:
                self._call(self._on_entries, [entry])

    # ── Ending ───────────────────────────────────────────────────────────────

    def _finish(self, reason, error) -> None:
        """The engine's own stop: said once, before everything is released."""
        with self._lock:
            if self._finished:
                return
            self._finished = True
        self._call(self._on_stopped, reason, error)
        self._shutdown()

    def _shutdown(self) -> None:
        self._finished = True
        self._stop.set()
        self._tap.close()
        self._queue.close()
        try:
            self._transcriber.cancel()
        except Exception as exc:
            logging.debug("[call_captions] cancel raised %s", type(exc).__name__)

    @staticmethod
    def _call(callback, *args) -> None:
        if callback is None:
            return
        try:
            callback(*args)
        except Exception as exc:
            logging.error("[call_captions] a callback raised %s", type(exc).__name__)
