"""Live captions and the call: the one hook in the audio path, and the window's
half (main_window/call_captions.py), ACC-04.

The golden rule is that the call's audio never gets worse because of the
captions, so the first group pins how little the audio path does for them and
that nothing the tap does can reach the call. The second group runs the
mixin's methods unbound on a plain stub: no window, no wx loop, no thread.
"""

import types

import numpy as np
import pytest

from core import call_captions as cc
from core.call_audio import CallAudioConfig, CallAudioSession
from core.shortcut_catalog import SHORTCUTS
from main_window import call_captions as glue
from main_window.call_captions import CallCaptionsMixin, stop_call_captions


class _Socket:
    def emit(self, name, payload):
        pass


def _session():
    return CallAudioSession(_Socket(), CallAudioConfig(session="winzapp"),
                            sounddevice_module=object())


def _pcm(frames=960):
    return np.full(frames, 1000, dtype="<i2").tobytes()


# ── The audio path ───────────────────────────────────────────────────────────


class TestTheAudioPath:
    def test_a_tap_is_offered_the_received_audio_and_the_call_still_gets_it(self):
        session, tap = _session(), cc.RemoteTap()
        session.set_caption_tap(tap)
        session.enqueue_remote_audio(_pcm(), 48000)
        assert tap.take() == [(_pcm(), 48000)]
        assert session._output_queue.qsize() == 1

    def test_without_a_tap_nothing_changes(self):
        session = _session()
        session.enqueue_remote_audio(_pcm(), 48000)
        assert session._output_queue.qsize() == 1

    def test_a_tap_that_raises_is_dropped_and_the_audio_is_not(self):
        class Broken:
            def offer(self, pcm, rate):
                raise RuntimeError("boom")

        session = _session()
        session.set_caption_tap(Broken())
        session.enqueue_remote_audio(_pcm(), 48000)
        assert session._output_queue.qsize() == 1
        assert session._caption_tap is None

    def test_a_full_tap_costs_the_call_nothing(self):
        session, tap = _session(), cc.RemoteTap(max_chunks=2)
        session.set_caption_tap(tap)
        for _ in range(10):
            session.enqueue_remote_audio(_pcm(), 48000)
        assert tap.dropped_chunks == 8
        assert session._output_queue.qsize() == 10

    def test_the_tap_gets_the_original_speech_when_a_translation_takes_it_over(self):
        class Direction:
            def __init__(self):
                self.fed = 0

            def feed(self, pcm, rate):
                self.fed += 1
                return True

        session, tap = _session(), cc.RemoteTap()
        direction = Direction()
        session._live_translation = (direction,)
        session.set_caption_tap(tap)
        session.enqueue_remote_audio(_pcm(), 48000)
        assert direction.fed == 1
        assert len(tap.take()) == 1
        assert session._output_queue.qsize() == 0   # the translation owns playback

    def test_audio_that_the_session_refuses_is_not_tapped(self):
        session, tap = _session(), cc.RemoteTap()
        session.set_caption_tap(tap)
        session._stop_event.set()
        session.enqueue_remote_audio(_pcm(), 48000)
        assert tap.take() == []

    def test_the_hook_in_the_audio_path_is_one_guarded_offer(self):
        import inspect
        source = inspect.getsource(CallAudioSession.enqueue_remote_audio)
        assert "tap.offer(pcm, sample_rate)" in source
        assert "except Exception" in source

    def test_the_underrun_count_is_readable_for_the_guard(self):
        assert _session().remote_underruns == 0


# ── The command ──────────────────────────────────────────────────────────────


class _Engine:
    def __init__(self):
        self.stopped = 0

    def stop(self):
        self.stopped += 1


class _Window:
    def __init__(self):
        self.statuses = []
        self.focused = 0

    def set_status(self, text):
        self.statuses.append(text)

    def focus_list(self):
        self.focused += 1

    def IsBeingDeleted(self):
        return False

    def __bool__(self):
        return True


class _Stub:
    _voice_call_last_announced_state = "ACTIVE"

    def __init__(self):
        self.spoken = []
        self.i18n = types.SimpleNamespace(t=lambda key: key)
        self._call_audio_session = types.SimpleNamespace(running=True)
        self._active_voice_call = {"call_id": "c1", "peer_jid": "1@s.whatsapp.net"}
        self.conversations_panel = types.SimpleNamespace()

    def output(self, text, interrupt=False):
        self.spoken.append(text)

    _call_captions_prepare = CallCaptionsMixin._call_captions_prepare

    def press(self):
        CallCaptionsMixin._on_call_captions_command(self)


@pytest.fixture
def idle_slot(monkeypatch):
    monkeypatch.setattr(glue.machine_slot, "slot", types.SimpleNamespace(holding=False))


@pytest.fixture
def threads(monkeypatch):
    started = []

    class FakeThread:
        def __init__(self, target=None, args=(), **kwargs):
            self.target, self.args = target, args

        def start(self):
            started.append(self)

    monkeypatch.setattr(glue.threading, "Thread", FakeThread)
    return started


class TestTheCommand:
    def test_outside_a_call_it_says_so(self, idle_slot, threads):
        stub = _Stub()
        stub._call_audio_session = None
        stub.press()
        assert stub.spoken == [cc.REASON_NO_CALL]
        assert threads == []

    def test_a_call_that_is_not_active_yet_is_refused(self, idle_slot, threads):
        stub = _Stub()
        stub._voice_call_last_announced_state = "RINGING"
        stub.press()
        assert stub.spoken == [cc.REASON_NO_CALL]

    def test_a_transcription_holding_the_model_is_refused(self, monkeypatch, threads):
        monkeypatch.setattr(glue.machine_slot, "slot", types.SimpleNamespace(holding=True))
        stub = _Stub()
        stub.press()
        assert stub.spoken == [cc.REASON_BUSY]
        assert threads == []

    def test_a_dictation_in_progress_is_refused(self, idle_slot, threads):
        from core import dictation
        stub = _Stub()
        state = dictation.Dictation()
        state.begin()
        stub.conversations_panel = types.SimpleNamespace(_dictation=state)
        stub.press()
        assert stub.spoken == [cc.REASON_BUSY]

    def test_a_free_active_call_prepares_off_the_ui_thread_and_says_nothing_yet(
            self, idle_slot, threads):
        stub = _Stub()
        stub.press()
        assert stub.spoken == []
        assert len(threads) == 1
        assert threads[0].target.__name__ == "_call_captions_prepare"

    def test_a_second_press_while_preparing_does_not_start_another(self, idle_slot, threads):
        stub = _Stub()
        stub.press()
        stub.press()
        assert len(threads) == 1

    def test_pressing_again_moves_to_the_window_and_starts_nothing(self, idle_slot, threads):
        stub = _Stub()
        window = _Window()
        stub._call_captions = {"window": window, "stopped": False,
                               "call": stub._active_voice_call}
        stub.press()
        assert window.focused == 1
        assert threads == []

    def test_the_window_of_a_stopped_run_of_an_earlier_call_is_left_alone(
            self, idle_slot, threads):
        stub = _Stub()
        window = _Window()
        stub._call_captions = {"window": window, "stopped": True, "call": {"call_id": "old"}}
        stub.press()
        assert window.focused == 0
        assert len(threads) == 1

    def test_a_result_that_arrives_after_the_call_ended_starts_nothing(self):
        stub = _Stub()
        token = object()
        stub._call_captions_starting = token
        stub._call_audio_session = None
        CallCaptionsMixin._call_captions_ready(stub, token, object(), None)
        assert stub.spoken == [cc.REASON_NO_CALL]
        assert getattr(stub, "_call_captions", None) is None

    def test_a_stale_prepare_result_is_ignored(self):
        stub = _Stub()
        stub._call_captions_starting = object()
        CallCaptionsMixin._call_captions_ready(stub, object(), object(), None)
        assert stub.spoken == []


# ── Stopping ─────────────────────────────────────────────────────────────────


class _Session:
    running = True

    def __init__(self):
        self._caption_tap = None

    def set_caption_tap(self, tap):
        self._caption_tap = tap


class TestStopping:
    @pytest.fixture(autouse=True)
    def _after(self, monkeypatch):
        self.later = []
        monkeypatch.setattr(glue.wx, "CallAfter", lambda fn, *a: self.later.append((fn, a)))

    def _state(self, session):
        tap = cc.RemoteTap()
        session.set_caption_tap(tap)
        return {"tap": tap, "session": session, "stopped": False,
                "window": _Window(), "engine": _Engine(), "call": {}}

    def test_the_end_of_a_call_stops_the_engine_detaches_the_tap_and_keeps_the_text(self):
        stub = _Stub()
        session = _Session()
        stub._call_captions = state = self._state(session)
        assert stop_call_captions(stub) is True
        assert state["engine"].stopped == 1
        assert session._caption_tap is None
        assert not state["tap"].active
        for fn, args in self.later:
            fn(*args)
        assert state["window"].statuses == ["call_captions_status_ended"]

    def test_stopping_twice_does_nothing_the_second_time(self):
        stub = _Stub()
        stub._call_captions = state = self._state(_Session())
        stop_call_captions(stub)
        assert stop_call_captions(stub) is False
        assert state["engine"].stopped == 1

    def test_no_captions_is_not_an_error(self):
        assert stop_call_captions(_Stub()) is False

    def test_a_session_holding_another_tap_is_left_alone(self):
        stub = _Stub()
        session = _Session()
        stub._call_captions = self._state(session)
        other = cc.RemoteTap()
        session.set_caption_tap(other)
        stop_call_captions(stub)
        assert session._caption_tap is other

    def test_the_engine_stopping_itself_says_why_once(self):
        stub = _Stub()
        stub._call_captions = state = self._state(_Session())
        CallCaptionsMixin._call_captions_engine_stopped(stub, state, "protected")
        CallCaptionsMixin._call_captions_engine_stopped(stub, state, "protected")
        assert stub.spoken == ["call_captions_protected"]
        assert state["stopped"]

    def test_a_failure_says_the_failure_sentence(self):
        stub = _Stub()
        stub._call_captions = state = self._state(_Session())
        CallCaptionsMixin._call_captions_engine_stopped(stub, state, "failed")
        assert stub.spoken == ["call_captions_failed"]

    def test_closing_the_window_turns_the_captions_off_and_says_so(self):
        stub = _Stub()
        stub.voice_call_window = None
        stub._call_captions = state = self._state(_Session())
        window = state["window"]
        CallCaptionsMixin._on_call_captions_window_closed(stub, window)
        assert state["engine"].stopped == 1
        assert stub._call_captions is None
        assert stub.spoken == ["call_captions_stopped"]

    def test_closing_the_window_after_the_call_ended_says_nothing(self):
        stub = _Stub()
        stub.voice_call_window = None
        stub._call_captions = state = self._state(_Session())
        window = state["window"]
        stop_call_captions(stub)
        CallCaptionsMixin._on_call_captions_window_closed(stub, window)
        assert stub.spoken == []

    def test_closing_an_old_window_does_not_touch_the_new_run(self):
        stub = _Stub()
        stub.voice_call_window = None
        stub._call_captions = state = self._state(_Session())
        CallCaptionsMixin._on_call_captions_window_closed(stub, _Window())
        assert stub._call_captions is state
        assert not state["stopped"]


class TestAfterADeviceSwitch:
    def test_the_tap_follows_the_new_session(self):
        class Button:
            def IsShown(self):
                return True

            def HasFocus(self):
                return False

            def Show(self, shown):
                self.shown = shown

        stub = _Stub()
        stub.voice_call_window_captions_button = button = Button()
        old, new = _Session(), _Session()
        tap = cc.RemoteTap()
        old.set_caption_tap(tap)
        stub._call_captions = {"tap": tap, "session": old, "stopped": False}
        stub._call_audio_session = new
        CallCaptionsMixin._sync_call_captions_button(stub, {"call_id": "c1"})
        assert new._caption_tap is tap
        assert stub._call_captions["session"] is new
        assert button.shown is True


# ── The shortcut ─────────────────────────────────────────────────────────────


def test_the_command_has_one_catalog_row_on_a_combination_nothing_else_uses():
    row = next(s for s in SHORTCUTS if s.id == "call.ID_CALL_CAPTIONS")
    assert (row.mod, row.key) == (6, 88)   # Ctrl+Shift+X
    others = [s for s in SHORTCUTS if s is not row and (s.mod, s.key) == (6, 88)]
    assert others == []
