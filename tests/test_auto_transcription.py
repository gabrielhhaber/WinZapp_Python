"""MainWindow's glue for automatic transcription of received voice messages."""

import time

import pytest

from core.transcription import auto_queue, errors, message_run, preferences, statuses
from core.transcription import stored as stored_transcription
from core.transcription.backend import TranscriptionResult
from main_window import auto_transcription
from main_window.auto_transcription import AutoTranscriptionMixin
from main_window import status_transcription
from main_window.status_transcription import StatusTranscriptionMixin

KEY = stored_transcription.TRANSCRIPTION_KEY
JID = "5511999999999@s.whatsapp.net"


def _msg(msg_id="M1", **extra):
    msg = {
        "key": {"id": msg_id, "fromMe": False, "remoteJid": JID},
        "messageType": "audioMessage",
        "messageTimestamp": time.time(),
        "message": {"audioMessage": {"seconds": 8}},
    }
    msg.update(extra)
    return msg


def _result(text="olá"):
    return TranscriptionResult(
        text=text, language="pt", language_probability=0.9, duration_seconds=8.0,
        segments=(), vad_used=True, backend="faster_whisper", model_id="base",
        device="cpu", compute_type="int8",
    )


class _Queue:
    def __init__(self):
        self.submitted = []
        self.paused = []
        self.resumed = 0
        self.stopped = False

    def resume(self):
        self.resumed += 1

    def stop(self):
        self.stopped = True

    def submit(self, key, item):
        self.submitted.append((key, item))
        return auto_queue.ACCEPTED

    def pause(self):
        self.paused.append(True)
        return len(self.paused) == 1


class _I18n:
    language = "pt-BR"
    #: Keys whose text matters to a test; any other answers with its key.
    texts = {}

    def t(self, key):
        return self.texts.get(key, key)


class _Window(AutoTranscriptionMixin):
    def __init__(self, enabled=True, locked=False):
        self.settings = {"transcription": {"auto_transcribe_received": enabled}}
        self.key = b"k"
        self.i18n = _I18n()
        self._locked = locked
        self.queue = _Queue()
        self._auto_transcription_queue = self.queue
        self.records = []
        self.stored = []
        self.spoken = []
        self.repainted = []
        self.conversations_panel = None
        self._wa_connected = True

    def is_chat_locked(self, jid):
        return self._locked

    def is_chat_hidden_by_vault(self, jid):
        return self._locked

    def _transcription_copies(self, jid, msg_id):
        return self.records

    def store_message_transcription(self, jid, msg_id, value, existing=False):
        self.stored.append((jid, msg_id, value))
        return stored_transcription.SAVE_STORED

    def output(self, text, interrupt=False):
        self.spoken.append(text)

    @staticmethod
    def _normalize_jid(jid):
        return jid

    def _find_api_ffmpeg(self):
        return ""

    def handle_audio_message(self, msg):
        return False


@pytest.fixture(autouse=True)
def _inline_call_after(monkeypatch):
    monkeypatch.setattr(auto_transcription.wx, "CallAfter", lambda fn, *a, **k: fn(*a, **k))


class TestQueueing:
    def test_a_fresh_incoming_voice_message_is_queued(self):
        window = _Window()
        msg = _msg()
        window._maybe_auto_transcribe(JID, msg)
        assert window.queue.submitted == [((JID, "M1"), (JID, "M1", msg))]

    def test_nothing_happens_while_the_setting_is_off(self):
        window = _Window(enabled=False)
        window._maybe_auto_transcribe(JID, _msg())
        assert window.queue.submitted == []

    def test_a_model_chosen_with_its_cache_off_is_not_run_for_nothing(self):
        window = _Window()
        window.settings["transcription"].update(
            model="base", cache_by_model={"base": False})
        window._maybe_auto_transcribe(JID, _msg())
        assert window.queue.submitted == []

    def test_the_automatic_choice_is_still_queued_when_the_cache_is_off(self):
        # The model is only known after the run; storing refuses it then.
        window = _Window()
        window.settings["transcription"].update(cache_enabled=False)
        window._maybe_auto_transcribe(JID, _msg())
        assert len(window.queue.submitted) == 1

    def test_a_locked_chat_is_skipped(self):
        window = _Window(locked=True)
        window._maybe_auto_transcribe(JID, _msg())
        assert window.queue.submitted == []

    def test_an_ineligible_message_is_skipped(self):
        window = _Window()
        window._maybe_auto_transcribe(JID, _msg(messageType="imageMessage"))
        own = _msg()
        own["key"]["fromMe"] = True
        window._maybe_auto_transcribe(JID, own)
        assert window.queue.submitted == []

    def test_a_lock_check_that_raises_never_breaks_the_arrival(self):
        window = _Window()

        def boom(jid):
            raise RuntimeError("vault")

        window.is_chat_locked = boom
        window._maybe_auto_transcribe(JID, _msg())
        assert window.queue.submitted == []


class _FakeRun:
    """Stands in for MessageTranscription: finishes at start()."""

    outcome = (None, None)
    built = []
    resolution = None

    def __init__(self, msg, settings, key, voice_dir, media_dir, **kwargs):
        self.kwargs = kwargs
        _FakeRun.built.append(self)

    cancelled = False

    def start(self):
        result, error = _FakeRun.outcome
        self.kwargs["on_finished"](result, error)

    def join(self, timeout=None):
        self.joined = timeout

    def cancel(self):
        self.cancelled = True


@pytest.fixture
def fake_run(monkeypatch):
    _FakeRun.built = []
    _FakeRun.resolution = None
    monkeypatch.setattr(message_run, "MessageTranscription", _FakeRun)
    monkeypatch.setattr(auto_transcription, "external_models",
                        type("E", (), {"read_references": staticmethod(lambda a: ((), True))}))
    monkeypatch.setattr(auto_transcription.transcription_preferences, "stored_models_dir",
                        lambda app: "")
    monkeypatch.setattr(auto_transcription, "data_path", lambda *parts: "/".join(parts))
    return _FakeRun


class TestTheRun:
    def test_a_result_is_stored_as_an_inline_transcription(self, fake_run):
        window = _Window()
        msg = _msg()
        window.records = [msg]
        fake_run.outcome = (_result("bom dia"), None)

        window._run_auto_transcription((JID, "M1", msg))

        [(jid, msg_id, value)] = window.stored
        assert (jid, msg_id) == (JID, "M1")
        assert value["text"] == "bom dia"
        assert stored_transcription.is_inline(value)
        assert window.spoken == []

    def test_it_runs_with_the_arrivals_own_hooks(self, fake_run):
        window = _Window()
        msg = _msg()
        window.records = [msg]
        fake_run.outcome = (_result(), None)
        window._run_auto_transcription((JID, "M1", msg))
        kwargs = fake_run.built[0].kwargs
        assert kwargs["fetch_media"] == window._auto_fetch_media
        assert kwargs["is_online"]() is True
        assert "on_phase" not in kwargs and "on_progress" not in kwargs

    def test_an_empty_result_is_not_stored(self, fake_run):
        window = _Window()
        msg = _msg()
        window.records = [msg]
        fake_run.outcome = (_result(""), None)
        window._run_auto_transcription((JID, "M1", msg))
        assert window.stored == []

    def test_a_message_the_user_already_decided_about_is_left_alone(self, fake_run):
        window = _Window()
        msg = _msg(**{KEY: stored_transcription.tombstone(5.0)})
        window.records = [msg]
        fake_run.outcome = (_result(), None)
        window._run_auto_transcription((JID, "M1", msg))
        assert window.stored == []

    def test_a_message_that_left_memory_is_not_stored(self, fake_run):
        window = _Window()
        fake_run.outcome = (_result(), None)
        window._run_auto_transcription((JID, "M1", _msg()))
        assert window.stored == []

    def test_turning_the_setting_off_meanwhile_stops_it(self, fake_run):
        window = _Window(enabled=False)
        window._run_auto_transcription((JID, "M1", _msg()))
        assert fake_run.built == []

    def test_a_setup_fault_pauses_and_says_so_once(self, fake_run):
        window = _Window()
        fake_run.outcome = (None, errors.TranscriptionError(errors.MODEL_NOT_INSTALLED, "x"))
        window._run_auto_transcription((JID, "M1", _msg("M1")))
        window._run_auto_transcription((JID, "M2", _msg("M2")))
        assert window.spoken == ["transcription_auto_paused"]
        assert len(window.queue.paused) == 2

    def test_a_fault_of_one_message_says_nothing(self, fake_run):
        window = _Window()
        fake_run.outcome = (None, errors.TranscriptionError(errors.FFMPEG_FAILED, "x"))
        window._run_auto_transcription((JID, "M1", _msg()))
        assert window.spoken == [] and window.queue.paused == []


class _EveryPauseIsNew(_Queue):
    """A queue whose pause has always ended by the next failure: the quarter
    of an hour that passes between two voice notes in real life."""

    def pause(self):
        self.paused.append(True)
        return True


def _fail(window, fake_run, code, msg_id="M1"):
    fake_run.outcome = (None, errors.TranscriptionError(code, "x"))
    window._run_auto_transcription((JID, msg_id, _msg(msg_id)))


class TestTheNoticeIsNotRepeated:
    """The report: "from time to time it says transcription was paused, with
    no transcription running" — a new pause every 15 minutes, each one said."""

    def test_the_same_fault_after_the_pause_ended_is_not_said_again(self, fake_run):
        window = _Window()
        window.queue = window._auto_transcription_queue = _EveryPauseIsNew()
        for msg_id in ("M1", "M2", "M3"):
            _fail(window, fake_run, errors.MODEL_NOT_INSTALLED, msg_id)
        assert window.spoken == ["transcription_auto_paused"]
        assert len(window.queue.paused) == 3

    def test_another_fault_is_said_in_its_turn(self, fake_run):
        window = _Window()
        window.queue = window._auto_transcription_queue = _EveryPauseIsNew()
        _fail(window, fake_run, errors.MODEL_NOT_INSTALLED, "M1")
        _fail(window, fake_run, errors.WHISPER_CPP_NOT_INSTALLED, "M2")
        assert window.spoken == ["transcription_auto_paused"] * 2

    def test_a_run_that_worked_makes_the_fault_news_again(self, fake_run):
        window = _Window()
        window.queue = window._auto_transcription_queue = _EveryPauseIsNew()
        _fail(window, fake_run, errors.MODEL_NOT_INSTALLED, "M1")
        fake_run.outcome = (_result(""), None)
        window._run_auto_transcription((JID, "M2", _msg("M2")))
        _fail(window, fake_run, errors.MODEL_NOT_INSTALLED, "M3")
        assert window.spoken == ["transcription_auto_paused"] * 2

    def test_applying_the_settings_resumes_the_queue_and_makes_it_news_again(self, fake_run):
        window = _Window()
        window.queue = window._auto_transcription_queue = _EveryPauseIsNew()
        _fail(window, fake_run, errors.MODEL_NOT_INSTALLED, "M1")
        window.transcription_settings_applied()
        assert window.queue.resumed == 1
        _fail(window, fake_run, errors.MODEL_NOT_INSTALLED, "M2")
        assert window.spoken == ["transcription_auto_paused"] * 2

    def test_applying_the_settings_before_any_queue_is_fine(self):
        window = _Window()
        window._auto_transcription_queue = None
        window.transcription_settings_applied()


class TestAFullMemoryIsNotAWrongSetup:
    def test_no_model_fitting_the_free_memory_neither_pauses_nor_speaks(self, fake_run):
        window = _Window()
        fake_run.resolution = type("R", (), {
            "model_id": None, "model_none_reason": preferences.MODEL_NONE_NOTHING_FITS})()
        _fail(window, fake_run, errors.MODEL_NOT_INSTALLED)
        assert window.spoken == [] and window.queue.paused == []

    def test_nothing_installed_and_unmeasured_still_pauses(self, fake_run):
        window = _Window()
        fake_run.resolution = type("R", (), {
            "model_id": None, "model_none_reason": preferences.MODEL_NONE_UNMEASURED})()
        _fail(window, fake_run, errors.MODEL_NOT_INSTALLED)
        assert window.queue.paused == [True]


class TestTheNoticeSaysWhy:
    @pytest.fixture
    def window(self, monkeypatch):
        window = _Window()
        window.i18n.texts = {"transcription_auto_paused": "Paused. {reason}"}
        return window

    def test_nothing_installed(self, window, fake_run):
        _fail(window, fake_run, errors.MODEL_NOT_INSTALLED)
        assert window.spoken == ["Paused. transcription_error_model_not_installed"]

    def test_the_automatic_choice_could_not_measure_the_memory(self, window, fake_run):
        fake_run.resolution = type("R", (), {
            "model_id": None, "model_none_reason": preferences.MODEL_NONE_UNMEASURED,
            "backend_id": None, "substitutions": ()})()
        _fail(window, fake_run, errors.MODEL_NOT_INSTALLED)
        assert window.spoken == ["Paused. transcription_model_none_unmeasured"]

    def test_no_backend_at_all(self, window, fake_run):
        _fail(window, fake_run, errors.BACKEND_MISSING)
        assert window.spoken == [
            "Paused. " + errors.error_i18n_key(errors.BACKEND_MISSING)]

    def test_every_setup_fault_has_a_sentence_and_no_field_left(self, window):
        """Each code through the real catalog: nothing read out as a key or
        as a {field} — the notice is all the user gets."""
        from core.i18n import I18n
        window.i18n = I18n("en-US")
        for code in sorted(auto_queue.SETUP_FAILURES):
            sentence = window.i18n.t("transcription_auto_paused").format(
                reason=window._auto_pause_reason(code, None))
            assert "{" not in sentence and "transcription_" not in sentence, code


class _StatusWindow(_Window):
    for _name in ("_run_status_auto_transcription", "_fetch_status_media"):
        locals()[_name] = getattr(StatusTranscriptionMixin, _name)
    del _name

    def __init__(self):
        super().__init__()
        self.settings["transcription"]["auto_transcribe_statuses"] = True


class TestAStatusFollowsTheSameRule:
    @pytest.fixture(autouse=True)
    def _temp(self, monkeypatch, tmp_path):
        import contextlib

        @contextlib.contextmanager
        def folder(prefix):
            yield str(tmp_path)

        monkeypatch.setattr(status_transcription._fileops, "private_temp_dir", folder)

    def test_a_status_says_it_once_and_a_status_that_worked_rearms_it(self, fake_run):
        window = _StatusWindow()
        window.queue = window._auto_transcription_queue = _EveryPauseIsNew()
        status = _msg("S1")
        fake_run.outcome = (None, errors.TranscriptionError(errors.MODEL_NOT_INSTALLED, "x"))
        window._run_auto_transcription((statuses.STATUS_JID, "S1", status))
        window._run_auto_transcription((statuses.STATUS_JID, "S2", status))
        assert window.spoken == ["transcription_auto_paused"]
        fake_run.outcome = (_result(""), None)
        window._run_auto_transcription((statuses.STATUS_JID, "S3", status))
        fake_run.outcome = (None, errors.TranscriptionError(errors.MODEL_NOT_INSTALLED, "x"))
        window._run_auto_transcription((statuses.STATUS_JID, "S4", status))
        assert window.spoken == ["transcription_auto_paused"] * 2


class TestRepainting:
    def test_the_open_conversation_repaints_the_row(self):
        window = _Window()
        msg = _msg()
        window.records = [msg]
        repainted = []

        class _Panel:
            conversation = {"remoteJid": JID}

            def _repaint_or_repopulate(self, ids):
                repainted.append(ids)

        window.conversations_panel = _Panel()
        window._store_auto_transcription(JID, "M1", _result())
        assert repainted == [["M1"]]

    def test_another_conversation_is_not_touched(self):
        window = _Window()
        window.records = [_msg()]
        repainted = []

        class _Panel:
            conversation = {"remoteJid": "other@g.us"}

            def _repaint_or_repopulate(self, ids):
                repainted.append(ids)

        window.conversations_panel = _Panel()
        window._store_auto_transcription(JID, "M1", _result())
        assert repainted == []
        assert len(window.stored) == 1


class TestFetchingTheAudio:
    def test_without_auto_download_it_fetches_itself(self, monkeypatch, tmp_path):
        window = _Window()
        path = tmp_path / "M1.msv"
        calls = []
        monkeypatch.setattr(auto_transcription, "auto_download_enabled", lambda s: False)
        window.handle_audio_message = lambda msg: calls.append(msg) or path.write_bytes(b"x")
        assert window._auto_fetch_media(_msg(), str(path)) is True
        assert len(calls) == 1

    def test_with_auto_download_it_waits_for_the_file_already_coming(self, monkeypatch, tmp_path):
        window = _Window()
        path = tmp_path / "M1.msv"
        monkeypatch.setattr(auto_transcription, "auto_download_enabled", lambda s: True)
        monkeypatch.setattr(auto_transcription, "auto_download_allows", lambda s, m: True)
        monkeypatch.setattr(auto_transcription, "_DOWNLOAD_POLL_SECONDS", 0)
        polls = []

        def sleep(seconds):
            polls.append(seconds)
            if len(polls) == 2:
                path.write_bytes(b"x")

        monkeypatch.setattr(auto_transcription.time, "sleep", sleep)
        window.handle_audio_message = lambda msg: pytest.fail("a second download was started")
        assert window._auto_fetch_media(_msg(), str(path)) is True

    def test_a_download_that_never_arrives_falls_back_to_fetching(self, monkeypatch, tmp_path):
        window = _Window()
        path = tmp_path / "M1.msv"
        calls = []
        monkeypatch.setattr(auto_transcription, "auto_download_enabled", lambda s: True)
        monkeypatch.setattr(auto_transcription, "auto_download_allows", lambda s, m: True)
        monkeypatch.setattr(auto_transcription, "_DOWNLOAD_WAIT_SECONDS", 0)
        window.handle_audio_message = lambda msg: calls.append(msg)
        assert window._auto_fetch_media(_msg(), str(path)) is False
        assert len(calls) == 1


class TestTheLockIsCheckedAgainBeforeARun:
    def test_a_chat_locked_while_queued_is_not_run(self, fake_run):
        window = _Window()
        window._locked = True
        window._run_auto_transcription((JID, "M1", _msg()))
        assert fake_run.built == []

    def test_a_lock_check_that_raises_runs_nothing(self, fake_run):
        window = _Window()

        def boom(jid):
            raise RuntimeError("vault")

        window.is_chat_locked = boom
        window._run_auto_transcription((JID, "M1", _msg()))
        assert fake_run.built == []


class _Running:
    def __init__(self):
        self.cancelled = False
        self.joined = None

    def cancel(self):
        self.cancelled = True

    def join(self, timeout=None):
        self.joined = timeout


class TestQuitting:
    def test_the_queue_stops_and_its_run_is_cancelled_and_waited_for(self):
        window = _Window()
        running = _Running()
        window._auto_transcription_current = running
        window.stop_transcription_runs(timeout=1.0)
        assert window.queue.stopped is True
        assert running.cancelled is True
        assert running.joined is not None and running.joined <= 1.0

    def test_a_bulk_run_is_cancelled_too(self):
        window = _Window()
        bulk = _Running()
        window.conversations_panel = type(
            "P", (), {"_cancel_bulk_transcription_now": lambda self: (bulk.cancel(), bulk)[1]})()
        window.stop_transcription_runs(timeout=1.0)
        assert bulk.cancelled is True and bulk.joined is not None

    def test_nothing_running_is_fine(self):
        window = _Window()
        window._auto_transcription_queue = None
        window.stop_transcription_runs(timeout=0.1)

    def test_a_run_starting_after_the_stop_is_cancelled_before_it_starts(self, fake_run):
        window = _Window()
        msg = _msg()
        window.records = [msg]
        window.queue.stopped = True
        fake_run.outcome = (None, None)
        window._run_auto_transcription((JID, "M1", msg))
        assert fake_run.built[0].cancelled is True
        assert window._auto_transcription_current is None


class TestShutdownCallsIt:
    """_perform_shutdown() against a minimal stub, as
    tests/test_perform_shutdown_reentrancy.py runs it."""

    def test_perform_shutdown_stops_the_transcription_runs_before_the_server(self):
        import threading

        from main_window.window_lifecycle import WindowLifecycleMixin

        order = []

        class _Stub:
            _perform_shutdown = WindowLifecycleMixin._perform_shutdown

            def __init__(self):
                self._teardown_started_lock = threading.Lock()
                self._teardown_complete_event = threading.Event()
                self._shutting_down = False

            def _stop_voice_call_audio(self, grace_seconds=0.0):
                pass

            def stop_transcription_runs(self):
                order.append("transcriptions")

            def _stop_wpp_server(self):
                order.append("server")

            def _flush_pending_debounced_saves(self):
                pass

        assert _Stub()._perform_shutdown() is True
        assert order == ["transcriptions", "server"]
