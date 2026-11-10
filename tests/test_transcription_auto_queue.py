"""Automatic transcription of received voice messages: who qualifies, in what order."""

import pytest

from core.transcription import auto_queue, errors, preferences
from core.transcription.stored import TRANSCRIPTION_KEY

NOW = 1_000_000.0


def _msg(**overrides):
    msg = {
        "key": {"id": "M1", "fromMe": False, "remoteJid": "a@s.whatsapp.net"},
        "messageType": "audioMessage",
        "messageTimestamp": NOW - 5,
        "message": {"audioMessage": {"seconds": 12, "ptt": True}},
    }
    msg.update(overrides)
    return msg


class TestTheSetting:
    def test_it_is_off_by_default_and_spelled_the_same_in_both_modules(self):
        assert preferences.DEFAULTS[preferences.SETTING_AUTO_TRANSCRIBE_RECEIVED] is False
        assert auto_queue.SETTING_AUTO_TRANSCRIBE == preferences.SETTING_AUTO_TRANSCRIBE_RECEIVED

    def test_only_a_real_true_turns_it_on(self):
        assert auto_queue.enabled({"transcription": {"auto_transcribe_received": True}})
        for stored in (False, None, "yes", 1, []):
            assert not auto_queue.enabled({"transcription": {"auto_transcribe_received": stored}})
        assert not auto_queue.enabled({})
        assert not auto_queue.enabled(None)
        assert not auto_queue.enabled({"transcription": "junk"})


class TestWhoQualifies:
    def test_a_fresh_incoming_voice_message_does(self):
        assert auto_queue.eligible(_msg(), now=NOW)

    def test_our_own_message_does_not(self):
        msg = _msg()
        msg["key"]["fromMe"] = True
        assert not auto_queue.eligible(msg, now=NOW)

    def test_a_fresh_incoming_video_does(self):
        video = _msg(messageType="videoMessage",
                     message={"videoMessage": {"seconds": 30, "mimetype": "video/mp4"}})
        assert auto_queue.eligible(video, now=NOW)

    def test_a_gif_does_not(self):
        gif = _msg(messageType="videoMessage",
                   message={"videoMessage": {"seconds": 3, "gifPlayback": True}})
        assert not auto_queue.eligible(gif, now=NOW)

    def test_a_very_long_video_does_not(self):
        long = _msg(messageType="videoMessage",
                    message={"videoMessage": {"seconds": auto_queue.MAX_AUDIO_SECONDS + 1}})
        assert not auto_queue.eligible(long, now=NOW)

    @pytest.mark.parametrize("message_type", ["conversation", "imageMessage",
                                              "documentMessage", "stickerMessage"])
    def test_only_audio_and_video_do(self, message_type):
        assert not auto_queue.eligible(_msg(messageType=message_type), now=NOW)

    def test_a_message_without_an_id_does_not(self):
        msg = _msg()
        msg["key"]["id"] = ""
        assert not auto_queue.eligible(msg, now=NOW)

    def test_one_that_already_has_a_decision_does_not(self):
        assert not auto_queue.eligible(_msg(**{TRANSCRIPTION_KEY: {"deleted": True, "at": 1.0}}), now=NOW)
        assert not auto_queue.eligible(_msg(**{TRANSCRIPTION_KEY: {"text": "oi", "at": 1.0}}), now=NOW)

    def test_a_backlog_message_does_not(self):
        old = _msg(messageTimestamp=NOW - auto_queue.MAX_AGE_SECONDS - 1)
        assert not auto_queue.eligible(old, now=NOW)
        edge = _msg(messageTimestamp=NOW - auto_queue.MAX_AGE_SECONDS + 1)
        assert auto_queue.eligible(edge, now=NOW)

    def test_a_message_with_no_timestamp_just_arrived(self):
        msg = _msg()
        del msg["messageTimestamp"]
        assert auto_queue.eligible(msg, now=NOW)

    def test_a_very_long_recording_does_not(self):
        long = _msg(message={"audioMessage": {"seconds": auto_queue.MAX_AUDIO_SECONDS + 1}})
        assert not auto_queue.eligible(long, now=NOW)
        ok = _msg(message={"audioMessage": {"seconds": auto_queue.MAX_AUDIO_SECONDS}})
        assert auto_queue.eligible(ok, now=NOW)

    def test_an_unstated_or_odd_length_is_not_a_reason_to_skip(self):
        for seconds in (None, "", "abc", -3):
            msg = _msg(message={"audioMessage": {"seconds": seconds}})
            assert auto_queue.eligible(msg, now=NOW)

    @pytest.mark.parametrize("junk", [None, "x", 3, []])
    def test_junk_is_not_eligible(self, junk):
        assert not auto_queue.eligible(junk, now=NOW)


class TestWhichFailuresPauseTheQueue:
    @pytest.mark.parametrize("code", sorted(auto_queue.SETUP_FAILURES))
    def test_a_setup_fault_pauses(self, code):
        assert auto_queue.pauses_the_queue(errors.TranscriptionError(code, "x"))

    @pytest.mark.parametrize("code", [errors.INSUFFICIENT_RAM, errors.MEDIA_NOT_DOWNLOADED,
                                      errors.CANCELLED, errors.FFMPEG_FAILED,
                                      errors.BACKEND_ERROR, errors.CUDA_UNAVAILABLE])
    def test_a_fault_of_one_message_does_not(self, code):
        assert not auto_queue.pauses_the_queue(errors.TranscriptionError(code, "x"))

    def test_not_an_error_does_not(self):
        assert not auto_queue.pauses_the_queue(None)

    def test_no_model_fitting_the_memory_free_right_now_does_not(self):
        """"Automatic" found no model for the memory free at that moment: the
        same code as "nothing installed", and about the moment only."""
        error = errors.TranscriptionError(errors.MODEL_NOT_INSTALLED, "x")
        full = _Resolution(None, preferences.MODEL_NONE_NOTHING_FITS)
        assert auto_queue.nothing_fits_right_now(errors.MODEL_NOT_INSTALLED, full)
        assert not auto_queue.pauses_the_queue(error, full)

    def test_unmeasured_memory_with_nothing_installed_still_does(self):
        error = errors.TranscriptionError(errors.MODEL_NOT_INSTALLED, "x")
        unmeasured = _Resolution(None, preferences.MODEL_NONE_UNMEASURED)
        assert auto_queue.pauses_the_queue(error, unmeasured)

    def test_a_model_resolved_but_not_downloaded_still_does(self):
        error = errors.TranscriptionError(errors.MODEL_NOT_INSTALLED, "x")
        assert auto_queue.pauses_the_queue(error, _Resolution("small", None))

    def test_the_memory_never_excuses_another_code(self):
        error = errors.TranscriptionError(errors.WHISPER_CPP_NOT_INSTALLED, "x")
        full = _Resolution(None, preferences.MODEL_NONE_NOTHING_FITS)
        assert auto_queue.pauses_the_queue(error, full)


class _Resolution:
    def __init__(self, model_id, model_none_reason):
        self.model_id = model_id
        self.model_none_reason = model_none_reason


class TestPauseNotices:
    def test_each_fault_is_news_once(self):
        notices = auto_queue.PauseNotices()
        assert notices.first(errors.MODEL_NOT_INSTALLED) is True
        assert notices.first(errors.MODEL_NOT_INSTALLED) is False
        assert notices.first(errors.BACKEND_MISSING) is True

    def test_forgetting_makes_it_news_again(self):
        notices = auto_queue.PauseNotices()
        notices.first(errors.MODEL_NOT_INSTALLED)
        notices.forget()
        assert notices.first(errors.MODEL_NOT_INSTALLED) is True


class _Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def _queue(run_one, **kwargs):
    return auto_queue.AutoTranscriptionQueue(run_one, start_worker=False, **kwargs)


class TestTheQueue:
    def test_items_run_in_order_one_at_a_time(self):
        ran = []
        queue = _queue(ran.append)
        for index in range(3):
            assert queue.submit(("c", index), index) == auto_queue.ACCEPTED
        assert queue.pending == 3
        queue.drain()
        assert ran == [0, 1, 2]
        assert queue.pending == 0

    def test_the_same_message_is_not_queued_twice_nor_run_twice(self):
        # the live event and the sync can both bring it (caught_up_arrivals)
        queue = _queue(lambda item: None)
        assert queue.submit(("c", "M1"), 1) == auto_queue.ACCEPTED
        assert queue.submit(("c", "M1"), 1) == auto_queue.DUPLICATE
        queue.drain()
        assert queue.submit(("c", "M1"), 1) == auto_queue.DUPLICATE

    def test_a_run_that_raised_may_be_tried_again(self):
        def run_one(item):
            raise RuntimeError("boom")

        queue = _queue(run_one)
        queue.submit(("c", "M1"), 1)
        queue.drain()
        assert queue.submit(("c", "M1"), 1) == auto_queue.ACCEPTED

    def test_a_night_of_voice_notes_fits(self):
        assert auto_queue.MAX_PENDING >= 100

    def test_a_burst_beyond_the_limit_is_dropped(self):
        queue = _queue(lambda item: None, max_pending=2)
        assert queue.submit(1, 1) == auto_queue.ACCEPTED
        assert queue.submit(2, 2) == auto_queue.ACCEPTED
        assert queue.submit(3, 3) == auto_queue.FULL
        queue.drain()
        assert queue.submit(3, 3) == auto_queue.ACCEPTED

    def test_a_run_that_raises_does_not_stop_the_rest(self):
        ran = []

        def run_one(item):
            if item == 1:
                raise RuntimeError("boom")
            ran.append(item)

        queue = _queue(run_one)
        for item in (1, 2):
            queue.submit(item, item)
        queue.drain()
        assert ran == [2]
        assert queue.pending == 0

    def test_a_pause_refuses_new_work_and_skips_what_waits(self):
        clock = _Clock()
        ran = []
        queue = _queue(ran.append, clock=clock)
        queue.submit(1, 1)
        assert queue.pause(60) is True
        assert queue.paused
        assert queue.submit(2, 2) == auto_queue.PAUSED
        queue.drain()
        assert ran == []
        assert queue.pending == 0

    def test_resume_ends_a_pause_at_once(self):
        clock = _Clock()
        queue = _queue(lambda item: None, clock=clock)
        queue.pause(900)
        queue.resume()
        assert not queue.paused
        assert queue.submit("k", "item") == auto_queue.ACCEPTED

    def test_a_pause_is_announced_once_and_ends(self):
        clock = _Clock()
        queue = _queue(lambda item: None, clock=clock)
        assert queue.pause(60) is True
        assert queue.pause(60) is False
        clock.now += 61
        assert not queue.paused
        assert queue.submit(1, 1) == auto_queue.ACCEPTED
        assert queue.pause(60) is True

    def test_a_stopped_queue_takes_nothing(self):
        queue = _queue(lambda item: None)
        queue.stop()
        assert queue.submit(1, 1) == auto_queue.STOPPED

    def test_the_real_worker_thread_runs_items(self):
        import threading
        done = threading.Event()
        queue = auto_queue.AutoTranscriptionQueue(lambda item: done.set())
        try:
            queue.submit("k", "item")
            assert done.wait(5)
        finally:
            queue.stop()


class TestAgeWhileListening:
    """The time the computer slept, or WhatsApp was offline, is not age."""

    def test_the_sleep_is_taken_out_of_the_age(self):
        # sent 8 h ago, the computer slept from 1 min after to 1 min ago
        gaps = [(NOW - 8 * 3600 + 60, NOW - 60)]
        assert auto_queue.listening_age(NOW - 8 * 3600, NOW, gaps) == 120
        msg = _msg(messageTimestamp=NOW - 8 * 3600)
        assert auto_queue.eligible(msg, now=NOW, gaps=gaps)
        assert not auto_queue.eligible(msg, now=NOW, gaps=[])

    def test_a_message_sent_during_the_sleep_is_young(self):
        gaps = [(NOW - 3600, NOW - 30)]
        assert auto_queue.listening_age(NOW - 1800, NOW, gaps) == 30

    def test_an_old_message_from_before_the_sleep_stays_old(self):
        gaps = [(NOW - 3600, NOW - 30)]
        msg = _msg(messageTimestamp=NOW - 3600 - 600)
        assert not auto_queue.eligible(msg, now=NOW, gaps=gaps)

    def test_a_drop_after_being_connected_is_a_gap_the_first_connection_is_not(self):
        clock = [NOW]
        gaps = auto_queue.ListeningGaps(clock=lambda: clock[0])
        gaps.connection_changed(True, now=NOW - 5000)      # startup: no gap
        assert gaps.snapshot() == ()
        gaps.connection_changed(False, now=NOW - 4000)
        gaps.connection_changed(False, now=NOW - 3000)     # still the same drop
        gaps.connection_changed(True, now=NOW - 100)
        assert gaps.snapshot() == ((NOW - 4000, NOW - 100),)

    def test_old_gaps_are_forgotten(self):
        clock = [NOW]
        gaps = auto_queue.ListeningGaps(clock=lambda: clock[0])
        gaps.add(NOW - 30 * 86400, NOW - 29 * 86400)
        gaps.add(NOW - 100, NOW - 50)
        assert gaps.snapshot() == ((NOW - 100, NOW - 50),)
        gaps.add(5, 5)                                     # empty: ignored
        assert len(gaps.snapshot()) == 1

    def test_overlapping_gaps_count_once(self):
        """A sleep inside an offline stretch is recorded twice (wake detector
        and connection); its time must come off a message's age only once."""
        gaps = auto_queue.ListeningGaps(clock=lambda: NOW)
        gaps.add(NOW - 1000, NOW - 400)
        gaps.add(NOW - 800, NOW - 200)
        gaps.add(NOW - 100, NOW - 50)
        assert gaps.snapshot() == ((NOW - 1000, NOW - 200), (NOW - 100, NOW - 50))
        assert auto_queue.listening_age(NOW - 2000, NOW, gaps.snapshot()) == 2000 - 850


class TestCaughtUpBySync:
    def test_only_new_and_newer_than_the_cache(self):
        new = _msg(key={"id": "N", "fromMe": False}, messageTimestamp=NOW)
        older = _msg(key={"id": "O", "fromMe": False}, messageTimestamp=NOW - 999)
        known = _msg(key={"id": "K", "fromMe": False}, messageTimestamp=NOW)
        found = auto_queue.caught_up_arrivals([new, older, known], {"K"}, NOW - 10)
        assert found == [new]

    def test_a_chat_without_history_is_a_history_load(self):
        assert auto_queue.caught_up_arrivals([_msg()], set(), 0) == []



class TestHoweverOld:
    """Settings > Local transcription's box: the local automatic transcription
    takes messages however old they are; the online AI keeps the choice."""

    def test_only_a_real_true_lifts_the_limit(self):
        on = {"transcription": {"auto_any_age": True, "auto_max_age_minutes": 15}}
        assert auto_queue.local_max_age_seconds(on) == float("inf")
        assert auto_queue.max_age_seconds(on) == 15 * 60
        for stored in (False, "yes", 1, None):
            off = {"transcription": {"auto_any_age": stored, "auto_max_age_minutes": 15}}
            assert auto_queue.local_max_age_seconds(off) == 15 * 60
        assert auto_queue.local_max_age_seconds({}) == auto_queue.MAX_AGE_SECONDS

    def test_a_message_of_last_week_qualifies_with_it(self):
        old = _msg(messageTimestamp=NOW - 7 * 86400)
        assert auto_queue.eligible(old, now=NOW, max_age=float("inf"), gaps=())
        assert not auto_queue.eligible(old, now=NOW, gaps=())

    def test_it_is_spelled_the_same_in_both_modules_and_off_by_default(self):
        assert auto_queue.SETTING_ANY_AGE == preferences.SETTING_AUTO_ANY_AGE
        assert preferences.DEFAULTS[preferences.SETTING_AUTO_ANY_AGE] is False

    def test_the_box_shows_only_with_the_automatic_transcription(self):
        from ui.dialogs.transcription_options import any_age_box_shown
        assert any_age_box_shown(True) is True
        assert any_age_box_shown(False) is False
