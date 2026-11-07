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
        self._close_stream_handle = vr.VoiceRecordingMixin._close_stream_handle


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


def test_start_goes_basic_only_when_slot_occupied():
    panel = _FakePanel()
    controller = panel.main_window._incremental_controller
    assert controller.acquire_slot("someone-else", {"cancel": False})
    _start(panel)
    session = panel._incremental_session
    # No thread, no model, no spool — but the note KEEPS its own identity:
    # registered send authority, close confirmation, cancel, echo exclusion.
    assert session is not None
    assert session["basic_only"] is True
    assert session["thread"] is None and session["worker"] is None
    assert "spool" not in session
    assert session["stopped_event"] is not None
    assert controller.is_preparing(session["local_id"])
    # The slot is still the other note's.
    assert not controller.acquire_slot("third", {"cancel": False})


def test_discard_basic_only_session_forgets_entry():
    panel = _FakePanel()
    controller = panel.main_window._incremental_controller
    assert controller.acquire_slot("someone-else", {"cancel": False})
    _start(panel)
    session = panel._incremental_session
    vr.VoiceRecordingMixin._cancel_incremental_denoise(panel)
    # No drain will ever run for a basic-only session: the entry is
    # forgotten by discard itself, not left for a nonexistent thread.
    assert session["cancel"] is True
    assert not controller.is_preparing(session["local_id"])
    assert panel._incremental_session is None


def test_start_clears_basic_only_when_the_slot_is_free():
    panel = _FakePanel()
    _start(panel)
    session = panel._incremental_session
    assert session is not None
    assert session["basic_only"] is False
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
    # A slot-holding orphan whose drain never exits (thread marker set):
    previous = {"local_id": "old", "cancel": False, "thread": object()}
    assert controller.acquire_slot("old", previous)
    panel._incremental_session = previous
    # Slot held by the orphan: the new recording goes basic-only, and the
    # orphan is cancelled + dropped from the send-authority registry (its
    # slot stays until its own drain confirms cleanup).
    _start(panel)
    assert previous["cancel"] is True
    assert not controller.is_preparing("old")
    session = panel._incremental_session
    assert session is not None and session["basic_only"] is True
    controller.discard(session["local_id"])


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
    stream = _FakeStream()
    session = _session_with_close()
    session["stream"] = stream
    panel._recording_stream = stream  # panel still points at THIS stream
    vr.VoiceRecordingMixin._stop_recording_stream(panel, session)
    assert session["stopped_event"].is_set()
    assert session["device_failed"] is False
    assert stream.closed
    # Identity match: the panel attribute is cleared.
    assert panel._recording_stream is None


def test_stop_recording_stream_closes_only_the_sessions_own_stream():
    # R1: closing recording A must not touch the panel attributes a newer
    # recording B already owns.
    panel = _FakePanel()
    stream_a, stream_b = _FakeStream(), _FakeStream()
    session_a = _session_with_close("a")
    session_a["stream"] = stream_a
    panel._recording_stream = stream_b
    panel._sd_stream = stream_b
    vr.VoiceRecordingMixin._stop_recording_stream(panel, session_a)
    assert stream_a.closed and not stream_b.closed
    assert panel._recording_stream is stream_b and panel._sd_stream is stream_b
    assert session_a["stopped_event"].is_set()
    assert session_a["device_failed"] is False


def test_stop_recording_stream_without_session_uses_panel_handles():
    panel = _FakePanel()
    stream = _FakeStream()
    panel._recording_stream = stream
    vr.VoiceRecordingMixin._stop_recording_stream(panel, None)
    assert stream.closed
    assert panel._recording_stream is None


def test_stop_recording_stream_failed_close_is_not_reported_confirmed():
    panel = _FakePanel()
    session = _session_with_close()
    session["stream"] = _FakeStream(fail=True)
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


# ═══════════════════════════════════════════════════════════════════════
# Real SEND-path tests: the unmodified _send_voice_message /
# _write_and_enqueue code, driven with fake panel/main-window/queue and
# controlled wx.CallAfter. No MainWindow, no audio device, no model file.
# ═══════════════════════════════════════════════════════════════════════
import os  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import wave  # noqa: E402

from main_window import message_events as me  # noqa: E402
from ui.conversation_panel import message_actions as ma  # noqa: E402


class _FakeQueue:
    def __init__(self):
        self.enqueued = []
        self.event = threading.Event()

    def enqueue(self, pm):
        self.enqueued.append(pm)
        self.event.set()


class _FakeSendMW:
    def __init__(self, controller):
        self.settings = {"general": {"noise_reduction_enabled": True}}
        self._incremental_controller = controller
        self.message_queue = _FakeQueue()
        from cryptography.fernet import Fernet
        self.key = Fernet.generate_key()
        self.i18n = _FakeI18n()
        # A real announce target so the fallback notice path runs and is
        # observable through the recorded wx.CallAfter calls.
        self.output = lambda *a, **k: None
        self.voicemsg_send_sound = types.SimpleNamespace(play=lambda: None)
        self.failed_calls = []

    def send_recording_status(self, *a):
        pass

    def mark_conversation_as_read(self, *a):
        pass

    def _schedule_set_chats(self):
        pass

    def _convert_wav_to_ogg(self, wav_path):
        return None  # OGG pre-encode unavailable: WAV fallback, as published

    def _on_message_failed(self, *a, **k):
        self.failed_calls.append((a, k))


class _SendPanel:
    """Fake conversation panel exposing the REAL mixin methods the send
    path calls on self."""

    def __init__(self, mw, frames, session):
        self.main_window = mw
        self._is_recording = True
        self._system_audio_session = None
        self._recording_system_audio = False
        self._recording_channel_mode = "mono"
        self.conversation = {"remoteJid": "peer@s.whatsapp.net"}
        self._recording_frames = frames
        self._incremental_session = session
        self._recording_actual_rate = 48000
        self._recording_actual_ch = 1
        self._quoted_message = None
        self._recording_paused = False
        self._sorted_messages = []
        self.registered = None
        self.messages_list = types.SimpleNamespace(EnsureVisible=lambda i: None)
        self.message_field = types.SimpleNamespace(SetFocus=lambda: None)
        self._close_stream_handle = vr.VoiceRecordingMixin._close_stream_handle

    def _stop_recording_stream(self, session=None):
        return vr.VoiceRecordingMixin._stop_recording_stream(self, session)

    def _reduce_for_send(self, *a):
        return vr.VoiceRecordingMixin._reduce_for_send(self, *a)

    def _clear_empty_placeholder(self):
        pass

    def _render_message_line(self, msg):
        return "row"

    def _register_virtual_msg(self, msg):
        self.registered = msg

    def _on_cancel_reply(self):
        pass

    def _hide_voice_panel(self):
        pass


@pytest.fixture
def send_env(monkeypatch, tmp_path):
    monkeypatch.setattr(vr, "append_message_row", lambda panel, row: None)
    monkeypatch.setattr(vr, "message_row_count", lambda panel: 1)
    monkeypatch.setattr(vr, "data_path", lambda name: str(tmp_path / name))
    os.makedirs(tmp_path / "voice_messages", exist_ok=True)
    return tmp_path


def _send_session(controller, frames, local_id="m1", basic_only=False,
                  thread=None, worker=None, spool=None, stream=None):
    session = {"local_id": local_id, "frames": frames, "rate": 48000, "ch": 1,
               "worker_ch": 1, "worker": worker, "thread": thread,
               "stop": False, "cancel": False, "failed": False, "revoked": False,
               "stopped_event": threading.Event(), "device_failed": False,
               "basic_only": basic_only}
    if spool:
        session["spool"] = spool
    if stream is not None:
        session["stream"] = stream
    if basic_only:
        controller.register(local_id, session)
    else:
        assert controller.acquire_slot(local_id, session)
    return session


def _wav_payload(path):
    with wave.open(path, "rb") as wf:
        return wf.readframes(wf.getnframes())


def _no_neural(*a, **k):
    raise AssertionError("reduce_voice_noise must not run for this note")


def test_send_blocked_drain_falls_back_to_basic_with_all_bytes(send_env, monkeypatch):
    import core.audio_processing as ap
    import core.neural_noise_reduction as nnr
    monkeypatch.setattr(vr, "_INCREMENTAL_SEND_WORKER_TIMEOUT", 0.3)
    monkeypatch.setattr(ap, "apply_noise_gate",
                        lambda data, rate, ch, **kw: bytes(reversed(data)))
    monkeypatch.setattr(nnr, "reduce_voice_noise", _no_neural)
    controller = IncrementalDenoiseController()
    frames = [b"\x01\x02" * 480, b"\x03\x04" * 480]
    raw = b"".join(frames)
    spool = str(send_env / "spool.pcm")
    open(spool, "wb").write(b"partial-work")

    class _Worker:
        def finish(self):
            raise AssertionError("finish must not run after the stage-2 timeout")

        def abort(self):
            pass

    session = _send_session(controller, frames, worker=_Worker(),
                            spool=spool, stream=_FakeStream())

    def _stuck_drain():
        # A natively stuck feed: alive until cancel, then the drain's own
        # exit contract (spool cleanup + slot release).
        while not session["cancel"]:
            time.sleep(0.02)
        try:
            os.unlink(session["spool"])
        except OSError:
            pass
        controller.release_slot(session["local_id"])

    drain = threading.Thread(target=_stuck_drain, daemon=True)
    drain.start()
    session["thread"] = drain

    mw = _FakeSendMW(controller)
    panel = _SendPanel(mw, frames, session)
    WX_CALLS.clear()
    vr.VoiceRecordingMixin._send_voice_message(panel, None)
    assert mw.message_queue.event.wait(10), "send thread never enqueued"
    pm = mw.message_queue.enqueued[0]
    # Basic gate on the COMPLETE raw (close was confirmed first), no ONNX.
    assert _wav_payload(pm.audio_path) == bytes(reversed(raw))
    assert session["fallback_chosen"] is True
    drain.join(5)
    assert not drain.is_alive()
    assert not os.path.exists(spool)
    # Authority consumed by the real hand-off; echo exclusion lifted there.
    assert not controller.is_preparing("m1")
    assert "_incremental_preparing" not in panel.registered
    # The existing fallback announcement was queued for the UI thread.
    assert any(args and args[0] == "noise_reduction_fallback"
               for fn, args, k in WX_CALLS for _ in [0]
               if fn == (mw.output or fn))


def test_send_basic_only_note_goes_straight_to_gate(send_env, monkeypatch):
    import core.audio_processing as ap
    import core.neural_noise_reduction as nnr
    monkeypatch.setattr(ap, "apply_noise_gate",
                        lambda data, rate, ch, **kw: bytes(reversed(data)))
    monkeypatch.setattr(nnr, "reduce_voice_noise", _no_neural)
    controller = IncrementalDenoiseController()
    frames = [b"\x05\x06" * 480]
    raw = b"".join(frames)
    session = _send_session(controller, frames, basic_only=True, stream=_FakeStream())
    mw = _FakeSendMW(controller)
    panel = _SendPanel(mw, frames, session)
    WX_CALLS.clear()
    vr.VoiceRecordingMixin._send_voice_message(panel, None)
    assert mw.message_queue.event.wait(10)
    pm = mw.message_queue.enqueued[0]
    assert _wav_payload(pm.audio_path) == bytes(reversed(raw))
    # The note kept full identity: confirmed close, authority, echo exclusion.
    assert session["stopped_event"].is_set()
    assert not controller.is_preparing("m1")  # consumed by transfer
    assert "_incremental_preparing" not in panel.registered
    # It never held the neural slot.
    assert controller.acquire_slot("other", {"cancel": False})


def test_send_waits_for_confirmed_close_and_includes_late_callback(send_env, monkeypatch):
    import core.neural_noise_reduction as nnr
    monkeypatch.setattr(nnr, "reduce_voice_noise",
                        lambda data, rate, ch: (data, "neural"))
    controller = IncrementalDenoiseController()
    frames = [b"A" * 960]
    close_gate = threading.Event()

    class _SlowCloseStream:
        closed = False

        def stop_stream(self):
            pass

        def close(self):
            close_gate.wait(5)
            self.closed = True

    stream = _SlowCloseStream()
    session = _send_session(controller, frames, stream=stream)
    mw = _FakeSendMW(controller)
    panel = _SendPanel(mw, frames, session)
    vr.VoiceRecordingMixin._send_voice_message(panel, None)
    # The close has not confirmed: nothing may be enqueued yet.
    time.sleep(0.4)
    assert not mw.message_queue.event.is_set()
    # A capture callback landing BEFORE the confirmed close is part of the
    # recording and must be in the sent audio.
    frames.append(b"B" * 960)
    close_gate.set()
    assert mw.message_queue.event.wait(10)
    pm = mw.message_queue.enqueued[0]
    assert _wav_payload(pm.audio_path) == b"A" * 960 + b"B" * 960


def test_send_revoked_while_drain_blocked_sends_nothing(send_env, monkeypatch):
    controller = IncrementalDenoiseController()
    frames = [b"\x07\x08" * 480]
    session = _send_session(controller, frames, stream=_FakeStream())

    def _stuck_drain():
        while not session["cancel"]:
            time.sleep(0.02)
        controller.release_slot(session["local_id"])

    drain = threading.Thread(target=_stuck_drain, daemon=True)
    drain.start()
    session["thread"] = drain
    mw = _FakeSendMW(controller)
    panel = _SendPanel(mw, frames, session)
    vr.VoiceRecordingMixin._send_voice_message(panel, None)
    # Wait until the send thread is inside the stage-2 wait...
    deadline = time.time() + 5
    while not session.get("stop") and time.time() < deadline:
        time.sleep(0.02)
    assert session.get("stop")
    # ...then the user deletes the pending row (message_actions path).
    assert controller.revoke("m1")
    drain.join(5)
    deadline = time.time() + 5
    while time.time() < deadline and controller.is_preparing("m1"):
        time.sleep(0.05)
    assert not mw.message_queue.enqueued
    assert not controller.is_preparing("m1")
    assert session["cancel"] is True


def test_send_revoked_during_finish_helper_cleans_up_on_return(send_env, monkeypatch):
    controller = IncrementalDenoiseController()
    frames = [b"\x09\x0a" * 480]
    spool = str(send_env / "spool.pcm")
    open(spool, "wb").write(b"work")
    finish_gate = threading.Event()
    finish_started = threading.Event()
    aborted = []

    class _Worker:
        def finish(self):
            finish_started.set()
            finish_gate.wait(5)
            return spool, "neural"

        def abort(self):
            aborted.append(True)

    session = _send_session(controller, frames, worker=_Worker(),
                            spool=spool, stream=_FakeStream())
    mw = _FakeSendMW(controller)
    panel = _SendPanel(mw, frames, session)
    vr.VoiceRecordingMixin._send_voice_message(panel, None)
    assert finish_started.wait(5)
    assert controller.revoke("m1")
    time.sleep(0.4)
    # finish() is natively blocked: nothing enqueued, slot still held.
    assert not mw.message_queue.enqueued
    assert not controller.acquire_slot("other", {"cancel": False})
    # When the native call DOES return, the helper owns the cleanup:
    # result discarded, worker aborted, spool deleted, slot released.
    finish_gate.set()
    deadline = time.time() + 5
    while time.time() < deadline and os.path.exists(spool):
        time.sleep(0.05)
    assert not os.path.exists(spool)
    assert aborted
    assert controller.acquire_slot("other2", {"cancel": False})
    assert not mw.message_queue.enqueued


def test_send_finish_timeout_falls_back_and_late_result_is_discarded(send_env, monkeypatch):
    import core.audio_processing as ap
    import core.neural_noise_reduction as nnr
    monkeypatch.setattr(vr, "_INCREMENTAL_SEND_WORKER_TIMEOUT", 0.3)
    monkeypatch.setattr(ap, "apply_noise_gate",
                        lambda data, rate, ch, **kw: bytes(reversed(data)))
    monkeypatch.setattr(nnr, "reduce_voice_noise", _no_neural)
    controller = IncrementalDenoiseController()
    frames = [b"\x0b\x0c" * 480]
    raw = b"".join(frames)
    spool = str(send_env / "spool.pcm")
    open(spool, "wb").write(b"work")
    finish_gate = threading.Event()

    class _Worker:
        def finish(self):
            finish_gate.wait(5)
            return spool, "neural"

        def abort(self):
            pass

    session = _send_session(controller, frames, worker=_Worker(),
                            spool=spool, stream=_FakeStream())
    mw = _FakeSendMW(controller)
    panel = _SendPanel(mw, frames, session)
    vr.VoiceRecordingMixin._send_voice_message(panel, None)
    assert mw.message_queue.event.wait(10)
    pm = mw.message_queue.enqueued[0]
    assert _wav_payload(pm.audio_path) == bytes(reversed(raw))
    assert session["fallback_chosen"] is True
    # The late finish result is cleaned up by the helper when it returns.
    finish_gate.set()
    deadline = time.time() + 5
    while time.time() < deadline and os.path.exists(spool):
        time.sleep(0.05)
    assert not os.path.exists(spool)
    assert controller.acquire_slot("other", {"cancel": False})
