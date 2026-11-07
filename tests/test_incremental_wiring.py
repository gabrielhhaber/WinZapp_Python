"""Real-adapter wiring tests: the actual voice_recording mixin functions,
with wx and sound_lib stubbed (no wx, no audio device, no model file).

The functions under test are the UNMODIFIED module-level code of
ui/conversation_panel/voice_recording.py — _start_incremental_denoise and
_cancel_incremental_denoise — called with a minimal fake ``self`` (the
mixin's methods are plain functions; self only needs the attributes each
function touches). wx.CallAfter is recorded, not executed. The model file
is absent in this environment, so the real drain thread deterministically
takes its documented FileNotFoundError -> failed -> cleanup -> slot-release
path — itself under test here.
"""

import sys
import types

import pytest


class _WxStub(types.ModuleType):
    """Every attribute is a subclassable dummy class (class definitions at
    module level do ``class X(wx.Panel)`` etc.)."""

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        cls = type(name, (), {})
        setattr(self, name, cls)
        return cls


wx_stub = _WxStub("wx")
WX_CALLS = []
wx_stub.CallAfter = lambda fn, *a, **k: WX_CALLS.append((fn, a, k))
sys.modules.setdefault("wx", wx_stub)
_sound_lib = types.ModuleType("sound_lib")
_sound_lib.stream = types.ModuleType("sound_lib.stream")
sys.modules.setdefault("sound_lib", _sound_lib)
sys.modules.setdefault("sound_lib.stream", _sound_lib.stream)

from core.incremental_denoise import (  # noqa: E402
    CANCELLED, IncrementalDenoiseController,
)
from ui.conversation_panel import voice_recording as vr  # noqa: E402


class _FakeMainWindow:
    def __init__(self, nr_enabled=True, with_controller=True):
        self.settings = {"general": {"noise_reduction_enabled": nr_enabled}}
        if with_controller:
            self._incremental_controller = IncrementalDenoiseController()


class _FakePanel:
    """Just the attributes the wiring functions touch."""

    def __init__(self, **mw_kwargs):
        self.main_window = _FakeMainWindow(**mw_kwargs)
        self._incremental_session = None
        self._recording_channel_mode = "auto"
        # The real drain method, bound as it would be on the mixin instance.
        self._incremental_drain = (
            lambda session: vr.VoiceRecordingMixin._incremental_drain(self, session))


def _start(panel, frames=None):
    vr.VoiceRecordingMixin._start_incremental_denoise(
        panel, frames if frames is not None else [], 48000, 1)


def _join_drain(panel, timeout=5.0):
    thread = panel._incremental_session["thread"]
    thread.join(timeout)
    assert not thread.is_alive(), "drain thread did not exit"


def test_start_registers_session_and_model_missing_fails_clean():
    panel = _FakePanel()
    _start(panel)
    session = panel._incremental_session
    assert session is not None
    controller = panel.main_window._incremental_controller
    assert controller.is_preparing(session["local_id"])
    # The real drain: model file absent -> failed, spool cleaned, slot released.
    _join_drain(panel)
    assert session["failed"]
    import os
    assert not os.path.exists(session["spool"])
    # The slot is free for the next recording...
    assert controller.acquire_slot("other", {"cancel": False})
    controller.release_slot("other")
    # ...but the send authority is KEPT: the send path falls back to offline
    # and must still pass transfer() for this local_id.
    assert controller.is_preparing(session["local_id"])
    assert controller.transfer(session["local_id"])
    # A fresh start on the same panel cancels/discards the old session first.
    _start(panel)
    assert panel._incremental_session is not None
    panel._incremental_session["cancel"] = True
    controller.discard(panel._incremental_session["local_id"])
    _join_drain(panel)


def test_drain_model_load_failure_marks_model_unavailable():
    # The model file is absent in this environment: the real drain takes
    # its documented FileNotFoundError path BEFORE the worker exists.
    panel = _FakePanel()
    _start(panel)
    _join_drain(panel)
    session = panel._incremental_session
    assert session["failed"] is True
    assert session["model_unavailable"] is True
    assert session["worker"] is None


def test_drain_feed_failure_is_not_model_unavailable(monkeypatch):
    import core.neural_noise_reduction as nnr
    import core.incremental_denoise as inc
    monkeypatch.setattr(nnr, "model_path", lambda: __file__)  # any real file
    monkeypatch.setattr(nnr, "NeuralDenoiser", lambda: object())
    class _Worker:
        def abort(self):
            pass
    monkeypatch.setattr(inc, "IncrementalDenoiser", lambda *a, **k: _Worker())
    def _boom(session, worker, transform):
        raise RuntimeError("feed blew up")
    monkeypatch.setattr(inc, "drain_capture_frames", _boom)
    panel = _FakePanel()
    _start(panel)
    _join_drain(panel)
    session = panel._incremental_session
    assert session["failed"] is True
    # Worker existed: "error with worker released" — the published
    # neural/basic offline path remains allowed for this note.
    assert "model_unavailable" not in session


def test_start_skipped_when_noise_reduction_disabled():
    panel = _FakePanel(nr_enabled=False)
    _start(panel)
    assert panel._incremental_session is None


def test_start_skipped_when_no_controller():
    panel = _FakePanel(with_controller=False)
    _start(panel)
    assert panel._incremental_session is None


def test_start_goes_offline_when_slot_occupied():
    panel = _FakePanel()
    controller = panel.main_window._incremental_controller
    assert controller.acquire_slot("someone-else", {"cancel": False})
    _start(panel)
    # No session, no thread, no model: the recording goes to the basic gate.
    assert panel._incremental_session is None
    assert panel._incremental_basic_only is True
    assert controller.is_preparing("someone-else")


def test_start_clears_basic_only_when_the_slot_is_free():
    panel = _FakePanel()
    panel._incremental_basic_only = True  # stale flag from a previous busy note
    _start(panel)
    assert panel._incremental_basic_only is False
    session = panel._incremental_session
    assert session is not None
    session["cancel"] = True
    _join_drain(panel)


class _FakeI18n:
    @staticmethod
    def t(msgid):
        return msgid  # keep the msgid observable in assertions


class _FakeMwForReduce:
    def __init__(self):
        self.announced = []
        self.output = self.announced.append
        self.i18n = _FakeI18n()


def _run_reduce(mw, basic_only, data=b"\x01\x02" * 4800):
    panel = _FakePanel()
    return vr.VoiceRecordingMixin._reduce_for_send(
        panel, mw, data, 48000, 1, basic_only)


def test_reduce_for_send_basic_only_uses_gate_never_neural(monkeypatch):
    import core.audio_processing as ap
    import core.neural_noise_reduction as nnr
    calls = []
    monkeypatch.setattr(ap, "apply_noise_gate",
                        lambda data, rate, ch, **kw: calls.append((rate, ch, kw)) or b"GATED")
    def _no_neural(*a, **k):
        raise AssertionError("reduce_voice_noise must not run while the slot is occupied")
    monkeypatch.setattr(nnr, "reduce_voice_noise", _no_neural)
    mw = _FakeMwForReduce()
    WX_CALLS.clear()
    data, engine = _run_reduce(mw, basic_only=True)
    assert (data, engine) == (b"GATED", "gate")
    assert calls == [(48000, 1, {"strict": True})]
    # The existing fallback announcement, not a new busy notice (CallAfter
    # is recorded by the wx stub, so execute the delivery to inspect it).
    assert len(WX_CALLS) == 1
    fn, args, _ = WX_CALLS[0]
    fn(*args)
    assert mw.announced == ["noise_reduction_fallback"]


def test_reduce_for_send_normal_path_is_the_published_one(monkeypatch):
    import core.audio_processing as ap
    import core.neural_noise_reduction as nnr
    monkeypatch.setattr(ap, "apply_noise_gate",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("gate must not be called directly here")))
    monkeypatch.setattr(nnr, "reduce_voice_noise",
                        lambda data, rate, ch: (b"NEURAL", "neural"))
    mw = _FakeMwForReduce()
    WX_CALLS.clear()
    data, engine = _run_reduce(mw, basic_only=False)
    assert (data, engine) == (b"NEURAL", "neural")
    assert WX_CALLS == []  # neural: nothing to announce


def test_reduce_for_send_gate_fallback_announces_existing_msgid(monkeypatch):
    import core.neural_noise_reduction as nnr
    # The published fallback: neural unavailable, reduce_voice_noise ran the gate.
    monkeypatch.setattr(nnr, "reduce_voice_noise",
                        lambda data, rate, ch: (b"GATED", "gate"))
    mw = _FakeMwForReduce()
    WX_CALLS.clear()
    data, engine = _run_reduce(mw, basic_only=False)
    assert (data, engine) == (b"GATED", "gate")
    assert len(WX_CALLS) == 1
    fn, args, _ = WX_CALLS[0]
    fn(*args)
    assert mw.announced == ["noise_reduction_fallback"]


def test_reduce_for_send_gate_failure_keeps_published_behavior(monkeypatch):
    import core.audio_processing as ap
    def _boom(data, rate, ch, **kw):
        raise RuntimeError("gate blew up")
    monkeypatch.setattr(ap, "apply_noise_gate", _boom)
    mw = _FakeMwForReduce()
    WX_CALLS.clear()
    original = b"\x03\x04" * 100
    data, engine = _run_reduce(mw, basic_only=True, data=original)
    # No further escalation step: unprocessed audio + the published notice.
    assert (data, engine) == (original, None)
    assert len(WX_CALLS) == 1
    fn, args, _ = WX_CALLS[0]
    fn(*args)
    assert mw.announced == ["noise_reduction_failed"]


def test_cancel_marks_session_and_controller():
    panel = _FakePanel()
    controller = panel.main_window._incremental_controller
    # Register a session directly (as if a recording were in flight) and make
    # it the panel's active one.
    session = {"local_id": "m1", "cancel": False}
    assert controller.acquire_slot("m1", session)
    panel._incremental_session = session
    vr.VoiceRecordingMixin._cancel_incremental_denoise(panel)
    assert session["cancel"] is True
    assert panel._incremental_session is None
    assert not controller.is_preparing("m1")
    # The drain's exit path releases the slot (simulated here as the wiring's
    # finally does); a new recording can take it.
    controller.release_slot("m1")
    assert controller.acquire_slot("m2", {"cancel": False})


def test_orphaned_previous_session_is_discarded_on_new_start():
    panel = _FakePanel()
    controller = panel.main_window._incremental_controller
    previous = {"local_id": "old", "cancel": False}
    assert controller.acquire_slot("old", previous)
    panel._incremental_session = previous
    # Slot held by the orphan: the new recording must still go offline, and
    # the orphan must be cancelled + discarded from the registry.
    _start(panel)
    assert previous["cancel"] is True
    assert not controller.is_preparing("old")
    assert panel._incremental_session is None


class _FakeStream:
    def __init__(self, fail=False):
        self.fail = fail
        self.closed = False

    def stop(self):
        if self.fail:
            raise RuntimeError("device wedged")

    def close(self):
        self.closed = True

    def stop_stream(self):
        if self.fail:
            raise RuntimeError("device wedged")


def _session_with_close(local_id="m1"):
    import threading as _t
    return {"local_id": local_id, "cancel": False,
            "stopped_event": _t.Event(), "device_failed": None}


def test_stop_recording_stream_confirmed_close_sets_event():
    panel = _FakePanel()
    panel._sd_stream = _FakeStream()
    panel._recording_stream = _FakeStream()
    session = _session_with_close()
    panel._incremental_session = session
    vr.VoiceRecordingMixin._stop_recording_stream(panel, session)
    assert session["stopped_event"].is_set()
    assert session["device_failed"] is False
    assert panel._sd_stream is None and panel._recording_stream is None


def test_stop_recording_stream_failed_close_is_not_reported_confirmed():
    panel = _FakePanel()
    panel._sd_stream = None
    panel._recording_stream = _FakeStream(fail=True)
    session = _session_with_close()
    vr.VoiceRecordingMixin._stop_recording_stream(panel, session)
    # The send thread wakes, but sees the failure marker — never a snapshot.
    assert session["stopped_event"].is_set()
    assert session["device_failed"] is True


def test_stop_recording_stream_defaults_to_the_active_session():
    panel = _FakePanel()
    panel._sd_stream = None
    panel._recording_stream = None
    session = _session_with_close()
    panel._incremental_session = session
    vr.VoiceRecordingMixin._stop_recording_stream(panel)   # no explicit session
    assert session["stopped_event"].is_set()


def test_on_destroy_cancels_incremental_for_normal_mic_recording():
    """Regression: destroy used to cancel the incremental session only inside
    the system-audio branch, orphaning normal mic recordings."""
    panel = _FakePanel()
    panel._recording_system_audio = False
    panel._recording_pa = None
    panel._video_player = None
    panel._cancel_incremental_denoise = (
        lambda: vr.VoiceRecordingMixin._cancel_incremental_denoise(panel))
    controller = panel.main_window._incremental_controller
    session = {"local_id": "m1", "cancel": False}
    assert controller.acquire_slot("m1", session)
    panel._incremental_session = session
    skipped = []
    event = type("E", (), {"GetEventObject": lambda self: panel,
                           "Skip": lambda self: skipped.append(True)})()
    vr.VoiceRecordingMixin._on_destroy(panel, event)
    assert session["cancel"] is True
    assert not controller.is_preparing("m1")
    assert skipped == [True]
