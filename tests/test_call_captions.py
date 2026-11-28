"""The pure half of live captions in calls (core/call_captions.py), ACC-04.

No wx, no hardware, no Whisper: synthetic PCM, fake clocks. The rules pinned:
the tap never blocks or raises and drops the oldest, phrases are cut at pauses
and by the length cap, a queue never grows, a phrase cut by the cap is joined
before it becomes a row, speech is rationed, and the guard asks to stop only
when the call got worse than its own baseline.
"""

import numpy as np
import pytest

from core import call_captions as cc


def _tone(seconds, rate=16000, amplitude=6000):
    t = np.arange(int(seconds * rate)) / rate
    return (np.sin(2 * np.pi * 220 * t) * amplitude).astype("<i2")


def _silence(seconds, rate=16000):
    return np.zeros(int(seconds * rate), dtype="<i2")


# ── The tap ──────────────────────────────────────────────────────────────────


class TestRemoteTap:
    def test_it_hands_over_what_was_offered_in_order(self):
        tap = cc.RemoteTap()
        tap.offer(b"\x01\x00", 48000)
        tap.offer(b"\x02\x00", 16000)
        assert tap.take() == [(b"\x01\x00", 48000), (b"\x02\x00", 16000)]
        assert tap.take() == []

    def test_a_slow_consumer_loses_the_oldest_and_the_count_says_so(self):
        tap = cc.RemoteTap(max_chunks=3)
        for value in range(5):
            tap.offer(bytes([value, 0]), 16000)
        assert [chunk[0][0] for chunk in tap.take()] == [2, 3, 4]
        assert tap.dropped_chunks == 2

    def test_offer_never_raises(self):
        tap = cc.RemoteTap()
        tap.offer(None, 16000)        # not bytes
        tap.offer(b"\x00\x00", "x")   # not a rate
        assert tap.take() == []

    def test_a_closed_tap_takes_nothing_and_forgets(self):
        tap = cc.RemoteTap()
        tap.offer(b"\x00\x00", 16000)
        tap.close()
        tap.offer(b"\x00\x00", 16000)
        assert tap.take() == []


# ── 16 kHz ───────────────────────────────────────────────────────────────────


class TestTo16k:
    def test_48k_becomes_a_third_as_many_samples_across_chunk_boundaries(self):
        converter = cc.To16k()
        pcm = _tone(1.0, 48000).tobytes()
        parts = [converter.process(pcm[i:i + 2050], 48000) for i in range(0, len(pcm), 2050)]
        total = sum(len(p) for p in parts)
        assert abs(total - 16000) <= 3

    def test_16k_passes_through(self):
        out = cc.To16k().process(_tone(0.1).tobytes(), 16000)
        assert len(out) == 1600

    def test_an_odd_rate_is_interpolated(self):
        out = cc.To16k().process(_tone(1.0, 44100).tobytes(), 44100)
        assert abs(len(out) - 16000) <= 1

    def test_an_absurd_rate_gives_nothing(self):
        assert len(cc.To16k().process(b"\x00\x00" * 100, 10)) == 0

    def test_a_trailing_odd_byte_is_ignored(self):
        assert len(cc.To16k().process(b"\x00\x00\x01", 16000)) == 1


# ── Phrases ──────────────────────────────────────────────────────────────────


def _feed_all(segmenter, samples, chunk=480):
    phrases = []
    for start in range(0, len(samples), chunk):
        phrases += segmenter.feed(samples[start:start + chunk])
    return phrases


class TestPhraseSegmenter:
    def test_speech_between_pauses_is_one_phrase(self):
        seg = cc.PhraseSegmenter()
        audio = np.concatenate([_silence(0.5), _tone(1.5), _silence(1.0)])
        phrases = _feed_all(seg, audio)
        assert len(phrases) == 1
        assert not phrases[0].cut
        assert 1.5 <= phrases[0].seconds <= 2.3

    def test_two_pauses_make_two_phrases(self):
        seg = cc.PhraseSegmenter()
        audio = np.concatenate([_silence(0.3), _tone(1.0), _silence(1.0),
                                _tone(1.0), _silence(1.0)])
        assert len(_feed_all(seg, audio)) == 2

    def test_a_short_pause_does_not_split_a_phrase(self):
        seg = cc.PhraseSegmenter()
        audio = np.concatenate([_tone(1.0), _silence(0.3), _tone(1.0), _silence(1.0)])
        assert len(_feed_all(seg, audio)) == 1

    def test_silence_and_quiet_noise_make_nothing(self):
        seg = cc.PhraseSegmenter()
        noise = (np.random.default_rng(1).normal(0, 40, 16000 * 5)).astype("<i2")
        assert _feed_all(seg, noise) == []
        assert seg.flush() is None

    def test_a_cough_shorter_than_the_minimum_is_dropped(self):
        seg = cc.PhraseSegmenter()
        audio = np.concatenate([_silence(0.3), _tone(0.12), _silence(1.0)])
        assert _feed_all(seg, audio) == []

    def test_speech_that_never_pauses_is_cut_at_the_cap_and_marked(self):
        seg = cc.PhraseSegmenter(max_phrase_seconds=2.0)
        phrases = _feed_all(seg, _tone(5.0))
        assert len(phrases) == 2
        assert all(p.cut for p in phrases)
        assert all(p.seconds <= 2.1 for p in phrases)
        rest = seg.flush()
        assert rest is not None and not rest.cut

    def test_flush_returns_a_phrase_still_in_progress(self):
        seg = cc.PhraseSegmenter()
        assert _feed_all(seg, np.concatenate([_silence(0.3), _tone(1.0)])) == []
        assert seg.in_speech
        assert seg.flush() is not None
        assert not seg.in_speech

    def test_the_phrase_is_16k_mono_s16le(self):
        seg = cc.PhraseSegmenter()
        phrase = _feed_all(seg, np.concatenate([_tone(1.0), _silence(1.0)]))[0]
        assert len(phrase.pcm) % 2 == 0
        assert phrase.seconds == pytest.approx(len(phrase.pcm) / 2 / 16000)

    def test_samples_that_are_not_a_whole_frame_are_kept_for_the_next_feed(self):
        seg = cc.PhraseSegmenter()
        audio = np.concatenate([_tone(1.0), _silence(1.0)])
        odd = _feed_all(seg, audio, chunk=457)
        assert len(odd) == 1


# ── The queue ────────────────────────────────────────────────────────────────


class TestPhraseQueue:
    def test_it_never_grows_past_its_limit_and_drops_the_oldest(self):
        queue = cc.PhraseQueue(limit=2)
        assert queue.put("a") == 0
        assert queue.put("b") == 0
        assert queue.put("c") == 1
        assert len(queue) == 2
        assert queue.get(0.01) == "b"
        assert queue.get(0.01) == "c"
        assert queue.get(0.01) is None
        assert queue.dropped == 1

    def test_a_closed_queue_takes_nothing_and_wakes_a_reader(self):
        queue = cc.PhraseQueue()
        queue.put("a")
        queue.close()
        assert queue.put("b") == 0
        assert queue.get(0.01) is None


# ── Rows ─────────────────────────────────────────────────────────────────────


class TestCaptionLog:
    def test_a_whole_phrase_is_one_row(self):
        log = cc.CaptionLog()
        assert log.add("Hello there.", "Ana", at=1.0) == [cc.CaptionEntry("Hello there.", "Ana", 1.0)]

    def test_a_phrase_cut_by_the_cap_waits_for_its_continuation(self):
        log = cc.CaptionLog()
        assert log.add("I was saying that", "Ana", cut=True) == []
        rows = log.add("we should meet.", "Ana")
        assert [row.text for row in rows] == ["I was saying that we should meet."]

    def test_at_most_max_join_pieces_become_one_row(self):
        log = cc.CaptionLog(max_join=2)
        assert log.add("one", cut=True) == []
        assert [r.text for r in log.add("two", cut=True)] == ["one two"]

    def test_another_speaker_completes_the_waiting_row_first(self):
        log = cc.CaptionLog()
        log.add("first half", "Ana", cut=True)
        rows = log.add("Hi.", "Bia")
        assert [(r.speaker, r.text) for r in rows] == [("Ana", "first half"), ("Bia", "Hi.")]

    def test_flush_releases_a_waiting_row(self):
        log = cc.CaptionLog()
        log.add("unfinished", cut=True)
        assert log.flush().text == "unfinished"
        assert log.flush() is None

    @pytest.mark.parametrize("junk", ["", "   ", "...", "♪", " - "])
    def test_text_without_a_letter_or_digit_makes_no_row(self, junk):
        assert cc.CaptionLog().add(junk) == []

    def test_an_immediate_repeat_is_dropped_but_a_later_one_is_not(self):
        log = cc.CaptionLog()
        assert len(log.add("Thank you.")) == 1
        assert log.add("thank you.") == []
        assert len(log.add("Okay.")) == 1
        assert len(log.add("Thank you.")) == 1

    def test_whitespace_is_normalised(self):
        assert cc.CaptionLog().add("a \n b")[0].text == "a b"

    def test_rows_and_transcript_show_the_speaker_only_when_known(self):
        a = cc.CaptionEntry("Hi.", "Ana")
        b = cc.CaptionEntry("Hello.")
        assert cc.format_row(a) == "Ana: Hi."
        assert cc.format_row(b) == "Hello."
        assert cc.transcript_text([a, b]) == "Ana: Hi.\nHello."


# ── Speech ───────────────────────────────────────────────────────────────────


class TestSpeechCoalescer:
    def test_the_first_phrase_is_said_at_once(self):
        assert cc.SpeechCoalescer().add("hello", 0.0) == "hello"

    def test_a_burst_is_held_and_said_as_one_utterance(self):
        speech = cc.SpeechCoalescer(min_gap=1.5)
        assert speech.add("one", 0.0) == "one"
        assert speech.add("two", 0.2) is None
        assert speech.add("three", 0.4) is None
        assert speech.has_pending
        assert speech.due(1.0) is None
        assert speech.due(1.6) == "two three"
        assert not speech.has_pending
        assert speech.due(9.0) is None

    def test_a_long_utterance_keeps_its_latest_words_cut_at_a_word(self):
        speech = cc.SpeechCoalescer(limit=20)
        out = speech.add("alpha beta gamma delta epsilon zeta", 0.0)
        assert len(out) <= 20
        assert out.endswith("zeta")
        assert not out.startswith(("lpha", "eta", "amma"))

    def test_clear_forgets_what_was_waiting(self):
        speech = cc.SpeechCoalescer()
        speech.add("a", 0.0)
        speech.add("b", 0.1)
        speech.clear()
        assert speech.due(9.0) is None


# ── The call's health ────────────────────────────────────────────────────────


class TestAudioHealthGuard:
    def test_a_call_that_underruns_on_its_own_is_not_blamed_on_the_captions(self):
        guard = cc.AudioHealthGuard(calibration=6, window=10, allowance=6)
        underruns = 0
        for second in range(0, 60):
            underruns += 3   # 3 per second before and after
            assert not guard.update(underruns, float(second))

    def test_a_clean_call_that_starts_underrunning_is_stopped(self):
        guard = cc.AudioHealthGuard(calibration=6, window=10, allowance=6)
        for second in range(0, 8):
            assert not guard.update(0, float(second))
        stopped = False
        underruns = 0
        for second in range(8, 30):
            underruns += 2
            stopped = guard.update(underruns, float(second)) or stopped
        assert stopped

    def test_a_couple_of_underruns_do_not_stop_a_clean_call(self):
        guard = cc.AudioHealthGuard(calibration=6, window=10, allowance=6)
        for second in range(0, 8):
            guard.update(0, float(second))
        assert not guard.update(2, 9.0)
        assert not guard.update(4, 10.0)

    def test_nothing_is_decided_while_calibrating(self):
        guard = cc.AudioHealthGuard(calibration=6, window=10, allowance=0)
        assert not guard.update(0, 0.0)
        assert not guard.update(500, 3.0)


# ── Whether it may start ─────────────────────────────────────────────────────


class TestRefusalKey:
    def test_no_call_is_refused_first(self):
        assert cc.refusal_key(False, False, True, True) == cc.REASON_NO_CALL
        assert cc.refusal_key(True, False, False, False) == cc.REASON_NO_CALL

    def test_a_busy_transcription_or_a_dictation_is_refused(self):
        assert cc.refusal_key(True, True, True, False) == cc.REASON_BUSY
        assert cc.refusal_key(True, True, False, True) == cc.REASON_BUSY

    def test_a_free_active_call_may_start(self):
        assert cc.refusal_key(True, True, False, False) is None
