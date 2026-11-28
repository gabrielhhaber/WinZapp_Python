"""The threads of live captions (core/call_captions_engine.py), ACC-04.

A fake transcriber stands in for Whisper and synthetic PCM for the call: the
tests need no model, no device and no window. What they pin: phrases reach the
rows, a slow transcriber never makes the tap or the queue grow, the engine
stops itself for the call's sake or for a failure, a callback that raises costs
it nothing, and stopping is idempotent and cancels the run in flight.
"""

import threading
import time

import numpy as np
import pytest

from core import call_captions as cc
from core.call_captions_engine import (
    CaptionEngine, REASON_FAILED, REASON_PROTECTED, REASON_SETUP)
from core.transcription import errors


def _speech(seconds, rate=48000):
    t = np.arange(int(seconds * rate)) / rate
    return (np.sin(2 * np.pi * 200 * t) * 6000).astype("<i2").tobytes()


def _silence(seconds, rate=48000):
    return np.zeros(int(seconds * rate), dtype="<i2").tobytes()


def _offer_phrase(tap, text_seconds=1.0):
    tap.offer(_silence(0.3), 48000)
    tap.offer(_speech(text_seconds), 48000)
    tap.offer(_silence(1.0), 48000)


def _wait(predicate, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class _Transcriber:
    def __init__(self, texts=("Hello.",), error=None, delay=0.0):
        self.texts = list(texts)
        self.error = error
        self.delay = delay
        self.calls = 0
        self.cancelled = False
        self.release = threading.Event()
        self.release.set()

    def transcribe(self, pcm):
        self.calls += 1
        self.release.wait(5)
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.texts.pop(0) if self.texts else "More."

    def cancel(self):
        self.cancelled = True
        self.release.set()


class _Sink:
    def __init__(self):
        self.entries = []
        self.stopped = []
        self.dropped = []

    def engine(self, tap, transcriber, **kwargs):
        return CaptionEngine(
            tap, transcriber, on_entries=self.entries.extend,
            on_stopped=lambda reason, error: self.stopped.append((reason, error)),
            on_dropped=self.dropped.append, **kwargs)


def test_a_phrase_of_the_call_becomes_a_row_with_the_speaker():
    sink, tap = _Sink(), cc.RemoteTap()
    engine = sink.engine(tap, _Transcriber(["Good morning."]), speaker="Ana")
    engine.start()
    try:
        _offer_phrase(tap)
        assert _wait(lambda: sink.entries)
    finally:
        engine.stop()
    assert [(e.speaker, e.text) for e in sink.entries] == [("Ana", "Good morning.")]
    assert sink.stopped == []


def test_an_empty_transcription_makes_no_row():
    sink, tap = _Sink(), cc.RemoteTap()
    transcriber = _Transcriber([""])
    engine = sink.engine(tap, transcriber)
    engine.start()
    try:
        _offer_phrase(tap)
        assert _wait(lambda: transcriber.calls == 1)
        time.sleep(0.1)
    finally:
        engine.stop()
    assert sink.entries == []


def test_a_slow_transcriber_never_lets_the_tap_or_the_queue_grow():
    sink, tap = _Sink(), cc.RemoteTap(max_chunks=50)
    transcriber = _Transcriber()
    transcriber.release.clear()   # Whisper is stuck on its first phrase
    engine = sink.engine(tap, transcriber)
    engine.start()
    try:
        for _ in range(8):
            _offer_phrase(tap)
        assert _wait(lambda: engine.dropped_phrases > 0)
        # the segmenter keeps draining the tap while Whisper is stuck
        assert _wait(lambda: not tap._chunks)
        assert len(engine._queue) <= cc.QUEUE_LIMIT
        assert sink.dropped and sink.dropped[-1] == engine.dropped_phrases
    finally:
        engine.stop()


def test_stop_cancels_the_run_in_flight_and_is_idempotent():
    sink, tap = _Sink(), cc.RemoteTap()
    transcriber = _Transcriber()
    transcriber.release.clear()
    engine = sink.engine(tap, transcriber)
    engine.start()
    _offer_phrase(tap)
    assert _wait(lambda: transcriber.calls == 1)
    engine.stop()
    engine.stop()
    assert transcriber.cancelled
    assert not engine.running
    assert sink.stopped == []        # a user's stop is not "stopped by itself"
    tap.offer(_speech(1.0), 48000)   # a closed tap takes nothing
    assert tap.take() == []


def test_a_row_waiting_for_its_continuation_is_released_when_the_engine_stops():
    sink, tap = _Sink(), cc.RemoteTap()
    log = cc.CaptionLog()
    transcriber = _Transcriber(["half a sentence"])
    engine = sink.engine(tap, transcriber)
    engine._log = log
    engine._segmenter = cc.PhraseSegmenter(max_phrase_seconds=1.0)
    engine.start()
    try:
        tap.offer(_speech(1.3), 48000)   # longer than the cap: a cut phrase
        assert _wait(lambda: transcriber.calls == 1)
        assert _wait(lambda: log._pending is not None)
        assert sink.entries == []
    finally:
        engine.stop()
    assert _wait(lambda: [e.text for e in sink.entries] == ["half a sentence"])


def test_a_model_that_is_not_set_up_stops_the_engine_once_with_its_reason():
    sink, tap = _Sink(), cc.RemoteTap()
    error = errors.TranscriptionError(errors.MODEL_NOT_INSTALLED, "none")
    engine = sink.engine(tap, _Transcriber(error=error))
    engine.start()
    _offer_phrase(tap)
    assert _wait(lambda: sink.stopped)
    assert sink.stopped == [(REASON_SETUP, error)]
    assert not engine.running


def test_three_failures_in_a_row_stop_it_but_two_do_not():
    sink, tap = _Sink(), cc.RemoteTap()
    transcriber = _Transcriber(error=errors.TranscriptionError(errors.BACKEND_ERROR, "x"))
    engine = sink.engine(tap, transcriber)
    engine.start()
    try:
        _offer_phrase(tap)
        _offer_phrase(tap)
        assert _wait(lambda: transcriber.calls >= 2)
        time.sleep(0.1)
        assert transcriber.calls == 2
        assert sink.stopped == []
        _offer_phrase(tap)
        assert _wait(lambda: sink.stopped)
    finally:
        engine.stop()
    assert sink.stopped[0][0] == REASON_FAILED


def test_a_cancelled_run_is_not_a_failure():
    sink, tap = _Sink(), cc.RemoteTap()
    transcriber = _Transcriber(error=errors.TranscriptionError(errors.CANCELLED, "x"))
    engine = sink.engine(tap, transcriber)
    engine.start()
    _offer_phrase(tap)
    assert _wait(lambda: transcriber.calls == 1)
    time.sleep(0.1)
    engine.stop()
    assert sink.stopped == []


def test_the_engine_stops_itself_when_the_call_audio_got_worse():
    sink, tap = _Sink(), cc.RemoteTap()
    clock = {"now": 0.0}
    underruns = {"n": 0}

    class _Guard:
        def update(self, count, now):
            return count >= 50

    engine = sink.engine(tap, _Transcriber(), health=_Guard(),
                         underruns=lambda: underruns["n"],
                         clock=lambda: clock["now"])
    engine.start()
    try:
        underruns["n"] = 80
        clock["now"] = 5.0
        assert _wait(lambda: sink.stopped)
    finally:
        engine.stop()
    assert sink.stopped == [(REASON_PROTECTED, None)]


def test_a_health_probe_that_raises_does_not_stop_anything():
    sink, tap = _Sink(), cc.RemoteTap()
    clock = {"now": 0.0}

    def broken():
        raise RuntimeError("no reservoir")

    engine = sink.engine(tap, _Transcriber(), health=cc.AudioHealthGuard(),
                         underruns=broken, clock=lambda: clock["now"])
    engine.start()
    try:
        clock["now"] = 5.0
        time.sleep(0.15)
        assert engine.running
        assert sink.stopped == []
    finally:
        engine.stop()


def test_a_callback_that_raises_costs_the_engine_nothing():
    tap = cc.RemoteTap()
    seen = []

    def boom(entries):
        seen.extend(entries)
        raise RuntimeError("window gone")

    engine = CaptionEngine(tap, _Transcriber(["One.", "Two."]), on_entries=boom)
    engine.start()
    try:
        _offer_phrase(tap)
        _offer_phrase(tap)
        assert _wait(lambda: len(seen) == 2)
    finally:
        engine.stop()


def test_nothing_the_engine_logs_holds_the_text(caplog):
    sink, tap = _Sink(), cc.RemoteTap()
    secret = "my secret sentence"
    transcriber = _Transcriber([secret])
    with caplog.at_level("DEBUG"):
        engine = sink.engine(tap, transcriber)
        engine.start()
        try:
            _offer_phrase(tap)
            assert _wait(lambda: sink.entries)
        finally:
            engine.stop()
    assert secret not in caplog.text


@pytest.mark.parametrize("rate", [16000, 48000])
def test_the_calls_two_rates_both_reach_whisper(rate):
    sink, tap = _Sink(), cc.RemoteTap()
    engine = sink.engine(tap, _Transcriber(["Hi."]))
    engine.start()
    try:
        tap.offer(_silence(0.3, rate), rate)
        tap.offer(_speech(1.0, rate), rate)
        tap.offer(_silence(1.0, rate), rate)
        assert _wait(lambda: sink.entries)
    finally:
        engine.stop()
