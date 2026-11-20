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
# setdefault only wins when nothing imported the real wx first. On a Windows
# CI shard another test module imports the installed wx earlier, the real wx
# stays in sys.modules, the module under test binds it, and wx.CallAfter then
# fails with "No wx.App created yet". The autouse fixture below forces the
# recorder onto whichever wx object the module under test actually holds, and
# monkeypatch restores it afterwards.
sys.modules.setdefault("wx", wx_stub)
_sound_lib = types.ModuleType("sound_lib")
_sound_lib.stream = types.ModuleType("sound_lib.stream")
sys.modules.setdefault("sound_lib", _sound_lib)
sys.modules.setdefault("sound_lib.stream", _sound_lib.stream)

from core.incremental_denoise import (  # noqa: E402
    CANCELLED, IncrementalDenoiseController,
)
from ui.conversation_panel import voice_recording as vr  # noqa: E402
from ui.conversation_panel import voice_incremental_session as vis  # noqa: E402
from ui.conversation_panel import voice_nr_send as vns  # noqa: E402
from ui.conversation_panel import voice_nr_pending as vnp  # noqa: E402
from core import incremental_send as isend  # noqa: E402


@pytest.fixture(autouse=True)
def _record_wx_calls(monkeypatch):
    """Route the module under test's wx dispatch into WX_CALLS and restore it.

    Patching the attribute on ``vr.wx`` works whichever wx the module bound
    at import time: the stub above (no wx installed) or the real wx (Windows
    CI shard, where setdefault lost to an earlier import).
    """
    monkeypatch.setattr(vr.wx, "CallAfter",
                        lambda fn, *a, **k: WX_CALLS.append((fn, a, k)))
    monkeypatch.setattr(vr.wx, "CallLater",
                        lambda ms, fn, *a, **k: WX_CALLS.append((fn, a, k)))
    yield
    WX_CALLS.clear()


class _FakeMainWindow:
    def __init__(self, nr_enabled=True, with_controller=True):
        self.settings = {"general": {"noise_reduction_enabled": nr_enabled,
                                     "voice_nr_engine": "neural",
                                     "voice_nr_moment": "record"}}
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
            lambda session: vis.VoiceIncrementalSessionMixin._incremental_drain(self, session))
        self._close_stream_handle = vr.VoiceRecordingMixin._close_stream_handle


def _start(panel, frames=None):
    vis.VoiceIncrementalSessionMixin._start_incremental_denoise(
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
    vis.VoiceIncrementalSessionMixin._cancel_incremental_denoise(panel)
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
    return vns.VoiceNrSendMixin._reduce_for_send(
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
    vis.VoiceIncrementalSessionMixin._cancel_incremental_denoise(panel)
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
        lambda: vis.VoiceIncrementalSessionMixin._cancel_incremental_denoise(panel))
    panel._nr_invalidate_attempts = (
        lambda: vnp.VoiceNrPendingMixin._nr_invalidate_attempts(panel))
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
        self.cancelled = []

    def enqueue(self, pm):
        self.enqueued.append(pm)
        self.event.set()

    def cancel(self, local_id):
        self.cancelled.append(local_id)
        return False


class _FakeSendMW:
    def __init__(self, controller):
        self.settings = {"general": {"noise_reduction_enabled": True,
                                     "voice_nr_engine": "neural",
                                     "voice_nr_moment": "record"}}
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
        self._nr_pending_row_alive = (
            lambda local_id: vnp.VoiceNrPendingMixin._nr_pending_row_alive(self, local_id))

    def _stop_recording_stream(self, session=None):
        return vr.VoiceRecordingMixin._stop_recording_stream(self, session)

    def _reduce_for_send(self, *a, **k):
        return vns.VoiceNrSendMixin._reduce_for_send(self, *a, **k)

    def _reduce_or_keep_pending(self, *a, **k):
        return vns.VoiceNrSendMixin._reduce_or_keep_pending(self, *a, **k)

    def _keep_note_pending(self, *a, **k):
        return vns.VoiceNrSendMixin._keep_note_pending(self, *a, **k)

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
    for _module in (vr, vnp):
        monkeypatch.setattr(_module, "append_message_row", lambda panel, row: None)
    monkeypatch.setattr(vr, "message_row_count", lambda panel: 1)
    for _module in (vr, vns, vnp):
        monkeypatch.setattr(_module, "data_path", lambda name: str(tmp_path / name))
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


def _cleanup_signal(controller):
    """Wrap release_slot with an Event set when the LAST cleanup step (the
    slot release) completes. Tests must wait on THIS, never on an
    intermediate step: abort and unlink run BEFORE the release, so seeing
    an abort registered or the spool gone does not prove the cleanup
    finished."""
    done = threading.Event()
    real_release = controller.release_slot

    def _release(local_id):
        real_release(local_id)
        done.set()

    controller.release_slot = _release
    return done


def _forget_signal(controller):
    """Wrap forget with an Event set when the send thread reaches its
    TERMINAL forget. is_preparing() proves nothing here: a revoke already
    moved the entry to CANCELLED, so only the real forget() call shows
    the send thread actually returned (e.g. without waiting on a drain
    whose exit the test still gates)."""
    done = threading.Event()
    real_forget = controller.forget

    def _forget(local_id):
        real_forget(local_id)
        done.set()

    controller.forget = _forget
    return done


def _wav_payload(path):
    with wave.open(path, "rb") as wf:
        return wf.readframes(wf.getnframes())


def _no_neural(*a, **k):
    raise AssertionError("reduce_voice_noise must not run for this note")


def test_send_blocked_drain_falls_back_to_basic_with_all_bytes(send_env, monkeypatch):
    import core.audio_processing as ap
    import core.neural_noise_reduction as nnr
    monkeypatch.setattr(isend, "_INCREMENTAL_SEND_FINAL_WAIT", 0.3)
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
        # A natively WEDGED feed: the stop flag cannot be observed until
        # the native call returns — here modelled as never observing stop
        # at all. cancel (set when the send abandons) is what unblocks it,
        # then the drain's own exit contract: spool cleanup + slot release.
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
    # The finisher owns the abandoned result: spool deleted, slot released.
    deadline = time.time() + 5
    while time.time() < deadline and os.path.exists(spool):
        time.sleep(0.05)
    assert not os.path.exists(spool)
    assert controller.acquire_slot("later", {"cancel": False})
    controller.release_slot("later")
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
    cleanup_done = _cleanup_signal(controller)
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
    # result discarded, worker aborted EXACTLY ONCE, spool deleted,
    # slot released — the send side observed not-ready and cleaned
    # nothing (order A of the finish-lock linearization).
    finish_gate.set()
    assert cleanup_done.wait(5), "helper cleanup never finished"
    assert not os.path.exists(spool)
    assert aborted == [True]
    assert controller.acquire_slot("other2", {"cancel": False})
    controller.release_slot("other2")
    assert not mw.message_queue.enqueued


def test_send_finish_timeout_falls_back_and_late_result_is_discarded(send_env, monkeypatch):
    import core.audio_processing as ap
    import core.neural_noise_reduction as nnr
    monkeypatch.setattr(isend, "_INCREMENTAL_SEND_FINAL_WAIT", 0.3)
    monkeypatch.setattr(ap, "apply_noise_gate",
                        lambda data, rate, ch, **kw: bytes(reversed(data)))
    monkeypatch.setattr(nnr, "reduce_voice_noise", _no_neural)
    controller = IncrementalDenoiseController()
    cleanup_done = _cleanup_signal(controller)
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
    assert cleanup_done.wait(5), "helper cleanup never finished"
    assert not os.path.exists(spool)
    assert controller.acquire_slot("other", {"cancel": False})


# ── Real delete / echo handlers (H1, H2) ─────────────────────────────────────
# The REAL _on_menu_delete_message and on_new_message functions, driven with
# fake selves — same headless pattern as the send harness above.

from ui.conversation_panel import message_actions as ma  # noqa: E402
from main_window import message_events as me  # noqa: E402
import threading  # noqa: E402


class _FakeWxDialog:
    """Stands in for wx.Dialog: always confirmed (ID_OK)."""

    def __init__(self, parent, title=None, style=None):
        pass

    def ShowModal(self):
        return 5100  # wx.ID_OK

    def Destroy(self):
        pass

    def SetSizer(self, sizer):
        pass

    def Fit(self):
        pass

    def CentreOnParent(self):
        pass


class _FakeWxWidget:
    def __init__(self, *a, **k):
        self._value = False

    def SetValue(self, v):
        self._value = v

    def GetValue(self):
        return self._value

    def SetSizer(self, sizer):
        pass

    def Add(self, *a, **k):
        pass

    def AddButton(self, *a, **k):
        pass

    def Realize(self):
        pass


@pytest.fixture
def delete_wx(monkeypatch):
    """Real dialog classes on the wx stub: the stub's classes cannot be
    instantiated with args, so the delete dialog gets plain fakes."""
    monkeypatch.setattr(ma.wx, "Dialog", _FakeWxDialog)
    monkeypatch.setattr(ma.wx, "Panel", _FakeWxWidget)
    monkeypatch.setattr(ma.wx, "BoxSizer", _FakeWxWidget)
    monkeypatch.setattr(ma.wx, "RadioButton", _FakeWxWidget)
    monkeypatch.setattr(ma.wx, "StdDialogButtonSizer", _FakeWxWidget)
    monkeypatch.setattr(ma.wx, "Button", _FakeWxWidget)
    monkeypatch.setattr(ma.wx, "DEFAULT_DIALOG_STYLE", 0, raising=False)
    monkeypatch.setattr(ma.wx, "VERTICAL", 1, raising=False)
    monkeypatch.setattr(ma.wx, "ALL", 2, raising=False)
    monkeypatch.setattr(ma.wx, "LEFT", 4, raising=False)
    monkeypatch.setattr(ma.wx, "RIGHT", 8, raising=False)
    monkeypatch.setattr(ma.wx, "BOTTOM", 16, raising=False)
    monkeypatch.setattr(ma.wx, "EXPAND", 32, raising=False)
    monkeypatch.setattr(ma.wx, "RB_GROUP", 64, raising=False)
    monkeypatch.setattr(ma.wx, "ID_OK", 5100, raising=False)
    monkeypatch.setattr(ma.wx, "ID_CANCEL", 5101, raising=False)


class _DeletePanel:
    """Fake conversation panel for the REAL _on_menu_delete_message."""

    def __init__(self, controller, msg):
        self.main_window = types.SimpleNamespace(
            i18n=_FakeI18n(),
            _is_self_jid=lambda jid: False,
            _incremental_controller=controller,
        )
        self.conversation = {"remoteJid": "peer@s.whatsapp.net"}
        self._sorted_messages = [msg]
        self._outgoing_virtual_messages = {msg["_local_id"]: {}}
        self._media_upload_progress = {msg["_local_id"]: 5}
        self._upload_stages_seen = {msg["_local_id"]: {"upload"}}
        self._media_transfer_started = {msg["_local_id"]}
        self.calls = []

    def _is_separator(self, msg):
        return False

    def _is_system_event(self, msg):
        return False

    def _add_delete_media_checkbox(self, panel, sizer, msgs):
        return None

    def remove_messages_by_id(self, ids, focus_previous=False):
        self.calls.append(("remove_messages_by_id", set(ids), focus_previous))

    def _hide_media_transfer_gauge(self):
        self.calls.append(("hide_gauge",))

    def _cancel_pending_message(self, msg, local_id, hold_for_echo=True):
        self.calls.append(("cancel_pending", local_id, hold_for_echo))

    def _delete_message_for_me_only(self, *a, **k):
        self.calls.append(("for_me_only",))

    def _delete_message_for_everyone_keep_row(self, *a, **k):
        self.calls.append(("for_everyone",))

    def _confirm_local_delete(self, msgs):
        self.calls.append(("confirm_local",))
        return True, True


def _pending_voice_msg(local_id):
    return {
        "key": {"id": local_id, "fromMe": True,
                "remoteJid": "peer@s.whatsapp.net"},
        "_local_id": local_id,
        "_local_pending": True,
        "messageType": "audioMessage",
    }


def test_delete_handler_revoke_wins_removes_incremental_note(delete_wx, monkeypatch):
    """H1: real delete handler, incremental branch — the note is still held
    by the NR machinery (PREPARING), revoke() wins, plain local removal."""
    discarded = []
    monkeypatch.setattr(ma, "discard_local_media_cache",
                        lambda *a: discarded.append(a))
    monkeypatch.setattr(ma, "data_path", lambda name: "/tmp/dp/" + name)
    controller = IncrementalDenoiseController()
    session = {"revoked": False, "cancel": False}
    controller.register("LID1", session)
    msg = _pending_voice_msg("LID1")
    panel = _DeletePanel(controller, msg)

    ma.MessageActionsMixin._on_menu_delete_message(panel, 0)

    # Revocation reached the session flags atomically.
    assert session["revoked"] and session["cancel"]
    assert ("remove_messages_by_id", {"LID1"}, True) in panel.calls
    assert ("hide_gauge",) in panel.calls
    assert panel._outgoing_virtual_messages == {}
    assert panel._media_upload_progress == {}
    assert panel._upload_stages_seen == {}
    assert panel._media_transfer_started == set()
    assert discarded == [("/tmp/dp/voice_messages", "/tmp/dp/media", "LID1")]
    # NOT the queue-cancel path and NOT a normal delete.
    assert not any(c[0] in ("cancel_pending", "for_me_only", "for_everyone")
                   for c in panel.calls)


def test_delete_handler_revoke_lost_falls_to_queue_cancel(delete_wx, monkeypatch):
    """H1: same handler, but the send thread already transferred the note
    (or never registered it): revoke() is False and the existing
    _cancel_pending_message path applies, holding the record for the echo."""
    monkeypatch.setattr(ma, "discard_local_media_cache", lambda *a: None)
    monkeypatch.setattr(ma, "data_path", lambda name: "/tmp/dp/" + name)
    controller = IncrementalDenoiseController()
    msg = _pending_voice_msg("LID2")
    panel = _DeletePanel(controller, msg)

    ma.MessageActionsMixin._on_menu_delete_message(panel, 0)

    assert panel.calls == [("cancel_pending", "LID2", True)]


class _FakeDb:
    def __init__(self):
        self.inserted = []

    def insert_message(self, jid, record):
        self.inserted.append((jid, record))


class _SyncExecutor:
    def submit(self, fn):
        fn()


def _echo_window(controller, jid, records):
    db = _FakeDb()
    window = types.SimpleNamespace(
        _live_events_ready=lambda: True,
        _normalize_jid=lambda j: j,
        _is_self_jid=lambda s: False,
        my_jid="",
        my_lid="",
        _lid_mapping_lock=threading.Lock(),
        _lid_to_phone={},
        _phone_to_lid={},
        _redirect_self_chat_artifact=lambda rj, key, fm: (rj, fm),
        _extract_lid_mapping=lambda msg: None,
        _drop_protocol_edit=lambda rj, msg: False,
        _deleted_chats=set(),
        chats={jid: {"t": 0, "messages": {"messages": {"records": records}}}},
        _apply_group_subject_change=lambda *a, **k: None,
        _refresh_mention_cache_on_membership_change=lambda *a, **k: None,
        _apply_group_settings_change=lambda *a, **k: None,
        db=db,
        _msg_bg_executor=_SyncExecutor(),
        _own_sent_ids_lock=threading.Lock(),
        _own_sent_ids=set(),
        conversations_panel=types.SimpleNamespace(
            _mark_message_sent=lambda *a, **k: None),
        _schedule_save=lambda **k: None,
        _schedule_set_chats=lambda: None,
        _incremental_controller=controller,
    )
    return window, db


def _audio_echo(jid, real_id):
    return {
        "key": {"id": real_id, "fromMe": True, "remoteJid": jid},
        "messageType": "audioMessage",
        "messageTimestamp": 200,
    }


def _pending_record(local_id, jid, preparing=False):
    record = {
        "key": {"id": local_id, "fromMe": True, "remoteJid": jid},
        "_local_id": local_id,
        "_local_pending": True,
        "messageType": "audioMessage",
        "messageTimestamp": 100,
    }
    if preparing:
        record["_incremental_preparing"] = True
    return record


def test_echo_matcher_skips_preparing_record(monkeypatch):
    """H2: real on_new_message — a note still held by the NR machinery has
    no send in flight, so the echo of ANOTHER pending audio claims that one,
    never the PREPARING record."""
    monkeypatch.setattr(me, "dismiss_typing_message", lambda *a: None)
    jid = "peer@s.whatsapp.net"
    controller = IncrementalDenoiseController()
    controller.register("LID_A", {})  # PREPARING: authority for the skip
    held = _pending_record("LID_A", jid, preparing=True)
    other = _pending_record("LID_B", jid)
    window, db = _echo_window(controller, jid, [held, other])

    me.MessageEventsMixin.on_new_message(window, _audio_echo(jid, "REAL1"))

    # The echo claimed the other pending audio...
    assert other["_local_pending"] is False
    assert other["key"]["id"] == "REAL1"
    assert db.inserted == [(jid, other)]
    # ...and the held note was left completely untouched.
    assert held["key"]["id"] == "LID_A"
    assert held["_local_pending"] is True


def test_echo_matcher_claims_transferred_record_despite_stale_flag(monkeypatch):
    """H2: same matcher — after the real hand-off the record may still carry
    a stale _incremental_preparing flag; the controller's state is the
    authority, so the transferred note DOES claim its echo."""
    monkeypatch.setattr(me, "dismiss_typing_message", lambda *a: None)
    jid = "peer@s.whatsapp.net"
    controller = IncrementalDenoiseController()
    controller.register("LID_A", {})
    assert controller.transfer("LID_A")  # real hand-off: state consumed
    record = _pending_record("LID_A", jid, preparing=True)  # stale flag
    window, db = _echo_window(controller, jid, [record])

    me.MessageEventsMixin.on_new_message(window, _audio_echo(jid, "REAL2"))

    assert record["_local_pending"] is False
    assert record["key"]["id"] == "REAL2"
    assert db.inserted == [(jid, record)]


def test_send_finish_completed_then_revoke_send_consumes_once(send_env, monkeypatch):
    """Boundary, order B: finish() completes and the send CONSUMES the
    result strictly BEFORE the revoke lands (deterministic barrier: the
    test waits for the counted spool unlink, which only the consume block
    performs, after the finisher stored the ready result under
    finish_lock).
    The finisher's flag check linearizes first (no flags, no cleanup);
    the send consumes the spool exactly once; the later revoke beats only
    the transfer, and nobody cleans up a second time."""
    unlinked = []
    real_unlink = os.unlink

    def _counting_unlink(path):
        unlinked.append(path)
        return real_unlink(path)

    monkeypatch.setattr(os, "unlink", _counting_unlink)
    # Park the send between the consume and the transfer: the revoke below
    # is then guaranteed to land while the send still owns the message,
    # before the enqueue decision — no wall-clock race against the WAV
    # write and transfer path. (mono send: ogg conversion precedes the
    # transfer; the fake's method is wrapped, no production code touched.)
    wav_gate = threading.Event()
    wav_reached = threading.Event()
    controller = IncrementalDenoiseController()
    frames = [b"\x0d\x0e" * 480]
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
    real_convert = mw._convert_wav_to_ogg

    def _gated_convert(*args, **kwargs):
        wav_reached.set()
        wav_gate.wait(5)
        return real_convert(*args, **kwargs)

    mw._convert_wav_to_ogg = _gated_convert
    panel = _SendPanel(mw, frames, session)
    vr.VoiceRecordingMixin._send_voice_message(panel, None)
    assert finish_started.wait(5)
    finish_gate.set()
    # Wait until the send has actually consumed the finished result: the
    # consume block deletes the spool (counted by the unlink wrapper) and
    # runs only after the finisher stored the ready result under
    # finish_lock.
    deadline = time.time() + 6
    while time.time() < deadline and spool not in unlinked:
        time.sleep(0.02)
    # The consume deleted the spool exactly once, before any revoke:
    assert unlinked.count(spool) == 1
    # Revoke AFTER consumption but BEFORE the transfer (the send is parked
    # at the gated WAV step): revoke() reports the message, the in-flight
    # transfer refuses the cancelled enqueue, and nobody cleans up again.
    assert wav_reached.wait(5)
    assert controller.revoke("m1")
    wav_gate.set()
    deadline = time.time() + 6
    while time.time() < deadline and controller.is_preparing("m1"):
        time.sleep(0.05)
    time.sleep(0.2)  # let the send thread finish its transfer-fail path
    assert not controller.is_preparing("m1")
    assert not mw.message_queue.enqueued
    # The finisher never aborted the worker (it completed before the
    # revoke) and the spool was deleted exactly once, by the send.
    assert aborted == []
    assert unlinked.count(spool) == 1
    assert not os.path.exists(spool)
    # The slot was released exactly into a free state.
    assert controller.acquire_slot("probe", {"cancel": False})
    controller.release_slot("probe")


class _FakePAStream:
    def __init__(self):
        self.closed = False

    def start_stream(self):
        pass

    def stop_stream(self):
        pass

    def close(self):
        self.closed = True


class _FakePA:
    """pa.open() blocks until the test releases the gate: a slow open."""

    def __init__(self):
        self.gate = threading.Event()
        self.open_entered = threading.Event()
        self.streams = []

    def open(self, **kw):
        self.open_entered.set()
        self.gate.wait(10)
        stream = _FakePAStream()
        self.streams.append(stream)
        return stream


class _RecPanel:
    """Fake conversation panel for the REAL _start_voice_recording."""

    def __init__(self, controller):
        self.main_window = types.SimpleNamespace(
            settings={"general": {"noise_reduction_enabled": True,
                                  "voice_nr_engine": "neural",
                                  "voice_nr_moment": "record"}},
            _incremental_controller=controller,
            i18n=_FakeI18n(),
            voicemsg_startrecording_sound=types.SimpleNamespace(play=lambda: None),
            send_recording_status=lambda *a: None,
            output=lambda *a: None,
        )
        self.conversation = {"remoteJid": "peer@s.whatsapp.net"}
        self._recording_pa = None
        self._recording_open_token = 0
        self._incremental_session = None
        self._recording_channel_mode = "auto"
        self._recording_system_audio = False
        self._recording_stream = None
        self._sd_stream = None
        self._recording_paused = False
        self._is_recording = False
        noop = lambda *a, **k: None
        self._voice_recording_focus_suppression_enabled = lambda: False
        self._focus_recording_button_silently = noop
        self.send_message_btn = types.SimpleNamespace(Hide=noop)
        self.record_voice_message_btn = types.SimpleNamespace(Hide=noop)
        self._add_attachment_btn = types.SimpleNamespace(Hide=noop)
        self._pause_resume_btn = types.SimpleNamespace(SetLabel=noop)
        self._voice_panel = types.SimpleNamespace(Show=noop)
        self.conversation_panel = types.SimpleNamespace(Layout=noop)
        self.message_field = types.SimpleNamespace(Hide=noop)
        self._send_voice_btn = object()
        self._discard_voice_btn = object()
        self._incremental_drain = (
            lambda session: vis.VoiceIncrementalSessionMixin._incremental_drain(self, session))
        self._close_stream_handle = vr.VoiceRecordingMixin._close_stream_handle
        self._start_incremental_denoise = (
            lambda frames, rate, ch: vis.VoiceIncrementalSessionMixin._start_incremental_denoise(
                self, frames, rate, ch))


def _patch_recording_env(monkeypatch):
    monkeypatch.setattr(vr, "refuse_in_channel", lambda self: False)
    monkeypatch.setattr(vr, "stop_typing_for_capture", lambda mw: None)
    monkeypatch.setattr(vr, "voice_recording_mode", lambda settings: "auto")
    monkeypatch.setattr(vr, "recording_configs_for",
                        lambda *a, **k: [(48000, 1)])


def _wait_stream_opened_call(timeout=5):
    """The recorded wx.CallAfter(_on_stream_opened, stream, rate, ch, fb)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for i, (fn, args, k) in enumerate(WX_CALLS):
            if len(args) == 4:
                return WX_CALLS.pop(i)
        time.sleep(0.02)
    raise AssertionError("_on_stream_opened was never scheduled")


def test_start_pyaudio_delayed_open_stream_goes_to_the_new_session(monkeypatch):
    """REAL _start_voice_recording, PyAudio path, slow open: the stream must
    land on the session THIS recording created — never on a stale session
    left over from a previous one, whose close was never confirmed."""
    _patch_recording_env(monkeypatch)
    controller = IncrementalDenoiseController()
    # Occupy the neural slot: both notes go basic-only, no drain threads.
    assert controller.acquire_slot("other", {"cancel": False})
    pa = _FakePA()
    monkeypatch.setattr(vr, "pyaudio", types.SimpleNamespace(
        PyAudio=lambda: pa, paInt16=8, paContinue=0))
    panel = _RecPanel(controller)
    panel._recording_pa = pa
    # Stale session from a previous recording: cancelled, close NEVER
    # confirmed (stopped_event unset) — the shape that used to steal the
    # next recording's stream handle.
    stale = {"local_id": "old1", "stopped_event": threading.Event(),
             "cancel": True}
    panel._incremental_session = stale
    WX_CALLS.clear()
    vr.VoiceRecordingMixin._start_voice_recording(panel)
    assert pa.open_entered.wait(5), "background open never started"
    pa.gate.set()  # the slow open completes now
    fn, args, _ = _wait_stream_opened_call()
    fn(*args)  # the real _on_stream_opened, on our "UI thread"
    session = panel._incremental_session
    assert session is not None and session is not stale
    assert session["stream"] is pa.streams[0]
    assert "stream" not in stale
    # And the real stop path now closes the session's OWN stream.
    vr.VoiceRecordingMixin._stop_recording_stream(panel, session)
    assert pa.streams[0].closed
    assert session["stopped_event"].is_set()
    controller.discard(session["local_id"])
    controller.discard("old1")
    controller.release_slot("other")


def test_send_during_model_load_goes_basic_without_second_engine(send_env, monkeypatch):
    """Milo 17:16 priority: send while the model constructor is still
    blocked. The send must take the existing basic gate — never spin up a
    SECOND ONNX instance via reduce_voice_noise — and the late-returning
    constructor's drain must abort and self-clean (spool + slot)."""
    import core.audio_processing as ap
    from core import neural_noise_reduction as nnr
    import core.incremental_denoise as inc
    monkeypatch.setattr(ap, "apply_noise_gate",
                        lambda data, rate, ch, **kw: bytes(reversed(data)))
    monkeypatch.setattr(nnr, "reduce_voice_noise", _no_neural)
    ctor_barrier = threading.Event()
    ctor_calls = []

    class _FakeDenoiser:
        pass

    def _held_ctor(*a, **k):
        ctor_calls.append(1)
        ctor_barrier.wait(10)
        return _FakeDenoiser()

    monkeypatch.setattr(nnr, "NeuralDenoiser", _held_ctor)
    monkeypatch.setattr(nnr, "model_path", lambda: __file__)  # exists
    aborted = []

    class _FakeWorker:
        def finish(self):
            raise AssertionError("finish must not run for an abandoned load")

        def abort(self):
            aborted.append(True)

    def _fake_worker_factory(rate, ch, denoiser, spool_path=None):
        return _FakeWorker()

    monkeypatch.setattr(inc, "IncrementalDenoiser", _fake_worker_factory)

    def _fake_drain_loop(session, worker, transform=None):
        # The real loop observes cancel cooperatively; on cancel it
        # aborts the worker, deletes the spool and marks the cleanup as
        # drain-owned (documented contract).
        while not session["cancel"] and not session["stop"]:
            time.sleep(0.01)
        if session["cancel"]:
            worker.abort()
            try:
                os.unlink(session["spool"])
            except OSError:
                pass
            session["drain_cleaned"] = True

    monkeypatch.setattr(inc, "drain_capture_frames", _fake_drain_loop)

    controller = IncrementalDenoiseController()
    cleanup_done = _cleanup_signal(controller)
    mw = _FakeSendMW(controller)
    frames = [b"\x01\x02" * 480, b"\x03\x04" * 480]
    raw = b"".join(frames)
    panel = _SendPanel(mw, frames, None)
    panel._incremental_drain = (
        lambda session: vis.VoiceIncrementalSessionMixin._incremental_drain(panel, session))
    # Real start: takes the slot, spawns the REAL drain — whose model
    # constructor is now held at the barrier.
    vis.VoiceIncrementalSessionMixin._start_incremental_denoise(panel, frames, 48000, 1)
    session = panel._incremental_session
    assert session is not None and not session["basic_only"]
    deadline = time.time() + 5
    while not ctor_calls and time.time() < deadline:
        time.sleep(0.02)
    assert ctor_calls == [1]

    vr.VoiceRecordingMixin._send_voice_message(panel, None)
    assert mw.message_queue.event.wait(10), "send thread never enqueued"
    pm = mw.message_queue.enqueued[0]
    # Basic gate on the complete raw; NO second engine was constructed.
    assert _wav_payload(pm.audio_path) == bytes(reversed(raw))
    assert session["fallback_chosen"] is True
    assert ctor_calls == [1]

    # The late constructor return: the drain aborts and self-cleans.
    ctor_barrier.set()
    # Wait the cleanup COMPLETION signal (slot release is the last step),
    # not an intermediate one.
    assert cleanup_done.wait(5), "drain self-cleanup never finished"
    assert aborted == [True]
    assert not os.path.exists(session["spool"])
    assert controller.acquire_slot("probe", {"cancel": False})
    controller.release_slot("probe")


def test_send_constructor_finishes_between_snapshot_and_liveness(send_env, monkeypatch):
    """Milo 17:32 frontier 1, exact interleave: the constructor publishes
    the worker and the drain EXITS between the send's worker snapshot
    (None) and the aliveness check. The send must still take the basic
    gate — never a second offline ONNX — and must clean the ownerless
    late worker/spool/slot exactly once (the drain is dead, so it cannot).
    """
    import core.audio_processing as ap
    from core import neural_noise_reduction as nnr
    monkeypatch.setattr(ap, "apply_noise_gate",
                        lambda data, rate, ch, **kw: bytes(reversed(data)))
    monkeypatch.setattr(nnr, "reduce_voice_noise", _no_neural)
    controller = IncrementalDenoiseController()
    frames = [b"\x01\x02" * 480, b"\x03\x04" * 480]
    raw = b"".join(frames)
    spool = str(send_env / "spool.pcm")
    open(spool, "wb").write(b"work")
    unlinked = []
    real_unlink = os.unlink

    def _counting_unlink(path):
        unlinked.append(path)
        return real_unlink(path)

    monkeypatch.setattr(os, "unlink", _counting_unlink)
    aborted = []

    class _LateWorker:
        def finish(self):
            raise AssertionError(
                "finish must not run: the send holds no worker reference")

        def abort(self):
            aborted.append(True)

    holder = {}

    class _DeadAfterPublishThread:
        # Deterministic interleave: the FIRST is_alive() call publishes
        # the late worker (what the real drain did before exiting) and
        # reports the thread as already dead.
        def is_alive(self):
            holder["session"]["worker"] = _LateWorker()
            return False

    session = _send_session(controller, frames, worker=None, spool=spool,
                            stream=_FakeStream(),
                            thread=_DeadAfterPublishThread())
    holder["session"] = session
    mw = _FakeSendMW(controller)
    panel = _SendPanel(mw, frames, session)
    vr.VoiceRecordingMixin._send_voice_message(panel, None)
    assert mw.message_queue.event.wait(10), "send thread never enqueued"
    pm = mw.message_queue.enqueued[0]
    # Basic gate on the complete raw; NO offline neural engine.
    assert _wav_payload(pm.audio_path) == bytes(reversed(raw))
    assert session["fallback_chosen"] is True
    # The late worker was cleaned exactly once, by the send...
    assert aborted == [True]
    assert session["worker"] is None
    assert unlinked.count(spool) == 1
    assert not os.path.exists(spool)
    # ...and the slot is free for the next note.
    assert controller.acquire_slot("probe", {"cancel": False})
    controller.release_slot("probe")


def test_start_temp_file_failure_releases_slot_for_next_capture(send_env, monkeypatch):
    """Milo 17:32 frontier 2: a temp-file failure AFTER acquire_slot must
    not leak the slot or the send-authority entry — the next capture
    starts a full incremental session."""
    controller = IncrementalDenoiseController()
    mw = _FakeSendMW(controller)
    frames = [b"\x01\x02" * 480]
    panel = _SendPanel(mw, frames, None)
    panel._incremental_drain = (
        lambda session: vis.VoiceIncrementalSessionMixin._incremental_drain(panel, session))
    real_ntf = vr.tempfile.NamedTemporaryFile

    def _raise_temp(*args, **kwargs):
        raise OSError("simulated disk/permission failure")

    monkeypatch.setattr(vr.tempfile, "NamedTemporaryFile", _raise_temp)
    vis.VoiceIncrementalSessionMixin._start_incremental_denoise(panel, frames, 48000, 1)
    # Failure logged and contained: no session published...
    assert panel._incremental_session is None
    # ...the slot is free...
    assert controller.acquire_slot("probe", {"cancel": False})
    controller.release_slot("probe")
    # ...and the NEXT capture takes a full (non-basic) session.
    monkeypatch.setattr(vr.tempfile, "NamedTemporaryFile", real_ntf)
    vis.VoiceIncrementalSessionMixin._start_incremental_denoise(panel, frames, 48000, 1)
    session = panel._incremental_session
    assert session is not None and not session["basic_only"]
    # Cleanup: stop the real drain (no model file here -> it fails and
    # self-cleans; cancel covers the model-present case too).
    session["cancel"] = True
    thread = session.get("thread")
    if thread is not None:
        thread.join(5)


def test_send_drain_fails_during_finish_join_never_touches_worker(send_env, monkeypatch):
    """Milo 17:32 helper revalidation: the drain fails (and self-cleans
    per its contract) WHILE the finisher is blocked in drain.join(). The
    finisher must re-validate AFTER the join: no finish() on the aborted
    worker, and the send must not abort/unlink/release again — the note
    falls to the basic gate."""
    import core.audio_processing as ap
    from core import neural_noise_reduction as nnr
    monkeypatch.setattr(ap, "apply_noise_gate",
                        lambda data, rate, ch, **kw: bytes(reversed(data)))
    # The failed drain already cleaned its OWN instance, so the published
    # offline policy (neural retry) applies — same as a feed failure
    # discovered before the send. The fake marks the engine used.
    monkeypatch.setattr(nnr, "reduce_voice_noise",
                        lambda data, rate, ch: (bytes(reversed(data)), "neural"))
    controller = IncrementalDenoiseController()
    frames = [b"\x01\x02" * 480, b"\x03\x04" * 480]
    raw = b"".join(frames)
    spool = str(send_env / "spool.pcm")
    open(spool, "wb").write(b"work")
    unlinked = []
    real_unlink = os.unlink

    def _counting_unlink(path):
        unlinked.append(path)
        return real_unlink(path)

    monkeypatch.setattr(os, "unlink", _counting_unlink)
    finish_calls = []
    aborted = []

    class _Worker:
        def finish(self):
            finish_calls.append(1)
            return spool, "neural"

        def abort(self):
            aborted.append(True)

    class _FailUnderJoinThread:
        # Emulates a drain that dies mid-join: its own failure path marks
        # the session failed and releases the slot (its contract also
        # covers abort/spool, irrelevant here — the point is the SEND
        # must not touch any of it).
        def __init__(self, session, controller):
            self._session = session
            self._controller = controller

        def is_alive(self):
            return True

        def join(self):
            self._session["failed"] = True
            # The real drain's failure path aborts the worker, deletes the
            # spool and marks the cleanup as drain-owned (drain_cleaned);
            # the wiring's finally then releases the slot. The fake
            # collapses that into the join: the point stands — the SEND
            # must not touch any of it.
            self._session["drain_cleaned"] = True
            self._controller.release_slot("m1")

    worker = _Worker()
    session = _send_session(controller, frames, worker=worker, spool=spool,
                            stream=_FakeStream())
    session["thread"] = _FailUnderJoinThread(session, controller)
    mw = _FakeSendMW(controller)
    panel = _SendPanel(mw, frames, session)
    vr.VoiceRecordingMixin._send_voice_message(panel, None)
    assert mw.message_queue.event.wait(10), "send thread never enqueued"
    pm = mw.message_queue.enqueued[0]
    # Offline path on the complete raw (fake neural marks the bytes).
    assert _wav_payload(pm.audio_path) == bytes(reversed(raw))
    # finish() never ran on the failed drain's worker...
    assert finish_calls == []
    # ...and the send never aborted the worker, never deleted the spool,
    # never double-released the slot — the drain owns that cleanup.
    assert aborted == []
    assert spool not in unlinked
    assert controller.acquire_slot("probe", {"cancel": False})
    controller.release_slot("probe")



def test_send_cancel_before_dead_drain_check_cleans_remnant_once(send_env, monkeypatch):
    """Milo 17:43 window A: worker-None frontier, drain DEAD at the
    aliveness check, and a cancel lands between the send's worker
    snapshot and the dead-drain cleanup check (a revoke/user-cancel
    interleave). The old `not cancel` guard skipped the cleanup and no
    reaper exists for a dead drain: the late worker/spool/slot leaked.
    Cleanup authority must key on drain_cleaned (what the drain ACTUALLY
    did), not on the cancel flag: the remnant is cleaned exactly once
    and the basic gate is still enqueued."""
    import core.audio_processing as ap
    from core import neural_noise_reduction as nnr
    monkeypatch.setattr(ap, "apply_noise_gate",
                        lambda data, rate, ch, **kw: bytes(reversed(data)))
    monkeypatch.setattr(nnr, "reduce_voice_noise", _no_neural)
    controller = IncrementalDenoiseController()
    cleanup_done = _cleanup_signal(controller)
    frames = [b"\x01\x02" * 480, b"\x03\x04" * 480]
    raw = b"".join(frames)
    spool = str(send_env / "spool.pcm")
    open(spool, "wb").write(b"work")
    unlinked = []
    real_unlink = os.unlink

    def _counting_unlink(path):
        unlinked.append(path)
        return real_unlink(path)

    monkeypatch.setattr(os, "unlink", _counting_unlink)
    aborted = []

    class _LateWorker:
        def finish(self):
            raise AssertionError(
                "finish must not run: the send holds no worker reference")

        def abort(self):
            aborted.append(True)

    holder = {}

    class _CancelBeforeDeadCheckThread:
        # Deterministic interleave: at the liveness check the drain is
        # already dead, it published a worker it never cleaned (no
        # drain_cleaned), and a cancel already landed.
        def is_alive(self):
            holder["session"]["worker"] = _LateWorker()
            holder["session"]["cancel"] = True
            return False

    session = _send_session(controller, frames, worker=None, spool=spool,
                            stream=_FakeStream(),
                            thread=_CancelBeforeDeadCheckThread())
    holder["session"] = session
    mw = _FakeSendMW(controller)
    panel = _SendPanel(mw, frames, session)
    vr.VoiceRecordingMixin._send_voice_message(panel, None)
    assert mw.message_queue.event.wait(10), "send thread never enqueued"
    pm = mw.message_queue.enqueued[0]
    # Basic gate on the complete raw; NO offline neural engine.
    assert _wav_payload(pm.audio_path) == bytes(reversed(raw))
    assert session["fallback_chosen"] is True
    assert session["cancel"] is True
    # The cleanup is synchronous on this branch: done before the enqueue.
    assert cleanup_done.is_set()
    assert aborted == [True]
    assert session["worker"] is None
    assert unlinked.count(spool) == 1
    assert not os.path.exists(spool)
    # ...and the slot is free for the next note.
    assert controller.acquire_slot("probe", {"cancel": False})
    controller.release_slot("probe")


def test_send_finish_timeout_drain_normal_exit_reclaims_remnant_once(send_env, monkeypatch):
    """Milo 17:43 window B: a worker EXISTS at send and the finisher is
    blocked in drain.join() while the drain keeps running. The bounded
    wait times out (basic gate chosen, cancel set) BEFORE the helper
    resumes, and the drain then exits through its NORMAL stop path
    without re-reading cancel and WITHOUT cleaning (no drain_cleaned).
    The helper must not presume the drain cleaned: it reclaims the
    remnant exactly once (abort + spool + slot), never calls finish(),
    and the basic audio the send already enqueued stands."""
    import core.audio_processing as ap
    from core import neural_noise_reduction as nnr
    monkeypatch.setattr(isend, "_INCREMENTAL_SEND_FINAL_WAIT", 0.3)
    monkeypatch.setattr(ap, "apply_noise_gate",
                        lambda data, rate, ch, **kw: bytes(reversed(data)))
    monkeypatch.setattr(nnr, "reduce_voice_noise", _no_neural)
    controller = IncrementalDenoiseController()
    cleanup_done = _cleanup_signal(controller)
    frames = [b"\x0b\x0c" * 480]
    raw = b"".join(frames)
    spool = str(send_env / "spool.pcm")
    open(spool, "wb").write(b"work")
    unlinked = []
    real_unlink = os.unlink

    def _counting_unlink(path):
        unlinked.append(path)
        return real_unlink(path)

    monkeypatch.setattr(os, "unlink", _counting_unlink)
    finish_calls = []
    aborted = []

    class _Worker:
        def finish(self):
            finish_calls.append(1)
            return spool, "neural"

        def abort(self):
            aborted.append(True)

    drain_exit_gate = threading.Event()

    class _NormalExitDrainThread:
        # Alive while the send snapshots; the join blocks until the test
        # lets the drain "exit" — through its normal path: no cleanup, no
        # drain_cleaned, worker+spool+slot kept for the send.
        def is_alive(self):
            return True

        def join(self):
            drain_exit_gate.wait(10)

    session = _send_session(controller, frames, worker=_Worker(), spool=spool,
                            stream=_FakeStream(),
                            thread=_NormalExitDrainThread())
    mw = _FakeSendMW(controller)
    panel = _SendPanel(mw, frames, session)
    vr.VoiceRecordingMixin._send_voice_message(panel, None)
    # The bounded wait times out under the blocked join: basic gate,
    # enqueued WITHOUT waiting for the drain.
    assert mw.message_queue.event.wait(10), "send thread never enqueued"
    pm = mw.message_queue.enqueued[0]
    assert _wav_payload(pm.audio_path) == bytes(reversed(raw))
    assert session["fallback_chosen"] is True
    assert session["cancel"] is True
    # The drain is still "exiting": nobody cleaned, finish() never ran.
    assert finish_calls == []
    assert aborted == []
    # The drain exits NORMALLY without re-reading cancel: the helper
    # resumes post-join, sees superseded BUT drain_cleaned absent, and
    # reclaims the remnant exactly once. Wait for the cleanup COMPLETION
    # signal, not the abort (which precedes unlink/release).
    drain_exit_gate.set()
    assert cleanup_done.wait(5), "helper never released the slot"
    assert finish_calls == []
    assert aborted == [True]
    assert unlinked.count(spool) == 1
    assert not os.path.exists(spool)
    assert controller.acquire_slot("probe", {"cancel": False})
    controller.release_slot("probe")


def test_send_cancel_unread_by_exiting_drain_reaper_cleans_once(send_env, monkeypatch):
    """Milo 17:39 window, opposite edge of the is_alive=False test: the
    drain is ALIVE when the send snapshots worker None, the send cancels
    and goes basic, but the drain exits through its normal path WITHOUT
    re-reading cancel — no self-cleanup. A reaper must join the drain off
    the send path (the send enqueues without waiting) and clean the late
    worker/spool/slot exactly once."""
    import core.audio_processing as ap
    from core import neural_noise_reduction as nnr
    monkeypatch.setattr(ap, "apply_noise_gate",
                        lambda data, rate, ch, **kw: bytes(reversed(data)))
    monkeypatch.setattr(nnr, "reduce_voice_noise", _no_neural)
    controller = IncrementalDenoiseController()
    cleanup_done = _cleanup_signal(controller)
    frames = [b"\x01\x02" * 480, b"\x03\x04" * 480]
    raw = b"".join(frames)
    spool = str(send_env / "spool.pcm")
    open(spool, "wb").write(b"work")
    unlinked = []
    real_unlink = os.unlink

    def _counting_unlink(path):
        unlinked.append(path)
        return real_unlink(path)

    monkeypatch.setattr(os, "unlink", _counting_unlink)
    aborted = []

    class _Worker:
        def finish(self):
            raise AssertionError("finish must not run for an abandoned session")

        def abort(self):
            aborted.append(True)

    exit_gate = threading.Event()

    def _fake_drain(session):
        # Still "in native work" while the send snapshots (worker None) and
        # cancels; publishes the late worker only at the very end and exits
        # WITHOUT re-reading cancel and WITHOUT cleaning — Milo's window.
        exit_gate.wait(10)
        session["worker"] = _Worker()

    session = _send_session(controller, frames, worker=None, spool=spool,
                            stream=_FakeStream())
    drain = threading.Thread(target=_fake_drain, args=(session,), daemon=True)
    session["thread"] = drain
    drain.start()
    mw = _FakeSendMW(controller)
    panel = _SendPanel(mw, frames, session)
    vr.VoiceRecordingMixin._send_voice_message(panel, None)
    # The send does NOT wait for the drain: basic gate enqueued already.
    assert mw.message_queue.event.wait(10), "send thread never enqueued"
    pm = mw.message_queue.enqueued[0]
    assert _wav_payload(pm.audio_path) == bytes(reversed(raw))
    assert session["fallback_chosen"] is True
    assert session["cancel"] is True
    # The drain is still parked in its native work: nobody cleaned yet.
    assert aborted == []
    # Now the drain exits without re-reading cancel: the reaper (which was
    # joined on it) claims the remainder exactly once. Wait for the
    # cleanup COMPLETION signal — abort is registered BEFORE unlink and
    # the slot release, so seeing it is not proof the cleanup finished.
    exit_gate.set()
    assert cleanup_done.wait(5), "reaper cleanup never finished"
    assert aborted == [True]
    assert session["worker"] is None
    assert unlinked.count(spool) == 1
    assert not os.path.exists(spool)
    # The next capture acquires the slot.
    assert controller.acquire_slot("probe", {"cancel": False})
    controller.release_slot("probe")


def test_send_revoke_before_finisher_gate_finalizer_cleans_once(send_env, monkeypatch):
    """Milo 17:50 window: a worker EXISTS at send and the user's revoke
    lands between the confirmed device close and the finisher-creation
    gate (cancel True AT the gate). The old `not cancel` in the gate
    skipped the helper, and no reaper exists for a session holding its
    own worker: the drain exits through its normal stop path without
    re-reading cancel (no drain_cleaned) and worker/spool/slot leak.
    Every own-worker session that reached the drain stop now keeps a
    FINALIZER: the helper joins the drain, never calls finish(), and
    reclaims the remnant exactly once — 0 enqueue, 0 finish, 1 abort,
    1 spool unlink, slot free after the cleanup-COMPLETION signal."""
    import core.audio_processing as ap
    from core import neural_noise_reduction as nnr
    monkeypatch.setattr(ap, "apply_noise_gate",
                        lambda data, rate, ch, **kw: bytes(reversed(data)))
    monkeypatch.setattr(nnr, "reduce_voice_noise", _no_neural)
    controller = IncrementalDenoiseController()
    cleanup_done = _cleanup_signal(controller)
    send_returned = _forget_signal(controller)
    frames = [b"\x0e\x0f" * 480]
    spool = str(send_env / "spool.pcm")
    open(spool, "wb").write(b"work")
    unlinked = []
    real_unlink = os.unlink

    def _counting_unlink(path):
        unlinked.append(path)
        return real_unlink(path)

    monkeypatch.setattr(os, "unlink", _counting_unlink)
    finish_calls = []
    aborted = []

    class _Worker:
        def finish(self):
            finish_calls.append(1)
            return spool, "neural"

        def abort(self):
            aborted.append(True)

    drain_exit_gate = threading.Event()

    class _NormalExitDrainThread:
        # Alive at the gate; the join blocks until the test lets the
        # drain "exit" — through its normal stop path: no cleanup, no
        # drain_cleaned, worker+spool+slot kept for the send.
        def is_alive(self):
            return True

        def join(self):
            drain_exit_gate.wait(10)

    class _RevokeOnCloseEvent:
        # stopped_event whose first confirmed read delivers the user's
        # revoke: the delete lands between the confirmed close and the
        # finisher gate — Milo's exact pre-helper interleave.
        def __init__(self):
            self._set = False

        def set(self):
            self._set = True

        def is_set(self):
            if self._set:
                controller.revoke("m1")
            return self._set

    session = _send_session(controller, frames, worker=_Worker(), spool=spool,
                            stream=_FakeStream(),
                            thread=_NormalExitDrainThread())
    session["stopped_event"] = _RevokeOnCloseEvent()
    mw = _FakeSendMW(controller)
    panel = _SendPanel(mw, frames, session)
    vr.VoiceRecordingMixin._send_voice_message(panel, None)
    # The note is never enqueued and the send does NOT wait for the
    # drain: its TERMINAL forget runs while the drain is still parked
    # (the real forget() call, not is_preparing — the revoke already
    # moved the entry to CANCELLED, which proves nothing).
    assert send_returned.wait(10), "send thread never reached its terminal forget"
    assert mw.message_queue.enqueued == []
    assert session["cancel"] is True
    assert session["revoked"] is True
    # The drain is still "exiting": nobody cleaned, finish() never ran.
    assert finish_calls == []
    assert aborted == []
    # The drain exits NORMALLY without re-reading cancel: the finalizer
    # resumes post-join, sees superseded with drain_cleaned absent, and
    # reclaims the remnant exactly once. Wait for the cleanup
    # COMPLETION signal, not an intermediate step (abort precedes
    # unlink and the slot release).
    drain_exit_gate.set()
    assert cleanup_done.wait(5), "finalizer never released the slot"
    assert finish_calls == []
    assert aborted == [True]
    assert unlinked.count(spool) == 1
    assert not os.path.exists(spool)
    # ...and the slot is free for the next note.
    assert controller.acquire_slot("probe", {"cancel": False})
    controller.release_slot("probe")


def test_send_revoke_after_finisher_ready_send_claims_once(send_env, monkeypatch):
    """Milo 17:55 second order of the pre-gate cancel window: the finisher
    COMPLETES finish() and publishes ready while cancel is still False (its
    post-join check already ran), exits unclaimed, and the revoke lands
    only then — before the send's cancel check. The send must NOT walk
    away supposing a live helper: it arbitrates under finish_lock, claims
    the completed result itself and cleans it exactly once — 0 enqueue,
    1 finish (completed before the revoke), 1 abort, 1 spool unlink, slot
    free after the cleanup-COMPLETION signal. The finisher's death, not a
    flag, gates the revoke: dict reads of "cancel" are hooked AFTER the
    finisher thread is gone, so the interleave is deterministic."""
    import core.audio_processing as ap
    from core import neural_noise_reduction as nnr
    monkeypatch.setattr(ap, "apply_noise_gate",
                        lambda data, rate, ch, **kw: bytes(reversed(data)))
    monkeypatch.setattr(nnr, "reduce_voice_noise", _no_neural)
    controller = IncrementalDenoiseController()
    cleanup_done = _cleanup_signal(controller)
    send_returned = _forget_signal(controller)
    frames = [b"\x1a\x1b" * 480]
    spool = str(send_env / "spool.pcm")
    open(spool, "wb").write(b"work")
    unlinked = []
    real_unlink = os.unlink

    def _counting_unlink(path):
        unlinked.append(path)
        return real_unlink(path)

    monkeypatch.setattr(os, "unlink", _counting_unlink)
    finish_calls = []
    aborted = []

    class _Worker:
        def finish(self):
            finish_calls.append(1)
            return spool, "neural"

        def abort(self):
            aborted.append(True)

    class _ExitedNormalDrainThread:
        # The drain already exited through its normal stop path: no
        # cleanup, no drain_cleaned, worker+spool+slot kept for the send.
        def is_alive(self):
            return False

        def join(self):
            return None

    # Record the finisher thread the module creates so the hook can wait
    # for its DEATH — the only external proof that the helper published
    # ready and exited unclaimed (its finally is the last step). The
    # target name is captured AT CREATION: CPython clears Thread._target
    # when the thread finishes, and the finisher completes fast.
    created_threads = []
    real_thread = threading.Thread

    class _ThreadShim:
        @staticmethod
        def Thread(*a, **k):
            t = real_thread(*a, **k)
            target = k.get("target", a[0] if a else None)
            created_threads.append(
                (t, getattr(target, "__name__", "")))
            return t

        def __getattr__(self, name):
            return getattr(threading, name)

    monkeypatch.setattr(isend, "threading", _ThreadShim())

    class _RevokeOnCancelRead(dict):
        # The consume path's first "cancel" item-read is the send's check
        # right after finisher.start() (the helper uses .get()). Hold that
        # read until the finisher is DEAD — ready published with cancel
        # False, helper exited — then deliver the revoke: Milo's exact
        # second-order interleave.
        fired = False

        def __getitem__(self, key):
            if key == "cancel" and not self.fired:
                self.fired = True
                deadline = time.time() + 10
                finisher = next(
                    (t for t, name in created_threads
                     if name == "_finish_incremental"), None)
                assert finisher is not None, "finisher thread was never created"
                while finisher.is_alive() and time.time() < deadline:
                    time.sleep(0.005)
                assert not finisher.is_alive(), "finisher never exited"
                controller.revoke("m1")
            return super().__getitem__(key)

    # The hooked session IS the registered one: the controller's revoke
    # and the send thread must see the same dict.
    session = _RevokeOnCancelRead({
        "local_id": "m1", "frames": frames, "rate": 48000, "ch": 1,
        "worker_ch": 1, "worker": _Worker(), "thread": _ExitedNormalDrainThread(),
        "stop": False, "cancel": False, "failed": False, "revoked": False,
        "stopped_event": threading.Event(), "device_failed": False,
        "basic_only": False, "spool": spool, "stream": _FakeStream()})
    assert controller.acquire_slot("m1", session)
    mw = _FakeSendMW(controller)
    panel = _SendPanel(mw, frames, session)
    vr.VoiceRecordingMixin._send_voice_message(panel, None)
    # The send claims the completed result itself and returns WITHOUT
    # enqueueing; its terminal forget runs after the cleanup.
    assert cleanup_done.wait(10), "send-side claim never released the slot"
    assert send_returned.wait(10), "send thread never reached its terminal forget"
    assert mw.message_queue.enqueued == []
    assert session["cancel"] is True
    assert session["revoked"] is True
    assert finish_calls == [1]
    assert aborted == [True]
    assert unlinked.count(spool) == 1
    assert not os.path.exists(spool)
    assert controller.acquire_slot("probe", {"cancel": False})
    controller.release_slot("probe")


# --- Engine/moment selector (voice_nr_engine / voice_nr_moment) ---
# The incremental pass only runs for the neural engine while recording.
# Every other combination keeps the published flow: no session at record
# time, offline reduction at send time.


def test_start_skipped_when_engine_is_basic():
    panel = _FakePanel()
    panel.main_window.settings["general"]["voice_nr_engine"] = "basic"
    _start(panel)
    assert panel._incremental_session is None


def test_start_skipped_when_moment_is_send():
    panel = _FakePanel()
    panel.main_window.settings["general"]["voice_nr_moment"] = "send"
    _start(panel)
    assert panel._incremental_session is None


def test_start_skipped_with_default_settings():
    # Keys absent: the defaults (neural, at send time) must reproduce the
    # published behavior, so no incremental session is created.
    panel = _FakePanel()
    general = panel.main_window.settings["general"]
    del general["voice_nr_engine"], general["voice_nr_moment"]
    _start(panel)
    assert panel._incremental_session is None


def test_start_still_registers_when_neural_and_recording():
    panel = _FakePanel()
    panel.main_window.settings["general"]["voice_nr_moment"] = "record"
    _start(panel)
    session = panel._incremental_session
    assert session is not None
    _join_drain(panel)  # no model here: the drain fails and self-cleans
    controller = panel.main_window._incremental_controller
    controller.discard(session["local_id"])


def _no_gate(*a, **k):
    raise AssertionError("apply_noise_gate must not run for this note")


def test_send_basic_engine_goes_straight_to_gate_without_session(send_env, monkeypatch):
    import core.audio_processing as ap
    import core.neural_noise_reduction as nnr
    monkeypatch.setattr(ap, "apply_noise_gate",
                        lambda data, rate, ch, **kw: bytes(reversed(data)))
    monkeypatch.setattr(nnr, "reduce_voice_noise", _no_neural)
    controller = IncrementalDenoiseController()
    frames = [b"\x05\x06" * 480]
    raw = b"".join(frames)
    mw = _FakeSendMW(controller)
    mw.settings["general"]["voice_nr_engine"] = "basic"
    panel = _SendPanel(mw, frames, None)
    vr.VoiceRecordingMixin._send_voice_message(panel, None)
    assert mw.message_queue.event.wait(10)
    pm = mw.message_queue.enqueued[0]
    assert _wav_payload(pm.audio_path) == bytes(reversed(raw))


def test_send_neural_at_send_moment_uses_the_offline_model(send_env, monkeypatch):
    import core.audio_processing as ap
    import core.neural_noise_reduction as nnr
    monkeypatch.setattr(ap, "apply_noise_gate", _no_gate)
    monkeypatch.setattr(nnr, "reduce_voice_noise",
                        lambda data, rate, ch: (bytes(reversed(data)), "neural"))
    controller = IncrementalDenoiseController()
    frames = [b"\x07\x08" * 480]
    raw = b"".join(frames)
    mw = _FakeSendMW(controller)
    mw.settings["general"]["voice_nr_moment"] = "send"
    panel = _SendPanel(mw, frames, None)
    vr.VoiceRecordingMixin._send_voice_message(panel, None)
    assert mw.message_queue.event.wait(10)
    pm = mw.message_queue.enqueued[0]
    assert _wav_payload(pm.audio_path) == bytes(reversed(raw))


def test_two_notes_reduced_at_send_at_once_both_get_the_neural_model(
        send_env, monkeypatch):
    # Default settings (neural, at send): a long note is still being
    # reduced when a short one is sent. Offline passes share the singleton,
    # so the second must NOT be downgraded to the basic gate because the
    # first holds an offline reservation (reviewer 1, PR #834).
    import core.audio_processing as ap
    import core.neural_noise_reduction as nnr
    monkeypatch.setattr(ap, "apply_noise_gate", _no_gate)
    first_entered = threading.Event()
    release_first = threading.Event()
    second_called = threading.Event()

    def _reduce(data, rate, ch):
        if not first_entered.is_set():
            first_entered.set()
            assert release_first.wait(10)
        else:
            second_called.set()
        return bytes(reversed(data)), "neural"

    monkeypatch.setattr(nnr, "reduce_voice_noise", _reduce)
    controller = IncrementalDenoiseController()
    mw = _FakeSendMW(controller)
    mw.settings["general"]["voice_nr_moment"] = "send"
    long_raw = bytes([0x11, 0x12]) * 960
    short_raw = bytes([0x21, 0x22]) * 480
    vr.VoiceRecordingMixin._send_voice_message(
        _SendPanel(mw, [long_raw], None), None)
    assert first_entered.wait(10)
    vr.VoiceRecordingMixin._send_voice_message(
        _SendPanel(mw, [short_raw], None), None)
    try:
        assert second_called.wait(10), "second note never reached the model"
    finally:
        release_first.set()
    deadline = time.time() + 10
    while time.time() < deadline and len(mw.message_queue.enqueued) < 2:
        time.sleep(0.02)
    payloads = [_wav_payload(pm.audio_path) for pm in mw.message_queue.enqueued]
    assert payloads == [bytes(reversed(long_raw)), bytes(reversed(short_raw))]
    assert not controller._offline_owners


def test_send_after_switching_to_send_moment_goes_offline(send_env, monkeypatch):
    # Recorded with neural-while-recording, but the preference changed to
    # at-send before the note was sent: the published offline path applies
    # and the session is cancelled so its drain self-cleans.
    import core.audio_processing as ap
    import core.neural_noise_reduction as nnr
    monkeypatch.setattr(ap, "apply_noise_gate", _no_gate)
    monkeypatch.setattr(nnr, "reduce_voice_noise",
                        lambda data, rate, ch: (bytes(reversed(data)), "neural"))
    controller = IncrementalDenoiseController()
    frames = [b"\x09\x0a" * 480]
    raw = b"".join(frames)
    spool = str(send_env / "spool.pcm")
    open(spool, "wb").write(b"work")

    class _Worker:
        def finish(self):
            raise AssertionError("finish must not run after the switch")

        def abort(self):
            pass

    session = _send_session(controller, frames, worker=_Worker(),
                            spool=spool, stream=_FakeStream())
    mw = _FakeSendMW(controller)
    mw.settings["general"]["voice_nr_moment"] = "send"
    panel = _SendPanel(mw, frames, session)
    vr.VoiceRecordingMixin._send_voice_message(panel, None)
    assert mw.message_queue.event.wait(10)
    pm = mw.message_queue.enqueued[0]
    assert _wav_payload(pm.audio_path) == bytes(reversed(raw))
    assert session["cancel"] is True


# --- Fourth option (voice_nr_on_failure="keep_pending"): when BOTH neural
# processings fail, keep the note pending for a manual resend instead of
# sending it gated or unprocessed. Never retried automatically. ---


def _keep_pending_mw(controller):
    mw = _FakeSendMW(controller)
    mw.settings["general"]["voice_nr_on_failure"] = "keep_pending"
    announced = []
    mw.output = lambda msg, **k: announced.append(msg)
    return mw, announced


def _drive_wx_calls():
    for fn, a, k in WX_CALLS:
        fn(*a, **k)


def _wait_kept_pending(panel, timeout=10.0):
    """The send thread keeps the note pending off the caller thread: wait
    until the resend file is recorded (the last step before the UI call)."""
    from core.message_resend import RESEND_PATH_KEY
    deadline = time.time() + timeout
    while time.time() < deadline:
        registered = panel.registered
        if registered is not None and RESEND_PATH_KEY in registered:
            return registered[RESEND_PATH_KEY]
        time.sleep(0.05)
    raise AssertionError("the note was never kept pending")


def test_send_kept_pending_when_both_neural_paths_fail(send_env, monkeypatch):
    import core.neural_noise_reduction as nnr

    def _boom(*a, **k):
        raise RuntimeError("no model")

    monkeypatch.setattr(nnr, "reduce_voice_noise", _boom)
    from core.message_resend import RESEND_PATH_KEY, resend_request
    controller = IncrementalDenoiseController()
    frames = [b"\x0d\x0e" * 480]
    raw = b"".join(frames)
    mw, announced = _keep_pending_mw(controller)
    panel = _SendPanel(mw, frames, None)
    panel.failed = []
    panel._mark_message_failed = lambda local_id: panel.failed.append(local_id)
    WX_CALLS.clear()
    vr.VoiceRecordingMixin._send_voice_message(panel, None)
    path = _wait_kept_pending(panel)
    # Nothing was sent.
    assert mw.message_queue.enqueued == []
    # The pending row keeps the ORIGINAL audio for the manual resend.
    assert _wav_payload(path) == raw
    # ...and the existing "Try sending again" affordance accepts the row.
    panel.registered["_send_failed"] = True  # what the real marker sets
    assert resend_request(panel.registered) == ("media", path, "audio", "")
    # Marked failed and announced on the UI thread.
    _drive_wx_calls()
    assert panel.failed == [panel.registered["_local_id"]]
    assert announced == ["noise_reduction_kept_pending"]
    assert "_incremental_preparing" not in panel.registered


def test_send_kept_pending_when_neural_returns_gate_result(send_env, monkeypatch):
    import core.neural_noise_reduction as nnr
    monkeypatch.setattr(nnr, "reduce_voice_noise",
                        lambda data, rate, ch: (bytes(reversed(data)), "gate"))
    controller = IncrementalDenoiseController()
    frames = [b"\x0f\x10" * 480]
    mw, announced = _keep_pending_mw(controller)
    panel = _SendPanel(mw, frames, None)
    panel.failed = []
    panel._mark_message_failed = lambda local_id: panel.failed.append(local_id)
    WX_CALLS.clear()
    vr.VoiceRecordingMixin._send_voice_message(panel, None)
    _wait_kept_pending(panel)
    # A gate result is NOT the neural engine: the note stays pending, the
    # gated audio is never sent.
    assert mw.message_queue.enqueued == []
    _drive_wx_calls()
    assert panel.failed == [panel.registered["_local_id"]]
    assert announced == ["noise_reduction_kept_pending"]


def test_send_basic_engine_still_sends_with_keep_pending_option(send_env, monkeypatch):
    # The basic engine is an explicit choice, not a neural failure.
    import core.audio_processing as ap
    import core.neural_noise_reduction as nnr
    monkeypatch.setattr(ap, "apply_noise_gate",
                        lambda data, rate, ch, **kw: bytes(reversed(data)))
    monkeypatch.setattr(nnr, "reduce_voice_noise", _no_neural)
    controller = IncrementalDenoiseController()
    frames = [b"\x11\x12" * 480]
    raw = b"".join(frames)
    mw, _ = _keep_pending_mw(controller)
    mw.settings["general"]["voice_nr_engine"] = "basic"
    panel = _SendPanel(mw, frames, None)
    vr.VoiceRecordingMixin._send_voice_message(panel, None)
    assert mw.message_queue.event.wait(10)
    pm = mw.message_queue.enqueued[0]
    assert _wav_payload(pm.audio_path) == bytes(reversed(raw))


def test_send_slot_busy_session_keeps_pending_without_second_onnx(send_env, monkeypatch):
    # Review 18:28 contract 1: slot busy at record + STILL busy at send is a
    # TEMPORARY impossibility, not a chosen basic engine — with keep_pending
    # the raw note stays pending and the offline pass (a second ONNX next to
    # the live one) is never run.
    import core.neural_noise_reduction as nnr
    called = []
    monkeypatch.setattr(nnr, "reduce_voice_noise",
                        lambda *a: (called.append(a), (a[0], "neural"))[1])
    controller = IncrementalDenoiseController()
    frames = [b"\x13\x14" * 480]
    raw = b"".join(frames)
    session = _send_session(controller, frames, basic_only=True, stream=_FakeStream())
    # Another note's live session holds the slot at send time.
    assert controller.acquire_slot("other-note", {"cancel": False})
    mw, announced = _keep_pending_mw(controller)
    panel = _SendPanel(mw, frames, session)
    panel.failed = []
    panel._mark_message_failed = lambda local_id: panel.failed.append(local_id)
    WX_CALLS.clear()
    vr.VoiceRecordingMixin._send_voice_message(panel, None)
    path = _wait_kept_pending(panel)
    assert mw.message_queue.enqueued == []
    assert called == []                      # no second ONNX
    assert _wav_payload(path) == raw         # the ORIGINAL is what is kept
    _drive_wx_calls()
    assert announced == ["noise_reduction_kept_pending"]
    controller.release_slot("other-note")


def test_slot_busy_at_record_freed_at_send_allows_posterior(send_env, monkeypatch):
    # Same contract, other side: the slot freed between record and send, so
    # the posterior neural pass is possible and its success sends the
    # PROCESSED note (freed impossibility is not a pending reason).
    import core.neural_noise_reduction as nnr
    monkeypatch.setattr(nnr, "reduce_voice_noise",
                        lambda data, rate, ch: (bytes(reversed(data)), "neural"))
    controller = IncrementalDenoiseController()
    frames = [b"\x3b\x3c" * 480]
    raw = b"".join(frames)
    session = _send_session(controller, frames, basic_only=True, stream=_FakeStream())
    # The other session's slot is free by send time (basic_only sessions
    # never hold it themselves).
    mw, _ = _keep_pending_mw(controller)
    panel = _SendPanel(mw, frames, session)
    vr.VoiceRecordingMixin._send_voice_message(panel, None)
    assert mw.message_queue.event.wait(10)
    pm = mw.message_queue.enqueued[0]
    assert _wav_payload(pm.audio_path) == bytes(reversed(raw))


def test_model_unavailable_session_keeps_pending_without_posterior(send_env, monkeypatch):
    # Contract 1: the incremental side already knows the model is missing —
    # running the offline pass would only re-fail the same load, so with
    # keep_pending the raw note goes straight to pending.
    import core.neural_noise_reduction as nnr
    called = []
    monkeypatch.setattr(nnr, "reduce_voice_noise",
                        lambda *a: (called.append(a), (a[0], "neural"))[1])
    controller = IncrementalDenoiseController()
    frames = [b"\x3d\x3e" * 480]
    raw = b"".join(frames)
    session = _send_session(controller, frames, stream=_FakeStream())
    session["model_unavailable"] = True
    mw, announced = _keep_pending_mw(controller)
    panel = _SendPanel(mw, frames, session)
    panel.failed = []
    panel._mark_message_failed = lambda local_id: panel.failed.append(local_id)
    WX_CALLS.clear()
    vr.VoiceRecordingMixin._send_voice_message(panel, None)
    path = _wait_kept_pending(panel)
    assert mw.message_queue.enqueued == []
    assert called == []
    assert _wav_payload(path) == raw
    controller.release_slot("m1")


def test_keep_pending_revoked_during_posterior_leaves_nothing(send_env, monkeypatch):
    # Contract 5: the user deletes the pending row WHILE the posterior pass
    # runs — no WAV is kept, nothing is marked or announced, the controller
    # entry is forgotten.
    import core.neural_noise_reduction as nnr
    controller = IncrementalDenoiseController()
    forgotten = _forget_signal(controller)
    frames = [b"\x39\x3a" * 480]
    session = _send_session(controller, frames, stream=_FakeStream())

    def _slow_boom(data, rate, ch):
        session["revoked"] = True  # the delete lands mid-reduction
        raise RuntimeError("no model")

    monkeypatch.setattr(nnr, "reduce_voice_noise", _slow_boom)
    mw, announced = _keep_pending_mw(controller)
    panel = _SendPanel(mw, frames, session)
    panel.failed = []
    panel._mark_message_failed = lambda local_id: panel.failed.append(local_id)
    WX_CALLS.clear()
    vr.VoiceRecordingMixin._send_voice_message(panel, None)
    assert forgotten.wait(10), "the session was never forgotten"
    time.sleep(0.2)  # let any stray write/callback land (none may)
    assert mw.message_queue.enqueued == []
    assert not list((send_env / "voice_messages").glob("*.resend.wav"))
    _drive_wx_calls()
    assert announced == []
    assert panel.failed == []


def test_keep_pending_row_deleted_before_mark_deletes_file(send_env, monkeypatch):
    # Contract 5, UI side: the row is deleted between the keep-write and the
    # UI callback — the callback (same thread as deletions) deletes the kept
    # file and announces nothing on the dead row.
    import core.neural_noise_reduction as nnr

    def _boom(*a, **k):
        raise RuntimeError("no model")

    monkeypatch.setattr(nnr, "reduce_voice_noise", _boom)
    controller = IncrementalDenoiseController()
    frames = [b"\x41\x42" * 480]
    mw, announced = _keep_pending_mw(controller)
    panel = _SendPanel(mw, frames, None)
    panel.failed = []
    panel._mark_message_failed = lambda local_id: panel.failed.append(local_id)
    WX_CALLS.clear()
    vr.VoiceRecordingMixin._send_voice_message(panel, None)
    path = _wait_kept_pending(panel)
    assert os.path.exists(path)
    panel._sorted_messages.clear()  # the delete lands first
    _drive_wx_calls()
    assert panel.failed == []
    assert announced == []
    assert not os.path.exists(path)


def test_keep_pending_save_failure_never_announces_a_retry(send_env, monkeypatch):
    # Contract 5: the keep-write itself failed — the row is marked failed
    # but the announcement must NOT promise a retry that cannot happen.
    import core.neural_noise_reduction as nnr

    def _boom(*a, **k):
        raise RuntimeError("no model")

    monkeypatch.setattr(nnr, "reduce_voice_noise", _boom)

    class _BrokenWave:
        @staticmethod
        def open(*a, **k):
            raise OSError("disk full")

    monkeypatch.setattr(vns, "wave", _BrokenWave)
    controller = IncrementalDenoiseController()
    frames = [b"\x43\x44" * 480]
    mw, announced = _keep_pending_mw(controller)
    panel = _SendPanel(mw, frames, None)
    panel.failed = []
    panel._mark_message_failed = lambda local_id: panel.failed.append(local_id)
    WX_CALLS.clear()
    vr.VoiceRecordingMixin._send_voice_message(panel, None)
    deadline = time.time() + 10
    while time.time() < deadline and not WX_CALLS:
        time.sleep(0.05)
    assert WX_CALLS, "the failed keep was never reported"
    from core.message_resend import RESEND_PATH_KEY
    assert RESEND_PATH_KEY not in (panel.registered or {})
    assert not list((send_env / "voice_messages").glob("*.resend.wav"))
    _drive_wx_calls()
    assert panel.failed == [panel.registered["_local_id"]]
    assert announced == ["noise_reduction_pending_save_failed"]


def test_send_neural_failure_sends_unprocessed_by_default(send_env, monkeypatch):
    # Option unset: the published behavior is unchanged — the note goes out
    # unprocessed with the published announcement.
    import core.neural_noise_reduction as nnr

    def _boom(*a, **k):
        raise RuntimeError("no model")

    monkeypatch.setattr(nnr, "reduce_voice_noise", _boom)
    controller = IncrementalDenoiseController()
    frames = [b"\x15\x16" * 480]
    raw = b"".join(frames)
    mw = _FakeSendMW(controller)
    announced = []
    mw.output = lambda msg, **k: announced.append(msg)
    panel = _SendPanel(mw, frames, None)
    WX_CALLS.clear()
    vr.VoiceRecordingMixin._send_voice_message(panel, None)
    assert mw.message_queue.event.wait(10)
    pm = mw.message_queue.enqueued[0]
    assert _wav_payload(pm.audio_path) == raw
    assert any(fn == mw.output and a and a[0] == "noise_reduction_failed"
               for fn, a, k in WX_CALLS)


# --- Fourth-option retry semantics: "Try sending again" RETRIES THE
# PROCESSING on the kept original; it never resends the raw audio, never
# uses the gate, and never retries on its own. ---


def _write_wav(path, raw, rate=48000, ch=1):
    with wave.open(path, "wb") as wf:
        wf.setnchannels(ch)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(raw)


def _kept_note(send_env, raw, local_id="m9"):
    import hashlib
    from core.message_resend import RESEND_PATH_KEY
    os.makedirs(send_env / "voice_messages", exist_ok=True)
    kept = str(send_env / "voice_messages" / (local_id + ".resend.wav"))
    _write_wav(kept, raw)
    with open(kept, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    msg = {"_local_id": local_id, "_local_pending": False, "_send_failed": True,
           "_nr_pending_retry": True, RESEND_PATH_KEY: kept,
           "_nr_pending_sha256": digest, "_nr_pending_size": os.path.getsize(kept),
           "key": {"id": local_id, "fromMe": True,
                   "remoteJid": "peer@s.whatsapp.net"},
           "messageType": "audioMessage", "message": {"audioMessage": {}},
           "messageTimestamp": 100}
    return msg, kept


class _RetryPanel:
    """Fake panel for the REAL _retry_nr_pending (voice-note handoff)."""

    def __init__(self, mw, row=None):
        self.main_window = mw
        self.removed = []
        self.registered = []
        self._outgoing_virtual_messages = {}
        self._sorted_messages = [row] if row is not None else []
        for _name in ("_nr_pending_row_alive", "_nr_attempt_begin",
                      "_nr_attempt_end", "_nr_attempt_context_alive",
                      "_nr_invalidate_attempts", "_nr_pending_announce",
                      "_read_nr_pending", "_nr_pending_handoff",
                      "_stage_pending_and_handoff"):
            setattr(self, _name,
                    (lambda n: (lambda *a, **k:
                                getattr(vnp.VoiceNrPendingMixin, n)(self, *a, **k))
                     )(_name))

    def _clear_empty_placeholder(self):
        pass

    def _render_message_line(self, msg):
        return "row"

    def _register_virtual_msg(self, msg):
        # Mirrors the real one (text_sending.py): records AND the outgoing
        # virtual registry the echo matcher reads.
        self.registered.append(msg)
        self._outgoing_virtual_messages[msg["_local_id"]] = msg

    def remove_messages_by_id(self, ids, focus_previous=False):
        self.removed.append(set(ids))
        # Mirrors the real one: a removed row takes its kept/processed
        # resend file with it (message_actions.py).
        from ui.conversation_panel.media_paths import discard_nr_pending_resend
        for _id in ids:
            discard_nr_pending_resend(vr.data_path("voice_messages"), _id)
        self._sorted_messages = [
            m for m in self._sorted_messages
            if not (isinstance(m, dict) and m.get("key", {}).get("id") in ids)]


def _retry_mw(controller):
    mw = _FakeSendMW(controller)
    announced = []
    mw.output = lambda msg, **k: announced.append(msg)
    mw._schedule_set_chats = lambda: None
    return mw, announced


def _wait_wx_calls(timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline and not WX_CALLS:
        time.sleep(0.05)
    assert WX_CALLS, "the retry worker never reached the UI thread"


def test_retry_nr_pending_reduces_and_sends_processed(send_env, monkeypatch):
    import core.neural_noise_reduction as nnr
    monkeypatch.setattr(nnr, "reduce_voice_noise",
                        lambda data, rate, ch: (bytes(reversed(data)), "neural"))
    controller = IncrementalDenoiseController()
    mw, announced = _retry_mw(controller)
    raw = b"\x21\x22" * 480
    msg, kept = _kept_note(send_env, raw)
    msg["contextInfo"] = {"stanzaId": "q1", "participant": "peer@s.whatsapp.net",
                          "quotedMessage": {"conversation": "hi"},
                          "_quotedFromMe": False}
    panel = _RetryPanel(mw, row=msg)
    WX_CALLS.clear()
    vnp.VoiceNrPendingMixin._retry_nr_pending(
        panel, msg, "peer@s.whatsapp.net", kept)
    _wait_wx_calls()
    _drive_wx_calls()
    # The PROCESSED audio (reversed marker) is queued as a VOICE NOTE —
    # ptt, the recording's channels, the reply quote — never a generic
    # attachment.
    assert len(mw.message_queue.enqueued) == 1
    pm = mw.message_queue.enqueued[0]
    assert pm.jid == "peer@s.whatsapp.net"
    assert _wav_payload(pm.audio_path) == bytes(reversed(raw))
    assert pm.quoted["key"]["id"] == "q1"
    # The processed copy persists as the new row's resend source, so a
    # LATER transport failure stays resendable (recoverable handoff).
    from core.message_resend import RESEND_PATH_KEY
    assert pm.audio_path.endswith(".resend.wav")
    assert os.path.basename(pm.audio_path) != os.path.basename(kept)
    assert len(panel.registered) == 1
    new_row = panel.registered[0]
    assert new_row["message"]["audioMessage"]["ptt"] is True
    assert new_row[RESEND_PATH_KEY] == pm.audio_path
    assert new_row["contextInfo"]["stanzaId"] == "q1"
    # Only once queued: the old failed row is dropped and the start announced.
    assert panel.removed == [{"m9"}]
    assert mw.message_queue.cancelled == ["m9"]
    assert announced == ["resend_started"]
    assert not getattr(panel, "_nr_attempts", {})
    os.unlink(pm.audio_path)


def test_retry_nr_pending_failure_keeps_the_row(send_env, monkeypatch):
    import core.neural_noise_reduction as nnr

    def _boom(*a, **k):
        raise RuntimeError("still no model")

    monkeypatch.setattr(nnr, "reduce_voice_noise", _boom)
    controller = IncrementalDenoiseController()
    mw, announced = _retry_mw(controller)
    raw = b"\x23\x24" * 480
    msg, kept = _kept_note(send_env, raw)
    panel = _RetryPanel(mw, row=msg)
    WX_CALLS.clear()
    vnp.VoiceNrPendingMixin._retry_nr_pending(
        panel, msg, "peer@s.whatsapp.net", kept)
    _wait_wx_calls()
    _drive_wx_calls()
    # Nothing sent, row untouched, original kept for a later retry, and the
    # attempt released its authority.
    assert mw.message_queue.enqueued == []
    assert panel.removed == []
    assert announced == ["noise_reduction_kept_pending"]
    assert os.path.exists(kept)
    assert not getattr(panel, "_nr_attempts", {})


def test_retry_nr_pending_refused_queue_keeps_the_row(send_env, monkeypatch):
    import core.neural_noise_reduction as nnr
    monkeypatch.setattr(nnr, "reduce_voice_noise",
                        lambda data, rate, ch: (bytes(reversed(data)), "neural"))
    controller = IncrementalDenoiseController()
    mw, announced = _retry_mw(controller)

    def _reject(pm):
        raise RuntimeError("queue unavailable")

    mw.message_queue.enqueue = _reject
    raw = b"\x25\x26" * 480
    msg, kept = _kept_note(send_env, raw)
    panel = _RetryPanel(mw, row=msg)
    WX_CALLS.clear()
    vnp.VoiceNrPendingMixin._retry_nr_pending(
        panel, msg, "peer@s.whatsapp.net", kept)
    _wait_wx_calls()
    _drive_wx_calls()
    # The representation WAS registered before the queue was activated and
    # rolled back when the enqueue was rejected: the old pending row and
    # the kept original stay, and NO new artifact (WAV/MSV/row/virtual
    # registration) is left behind (review 18:47, contracts 3+5).
    assert len(panel.registered) == 1
    new_id = panel.registered[0]["_local_id"]
    assert panel.removed == [{new_id}]
    assert all(not (isinstance(m, dict) and m.get("_local_id") == new_id)
               for m in panel._sorted_messages)
    assert new_id not in panel._outgoing_virtual_messages
    assert announced == ["noise_reduction_kept_pending"]
    assert os.path.exists(kept)
    assert sorted(os.listdir(send_env / "voice_messages")) == \
        [os.path.basename(kept)]
    assert not getattr(panel, "_nr_attempts", {})


def test_double_retry_runs_a_single_attempt(send_env, monkeypatch):
    # Contract 2: attempt authority — a second "Try sending again" while an
    # attempt runs is ignored, so a double retry never transfers twice.
    import core.neural_noise_reduction as nnr
    gate = threading.Event()

    def _slow(data, rate, ch):
        gate.wait(10)
        return bytes(reversed(data)), "neural"

    monkeypatch.setattr(nnr, "reduce_voice_noise", _slow)
    controller = IncrementalDenoiseController()
    mw, announced = _retry_mw(controller)
    raw = b"\x45\x46" * 480
    msg, kept = _kept_note(send_env, raw)
    panel = _RetryPanel(mw, row=msg)
    WX_CALLS.clear()
    vnp.VoiceNrPendingMixin._retry_nr_pending(
        panel, msg, "peer@s.whatsapp.net", kept)
    vnp.VoiceNrPendingMixin._retry_nr_pending(
        panel, msg, "peer@s.whatsapp.net", kept)  # ignored: attempt in flight
    gate.set()
    _wait_wx_calls()
    _drive_wx_calls()
    assert len(mw.message_queue.enqueued) == 1
    assert announced == ["resend_started"]
    assert not getattr(panel, "_nr_attempts", {})
    os.unlink(mw.message_queue.enqueued[0].audio_path)


def test_reduce_voice_noise_serializes_concurrent_inference(send_env, monkeypatch):
    # Contract 3: the singleton's lock covers CONSTRUCTION only; inference
    # itself is serialized — concurrent run() on one InferenceSession is a
    # property of the pinned onnxruntime this codebase does not claim.
    import core.neural_noise_reduction as nnr
    peak = [0]
    active = [0]
    lock = threading.Lock()
    entered = threading.Event()
    release = threading.Event()

    def _fake_denoise(audio, rate, channels, denoiser):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        entered.set()
        release.wait(10)
        with lock:
            active[0] -= 1
        return audio

    monkeypatch.setattr(nnr, "_denoiser", object())
    monkeypatch.setattr(nnr, "denoise_pcm16", _fake_denoise)
    results = []

    def _call():
        results.append(nnr.reduce_voice_noise(b"\x01\x02" * 480, 48000, 1))

    t1 = threading.Thread(target=_call)
    t2 = threading.Thread(target=_call)
    t1.start()
    assert entered.wait(10)
    t2.start()
    time.sleep(0.3)  # t2 must be BLOCKED on the inference lock, not inside
    release.set()
    t1.join(10)
    t2.join(10)
    assert peak[0] == 1
    assert sorted(r[1] for r in results) == ["neural", "neural"]


def test_resend_routes_nr_pending_note_to_processing_retry(send_env, monkeypatch):
    from core.message_resend import resend_request  # noqa: F401 (kept visible)
    raw = b"\x27\x28" * 480
    msg, kept = _kept_note(send_env, raw)
    retried = []
    panel = types.SimpleNamespace(
        conversation={"remoteJid": "peer@s.whatsapp.net"},
        _retry_nr_pending=lambda m, jid, path: retried.append((m, jid, path)),
    )
    ma.MessageActionsMixin._on_menu_resend_message(panel, msg)
    assert retried == [(msg, "peer@s.whatsapp.net", kept)]


def test_remove_messages_by_id_discards_kept_pending_files():
    # The artifact cleanup is wired into the REAL row-removal point, not
    # assumed: the kept original dies with its row (delete, successful
    # resend), never outliving it silently.
    import inspect
    src = inspect.getsource(ma.MessageActionsMixin.remove_messages_by_id)
    assert "discard_nr_pending_resend" in src


def test_discard_nr_pending_resend_removes_only_the_kept_file(tmp_path):
    from ui.conversation_panel.media_paths import discard_nr_pending_resend
    voice_dir = str(tmp_path)
    kept = os.path.join(voice_dir, "m9.resend.wav")
    other = os.path.join(voice_dir, "m9.msv")
    _write_wav(kept, b"\x01\x02" * 100)
    open(other, "wb").write(b"msv")
    discard_nr_pending_resend(voice_dir, "m9")
    assert not os.path.exists(kept)
    assert os.path.exists(other)
    # Idempotent and tolerant: already gone, empty id.
    discard_nr_pending_resend(voice_dir, "m9")
    discard_nr_pending_resend(voice_dir, "")


def test_switch_moment_cancel_cleanup_contract_fake_drain(send_env, monkeypatch):
    # Preference changed record->send while a drain is live. Provenance:
    # this is a WIRING test — the drain here is a FAKE thread that honors
    # the documented exit contract (observe cancel -> spool unlink -> slot
    # release), NOT the real drain_capture_frames loop and NOT the model;
    # the real loop's cleanup paths are covered at core level in
    # test_incremental_denoise.py. What is observed here: the send cancels
    # the session and cleanup lands through the contract, not by assumption.
    import core.neural_noise_reduction as nnr
    monkeypatch.setattr(nnr, "reduce_voice_noise",
                        lambda data, rate, ch: (bytes(reversed(data)), "neural"))
    controller = IncrementalDenoiseController()
    cleanup_done = _cleanup_signal(controller)
    frames = [b"\x29\x2a" * 480]
    raw = b"".join(frames)
    spool = str(send_env / "spool.pcm")
    open(spool, "wb").write(b"work")

    class _Worker:
        def finish(self):
            raise AssertionError("finish must not run after the switch")

        def abort(self):
            pass

    session = _send_session(controller, frames, worker=_Worker(),
                            spool=spool, stream=_FakeStream())

    def _drain():
        # Live drain: observes the cancel the send set, then follows the
        # real drain's exit contract (spool unlink + slot release, and the
        # ownership marker the send/reaper key on).
        while not session["cancel"]:
            time.sleep(0.02)
        session["drain_cleaned"] = True
        try:
            os.unlink(session["spool"])
        except OSError:
            pass
        controller.release_slot(session["local_id"])

    drain = threading.Thread(target=_drain, daemon=True)
    drain.start()
    session["thread"] = drain

    mw = _FakeSendMW(controller)
    mw.settings["general"]["voice_nr_moment"] = "send"  # changed mid-flight
    panel = _SendPanel(mw, frames, session)
    vr.VoiceRecordingMixin._send_voice_message(panel, None)
    assert mw.message_queue.event.wait(10)
    pm = mw.message_queue.enqueued[0]
    # Offline neural on the complete raw (the preference now says at-send).
    assert _wav_payload(pm.audio_path) == bytes(reversed(raw))
    assert session["cancel"] is True
    assert cleanup_done.wait(5), "drain never released the slot"
    drain.join(5)
    assert not drain.is_alive()
    assert not os.path.exists(spool)
    assert controller.acquire_slot("later", {"cancel": False})
    controller.release_slot("later")


# --- Audited retry contracts (revision 7-nov 18:26): one model at a time,
# fingerprint immutability, delete wins every race. ---


def test_retry_deferred_while_incremental_slot_busy(send_env, monkeypatch):
    import core.neural_noise_reduction as nnr
    called = []
    monkeypatch.setattr(nnr, "reduce_voice_noise",
                        lambda *a: (called.append(a), (a[0], "neural"))[1])
    controller = IncrementalDenoiseController()
    assert controller.acquire_slot("other-note", {"cancel": False})
    mw, announced = _retry_mw(controller)
    raw = b"\x31\x32" * 480
    msg, kept = _kept_note(send_env, raw)
    panel = _RetryPanel(mw, row=msg)
    WX_CALLS.clear()
    vnp.VoiceNrPendingMixin._retry_nr_pending(
        panel, msg, "peer@s.whatsapp.net", kept)
    _wait_wx_calls()
    _drive_wx_calls()
    # No second ONNX next to the live incremental one: nothing ran, the row
    # stays pending with its file, the deferral is announced.
    assert called == []
    assert mw.message_queue.enqueued == []
    assert announced == ["noise_reduction_kept_pending"]
    assert os.path.exists(kept)
    assert not getattr(panel, "_nr_attempts", {})
    controller.release_slot("other-note")


def test_retry_waits_for_cleanup_when_the_same_note_retains_its_slot(send_env, monkeypatch):
    # Regression (review 19:07): a native timeout leaves skip_posterior +
    # forget() with the slot STILL HELD BY THE SAME NOTE and its private
    # model possibly alive. ID equality is NOT cleanup: the retry must
    # defer — never load the singleton next to that model — until the
    # slot is actually released. Only then does the same row's retry run.
    import core.neural_noise_reduction as nnr
    called = []
    monkeypatch.setattr(nnr, "reduce_voice_noise",
                        lambda data, rate, ch: (called.append(data), (data, "neural"))[1])
    controller = IncrementalDenoiseController()
    # The kept note's OWN native session retains the slot (timeout/forget).
    assert controller.acquire_slot("m9", {"cancel": False})
    mw, announced = _retry_mw(controller)
    raw = b"\x53\x54" * 480
    msg, kept = _kept_note(send_env, raw, local_id="m9")
    panel = _RetryPanel(mw, row=msg)
    WX_CALLS.clear()
    vnp.VoiceNrPendingMixin._retry_nr_pending(
        panel, msg, "peer@s.whatsapp.net", kept)
    _wait_wx_calls()
    _drive_wx_calls()
    # No model call next to the possibly-live private one: deferred.
    assert called == []
    assert mw.message_queue.enqueued == []
    assert announced == ["noise_reduction_kept_pending"]
    assert not controller._offline_owners
    assert not getattr(panel, "_nr_attempts", {})
    # Cleanup confirmed (the slot is really released): the SAME row's
    # retry now runs the reduction and hands off.
    controller.release_slot("m9")
    WX_CALLS.clear()
    vnp.VoiceNrPendingMixin._retry_nr_pending(
        panel, msg, "peer@s.whatsapp.net", kept)
    _wait_wx_calls()
    _drive_wx_calls()
    assert called == [raw]
    assert announced == ["noise_reduction_kept_pending", "resend_started"]
    os.unlink(mw.message_queue.enqueued[0].audio_path)


def test_retry_processes_a_kept_note_without_fingerprint(send_env, monkeypatch):
    # Hash ausente (corrección de alcance 18:47): when NO fingerprint was
    # recorded at keep time, the check is SKIPPED, not claimed mandatory —
    # the retry processes the kept note without rejecting it.
    import core.neural_noise_reduction as nnr
    called = []
    monkeypatch.setattr(nnr, "reduce_voice_noise",
                        lambda data, rate, ch: (called.append(data), (data, "neural"))[1])
    controller = IncrementalDenoiseController()
    mw, announced = _retry_mw(controller)
    raw = b"\x57\x58" * 480
    msg, kept = _kept_note(send_env, raw)
    msg.pop("_nr_pending_sha256", None)
    msg.pop("_nr_pending_size", None)
    panel = _RetryPanel(mw, row=msg)
    WX_CALLS.clear()
    vnp.VoiceNrPendingMixin._retry_nr_pending(
        panel, msg, "peer@s.whatsapp.net", kept)
    _wait_wx_calls()
    _drive_wx_calls()
    assert called == [raw]
    assert announced == ["resend_started"]
    os.unlink(mw.message_queue.enqueued[0].audio_path)


def test_real_native_timeout_keeps_pending_and_blocks_retry_until_cleanup(send_env, monkeypatch):
    # E2E with a REAL hanging drain thread and the REAL 2 s final wait
    # (Milo 19:11: the m9 guard was synthetic). A moment=send switch with
    # a drain that hangs past the wait: skip_posterior keeps the note
    # pending with its RAW audio, the slot stays RETAINED (forget() never
    # frees it), and a retry of that same row defers — no model call —
    # until the drain really finishes and releases the slot. Audio
    # capture and wx remain stubbed; the timeout, threads, reservation
    # lifecycle and keep/retry chain are real.
    from core.message_resend import RESEND_PATH_KEY
    import core.neural_noise_reduction as nnr
    called = []
    monkeypatch.setattr(nnr, "reduce_voice_noise",
                        lambda data, rate, ch: (called.append(data), (data, "neural"))[1])
    controller = IncrementalDenoiseController()
    frames = [b"\x59\x5a" * 480]
    raw = b"".join(frames)
    spool = str(send_env / "spool.pcm")
    open(spool, "wb").write(b"work")

    session = _send_session(controller, frames, worker=types.SimpleNamespace(abort=lambda: None),
                            spool=spool, stream=_FakeStream())

    def _hanging_drain():
        # Ignores stop/cancel past the 2 s wait, then self-cleans like the
        # real drain does (drain_cleaned + slot release).
        time.sleep(3.0)
        session["drain_cleaned"] = True
        controller.release_slot("m1")

    drain = threading.Thread(target=_hanging_drain, daemon=True)
    session["thread"] = drain
    drain.start()
    mw, announced = _keep_pending_mw(controller)
    mw.settings["general"]["voice_nr_moment"] = "send"
    panel = _SendPanel(mw, frames, session)
    panel.failed = []
    panel._mark_message_failed = lambda local_id: panel.failed.append(local_id)
    WX_CALLS.clear()
    vr.VoiceRecordingMixin._send_voice_message(panel, None)  # async worker
    _wait_wx_calls()  # the keep-pending mark lands after the real ~2 s wait
    _drive_wx_calls()
    # Timeout -> skip_posterior -> pending with the ORIGINAL audio, model
    # never called, slot RETAINED by the still-hanging drain.
    assert called == []
    assert announced == ["noise_reduction_kept_pending"]
    assert panel.failed == ["m1"]
    assert controller._slot_owner == "m1"
    msg = panel.registered
    pending_path = msg[RESEND_PATH_KEY]
    assert _wav_payload(pending_path) == raw
    # Retry while the native side is still alive: defers, no model call.
    mw2, announced2 = _retry_mw(controller)
    retry_panel = _RetryPanel(mw2, row=msg)
    WX_CALLS.clear()
    vnp.VoiceNrPendingMixin._retry_nr_pending(
        retry_panel, msg, "peer@s.whatsapp.net", pending_path)
    _wait_wx_calls()
    _drive_wx_calls()
    assert called == []
    assert announced2 == ["noise_reduction_kept_pending"]
    assert controller._slot_owner == "m1"
    # The drain really finishes and releases: the retry now processes the
    # SAME original bytes and hands off.
    drain.join(10)
    assert controller._slot_owner is None
    mw3, announced3 = _retry_mw(controller)
    retry_panel2 = _RetryPanel(mw3, row=msg)
    WX_CALLS.clear()
    vnp.VoiceNrPendingMixin._retry_nr_pending(
        retry_panel2, msg, "peer@s.whatsapp.net", pending_path)
    _wait_wx_calls()
    _drive_wx_calls()
    assert called == [raw]
    assert announced3 == ["resend_started"]
    os.unlink(mw3.message_queue.enqueued[0].audio_path)


def test_retry_refuses_a_changed_kept_file(send_env, monkeypatch):
    import core.neural_noise_reduction as nnr
    called = []
    monkeypatch.setattr(nnr, "reduce_voice_noise",
                        lambda *a: (called.append(a), (a[0], "neural"))[1])
    controller = IncrementalDenoiseController()
    mw, announced = _retry_mw(controller)
    raw = b"\x33\x34" * 480
    msg, kept = _kept_note(send_env, raw)
    # The file on disk no longer matches what was kept (replaced/synced).
    _write_wav(kept, b"\x99\x99" * 480)
    panel = _RetryPanel(mw, row=msg)
    WX_CALLS.clear()
    vnp.VoiceNrPendingMixin._retry_nr_pending(
        panel, msg, "peer@s.whatsapp.net", kept)
    _wait_wx_calls()
    _drive_wx_calls()
    # Nothing processed, nothing sent; the row stays pending and the user
    # hears the same "nothing to send again" as any unreadable resend.
    assert called == []
    assert mw.message_queue.enqueued == []
    assert panel.removed == []
    assert announced == ["resend_not_available"]
    assert os.path.exists(kept)
    assert not getattr(panel, "_nr_attempts", {})


def test_retry_sends_nothing_when_row_deleted_mid_processing(send_env, monkeypatch):
    import core.neural_noise_reduction as nnr
    gate = threading.Event()

    def _slow(data, rate, ch):
        gate.wait(10)
        return bytes(reversed(data)), "neural"

    monkeypatch.setattr(nnr, "reduce_voice_noise", _slow)
    controller = IncrementalDenoiseController()
    mw, announced = _retry_mw(controller)
    raw = b"\x35\x36" * 480
    msg, kept = _kept_note(send_env, raw)
    panel = _RetryPanel(mw, row=msg)
    WX_CALLS.clear()
    vnp.VoiceNrPendingMixin._retry_nr_pending(
        panel, msg, "peer@s.whatsapp.net", kept)
    # The user deletes the pending row while the model is still running.
    panel._sorted_messages.clear()
    gate.set()
    _wait_wx_calls()
    _drive_wx_calls()
    # Delete wins: no send, no row removal noise, no announcement; the
    # persisted processed copy is discarded — only the kept original stays.
    assert mw.message_queue.enqueued == []
    assert panel.removed == []
    assert announced == []
    # Every artifact the handoff staged (processed copy AND .msv playback
    # copy) is gone; only the valid kept original stays (contract 5).
    assert sorted(os.listdir(send_env / "voice_messages")) == \
        [os.path.basename(kept)]
    assert not getattr(panel, "_nr_attempts", {})


def test_retry_unreadable_file_stays_silent_when_row_already_gone(send_env, monkeypatch):
    controller = IncrementalDenoiseController()
    mw, announced = _retry_mw(controller)
    raw = b"\x37\x38" * 480
    msg, kept = _kept_note(send_env, raw)
    os.unlink(kept)  # row deleted (file gone with it) before the retry click
    panel = _RetryPanel(mw)  # no row: already removed from the list
    WX_CALLS.clear()
    vnp.VoiceNrPendingMixin._retry_nr_pending(
        panel, msg, "peer@s.whatsapp.net", kept)
    deadline = time.time() + 10
    while time.time() < deadline and not WX_CALLS \
            and not mw.message_queue.enqueued:
        time.sleep(0.05)
    _drive_wx_calls()
    assert mw.message_queue.enqueued == []
    assert announced == []
    assert not getattr(panel, "_nr_attempts", {})


# --- Fran 18:35/18:36: the kept-pending note also offers "Send without
# noise reduction" — an explicit per-action user choice, never automatic,
# and only on a note whose reduction FAILED (a first-time success sends
# normally, untouched by any of this). ---


def test_send_nr_pending_raw_sends_original_as_voice_note(send_env, monkeypatch):
    import core.neural_noise_reduction as nnr
    called = []
    monkeypatch.setattr(nnr, "reduce_voice_noise",
                        lambda *a: (called.append(a), (a[0], "neural"))[1])
    controller = IncrementalDenoiseController()
    mw, announced = _retry_mw(controller)
    raw = b"\x47\x48" * 480
    msg, kept = _kept_note(send_env, raw)
    msg["contextInfo"] = {"stanzaId": "q9", "participant": "peer@s.whatsapp.net",
                          "quotedMessage": {"conversation": "hi"},
                          "_quotedFromMe": False}
    panel = _RetryPanel(mw, row=msg)
    WX_CALLS.clear()
    vnp.VoiceNrPendingMixin._send_nr_pending_raw(
        panel, msg, "peer@s.whatsapp.net", kept)
    _wait_wx_calls()
    _drive_wx_calls()
    # The ORIGINAL bytes, as a voice note with its quote; no model touched.
    assert called == []
    assert len(mw.message_queue.enqueued) == 1
    pm = mw.message_queue.enqueued[0]
    assert _wav_payload(pm.audio_path) == raw
    assert pm.quoted["key"]["id"] == "q9"
    assert panel.registered[0]["message"]["audioMessage"]["ptt"] is True
    assert panel.removed == [{"m9"}]
    assert announced == ["resend_started"]
    assert not getattr(panel, "_nr_attempts", {})
    os.unlink(pm.audio_path)


def test_send_nr_pending_raw_refuses_a_changed_file(send_env, monkeypatch):
    controller = IncrementalDenoiseController()
    mw, announced = _retry_mw(controller)
    raw = b"\x49\x4a" * 480
    msg, kept = _kept_note(send_env, raw)
    _write_wav(kept, b"\x99\x99" * 480)  # no longer what was kept
    panel = _RetryPanel(mw, row=msg)
    WX_CALLS.clear()
    vnp.VoiceNrPendingMixin._send_nr_pending_raw(
        panel, msg, "peer@s.whatsapp.net", kept)
    _wait_wx_calls()
    _drive_wx_calls()
    assert mw.message_queue.enqueued == []
    assert announced == ["resend_not_available"]
    assert os.path.exists(kept)
    assert not getattr(panel, "_nr_attempts", {})


def test_raw_send_ignored_while_retry_attempt_in_flight(send_env, monkeypatch):
    # Attempt authority is shared across BOTH actions: one live action per
    # note, whatever its kind.
    import core.neural_noise_reduction as nnr
    gate = threading.Event()

    def _slow(data, rate, ch):
        gate.wait(10)
        return bytes(reversed(data)), "neural"

    monkeypatch.setattr(nnr, "reduce_voice_noise", _slow)
    controller = IncrementalDenoiseController()
    mw, announced = _retry_mw(controller)
    raw = b"\x4b\x4c" * 480
    msg, kept = _kept_note(send_env, raw)
    panel = _RetryPanel(mw, row=msg)
    WX_CALLS.clear()
    vnp.VoiceNrPendingMixin._retry_nr_pending(
        panel, msg, "peer@s.whatsapp.net", kept)
    vnp.VoiceNrPendingMixin._send_nr_pending_raw(
        panel, msg, "peer@s.whatsapp.net", kept)  # ignored: attempt in flight
    gate.set()
    _wait_wx_calls()
    _drive_wx_calls()
    assert len(mw.message_queue.enqueued) == 1
    # The winner is the RETRY: the payload is the processed audio.
    assert _wav_payload(mw.message_queue.enqueued[0].audio_path) == bytes(reversed(raw))
    os.unlink(mw.message_queue.enqueued[0].audio_path)


# --- Review 18:47 residual contracts: reservation (not probe), attempt
# token/context, representation before queue activation, processed-voice
# resend, full artifact cleanup, single stable read. ---


def test_retry_reservation_blocks_capture_and_second_offline(send_env, monkeypatch):
    # Contract 1 (18:47): the offline side is RESERVED under the
    # controller's lock from before the inference-lock wait — a capture
    # cannot take the slot between a probe and the model use. A second
    # OFFLINE reservation is granted: it shares the singleton, so it is
    # no second ONNX. Released when the reduction ends, before
    # staging/handoff.
    import core.neural_noise_reduction as nnr
    gate = threading.Event()
    entered = threading.Event()

    def _slow(data, rate, ch):
        entered.set()
        gate.wait(10)
        return bytes(reversed(data)), "neural"

    monkeypatch.setattr(nnr, "reduce_voice_noise", _slow)
    controller = IncrementalDenoiseController()
    mw, announced = _retry_mw(controller)
    raw = b"\x51\x52" * 480
    msg, kept = _kept_note(send_env, raw)
    panel = _RetryPanel(mw, row=msg)
    WX_CALLS.clear()
    vnp.VoiceNrPendingMixin._retry_nr_pending(
        panel, msg, "peer@s.whatsapp.net", kept)
    assert entered.wait(10)
    assert controller.acquire_slot("other-note", {"cancel": False}) is False
    assert controller.try_reserve_offline("other-offline") is True
    controller.release_offline("other-offline")
    assert controller.acquire_slot("other-note", {"cancel": False}) is False
    gate.set()
    _wait_wx_calls()
    _drive_wx_calls()
    assert announced == ["resend_started"]
    assert controller.acquire_slot("other-note", {"cancel": False}) is True
    controller.release_slot("other-note")
    os.unlink(mw.message_queue.enqueued[0].audio_path)


def test_second_attempt_blocked_for_rehydrated_same_id_row(send_env, monkeypatch):
    # Contract 2 (18:47): authority lives per NOTE on the panel, not on
    # the row dict — a rehydrated row with the same id carries no flag
    # yet cannot start a second attempt while one runs.
    import core.neural_noise_reduction as nnr
    gate = threading.Event()
    calls = []

    def _slow(data, rate, ch):
        calls.append(1)
        gate.wait(10)
        return bytes(reversed(data)), "neural"

    monkeypatch.setattr(nnr, "reduce_voice_noise", _slow)
    controller = IncrementalDenoiseController()
    mw, announced = _retry_mw(controller)
    raw = b"\x53\x54" * 480
    msg, kept = _kept_note(send_env, raw)
    panel = _RetryPanel(mw, row=msg)
    WX_CALLS.clear()
    vnp.VoiceNrPendingMixin._retry_nr_pending(
        panel, msg, "peer@s.whatsapp.net", kept)
    # Same id, NEW dict — as a rehydration from records/DB would build
    # it, WITHOUT any per-dict attempt state.
    rehydrated = {k: v for k, v in msg.items() if k != "_nr_retry_active"}
    vnp.VoiceNrPendingMixin._retry_nr_pending(
        panel, rehydrated, "peer@s.whatsapp.net", kept)
    vnp.VoiceNrPendingMixin._send_nr_pending_raw(
        panel, rehydrated, "peer@s.whatsapp.net", kept)
    gate.set()
    _wait_wx_calls()
    _drive_wx_calls()
    assert calls == [1]
    assert len(mw.message_queue.enqueued) == 1
    assert not getattr(panel, "_nr_attempts", {})
    os.unlink(mw.message_queue.enqueued[0].audio_path)


def test_destroy_without_emptying_list_invalidates_the_attempt(send_env, monkeypatch):
    # Contract 2 (18:47): destroy invalidates live attempts even when the
    # message list SURVIVES the destroy — the worker's UI dispatch cleans
    # its artifacts quietly instead of calling into dead controls.
    import core.neural_noise_reduction as nnr
    gate = threading.Event()

    def _slow(data, rate, ch):
        gate.wait(10)
        return bytes(reversed(data)), "neural"

    monkeypatch.setattr(nnr, "reduce_voice_noise", _slow)
    controller = IncrementalDenoiseController()
    mw, announced = _retry_mw(controller)
    raw = b"\x55\x56" * 480
    msg, kept = _kept_note(send_env, raw)
    panel = _RetryPanel(mw, row=msg)
    WX_CALLS.clear()
    vnp.VoiceNrPendingMixin._retry_nr_pending(
        panel, msg, "peer@s.whatsapp.net", kept)
    vnp.VoiceNrPendingMixin._nr_invalidate_attempts(panel)
    gate.set()
    _wait_wx_calls()
    _drive_wx_calls()
    assert mw.message_queue.enqueued == []
    assert announced == []
    assert panel._sorted_messages == [msg]  # the list was never emptied
    assert not getattr(panel, "_nr_attempts", {})
    assert sorted(os.listdir(send_env / "voice_messages")) == \
        [os.path.basename(kept)]


def test_representation_registered_before_queue_activation(send_env, monkeypatch):
    # Contract 3 (18:47): enqueue() wakes the sender — a SYNCHRONOUS echo
    # at activation time must already find the row and its virtual
    # registration, or it lands as a late duplicate pending row.
    import core.neural_noise_reduction as nnr
    monkeypatch.setattr(nnr, "reduce_voice_noise",
                        lambda data, rate, ch: (bytes(reversed(data)), "neural"))
    controller = IncrementalDenoiseController()
    mw, announced = _retry_mw(controller)
    raw = b"\x57\x58" * 480
    msg, kept = _kept_note(send_env, raw)
    panel = _RetryPanel(mw, row=msg)
    seen = []

    def _enqueue(pm):
        seen.append(pm.local_id in panel._outgoing_virtual_messages
                    and any(isinstance(m, dict) and m.get("_local_id") == pm.local_id
                            for m in panel._sorted_messages))
        mw.message_queue.enqueued.append(pm)

    mw.message_queue.enqueue = _enqueue
    WX_CALLS.clear()
    vnp.VoiceNrPendingMixin._retry_nr_pending(
        panel, msg, "peer@s.whatsapp.net", kept)
    _wait_wx_calls()
    _drive_wx_calls()
    assert seen == [True]
    assert announced == ["resend_started"]
    os.unlink(mw.message_queue.enqueued[0].audio_path)


def _processed_voice_row(send_env, raw, local_id="n1"):
    import hashlib
    from core.message_resend import RESEND_PATH_KEY
    os.makedirs(send_env / "voice_messages", exist_ok=True)
    path = str(send_env / "voice_messages" / (local_id + ".resend.wav"))
    _write_wav(path, raw)
    with open(path, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    row = {"_local_id": local_id, "_local_pending": False, "_send_failed": True,
           "_nr_processed_voice": True, RESEND_PATH_KEY: path,
           "_nr_pending_sha256": digest, "_nr_pending_size": os.path.getsize(path),
           "key": {"id": local_id, "fromMe": True,
                   "remoteJid": "peer@s.whatsapp.net"},
           "messageType": "audioMessage",
           "message": {"audioMessage": {"ptt": True, "seconds": 1}},
           "contextInfo": {"stanzaId": "q1", "participant": "peer@s.whatsapp.net",
                           "quotedMessage": {"conversation": "hi"},
                           "_quotedFromMe": False},
           "messageTimestamp": 100}
    return row, path


def test_resend_processed_voice_resends_voice_bytes(send_env, monkeypatch):
    # Contract 4 (18:47): a handoff-produced row that failed in transport
    # resends THOSE bytes as a voice note — ptt, quote — never re-reduced,
    # never a generic attachment; the failed row's file survives until the
    # new enqueue is accepted.
    import core.neural_noise_reduction as nnr
    called = []
    monkeypatch.setattr(nnr, "reduce_voice_noise",
                        lambda *a: (called.append(a), (a[0], "neural"))[1])
    controller = IncrementalDenoiseController()
    mw, announced = _retry_mw(controller)
    raw = b"\x59\x5a" * 480
    row, path = _processed_voice_row(send_env, raw)
    panel = _RetryPanel(mw, row=row)
    WX_CALLS.clear()
    vnp.VoiceNrPendingMixin._resend_processed_voice(
        panel, row, "peer@s.whatsapp.net", path)
    _wait_wx_calls()
    _drive_wx_calls()
    assert called == []  # never re-reduced
    assert len(mw.message_queue.enqueued) == 1
    pm = mw.message_queue.enqueued[0]
    assert _wav_payload(pm.audio_path) == raw
    assert pm.quoted["key"]["id"] == "q1"
    assert panel.registered[0]["message"]["audioMessage"]["ptt"] is True
    assert announced == ["resend_started"]
    os.unlink(pm.audio_path)


def test_resend_routes_processed_voice_to_voice_resend(send_env):
    # Contract 4 routing: "Try sending again" on a processed-voice row
    # goes to the voice resend, NEVER to the generic attachment path.
    row, path = _processed_voice_row(send_env, b"\x61\x62" * 480, local_id="n2")
    routed = []

    def _no_generic(*a, **k):
        raise AssertionError("generic attachment path must not run")

    panel = types.SimpleNamespace(
        conversation={"remoteJid": "peer@s.whatsapp.net"},
        _resend_processed_voice=lambda m, jid, p: routed.append((m, jid, p)),
        _send_attachment_file=_no_generic,
    )
    ma.MessageActionsMixin._on_menu_resend_message(panel, row)
    assert routed == [(row, "peer@s.whatsapp.net", path)]


def test_retry_processes_the_same_bytes_it_validated(send_env, monkeypatch):
    # Fingerprint (18:47): ONE stable read — a file swapped between two
    # opens can never validate as A and be processed as B.
    import core.neural_noise_reduction as nnr
    monkeypatch.setattr(nnr, "reduce_voice_noise",
                        lambda data, rate, ch: (data, "neural"))
    controller = IncrementalDenoiseController()
    mw, announced = _retry_mw(controller)
    raw_a = b"\x63\x64" * 480
    raw_b = b"\x65\x66" * 480
    msg, kept = _kept_note(send_env, raw_a)
    other = str(send_env / "other.wav")
    _write_wav(other, raw_b)
    real_open = open
    reads = [0]

    def _swapping_open(path, mode="r", *a, **k):
        if str(path) == kept and "r" in mode:
            reads[0] += 1
            if reads[0] > 1:
                path = other  # swapped AFTER the first read
        return real_open(path, mode, *a, **k)

    monkeypatch.setattr("builtins.open", _swapping_open)
    panel = _RetryPanel(mw, row=msg)
    WX_CALLS.clear()
    vnp.VoiceNrPendingMixin._retry_nr_pending(
        panel, msg, "peer@s.whatsapp.net", kept)
    _wait_wx_calls()
    _drive_wx_calls()
    assert len(mw.message_queue.enqueued) == 1
    assert _wav_payload(mw.message_queue.enqueued[0].audio_path) == raw_a
    os.unlink(mw.message_queue.enqueued[0].audio_path)


def test_moment_switch_blocks_offline_until_drain_exit(send_env, monkeypatch):
    # Contract 1 (18:47), record->send with the native session retained:
    # the offline pass never starts while the cancelled drain can still
    # hold its private model. Provenance: FAKE drain thread honoring the
    # documented exit contract, as in
    # test_switch_moment_cancel_cleanup_contract_fake_drain.
    import core.neural_noise_reduction as nnr
    entered = []
    drain_gate = threading.Event()

    def _reduce(data, rate, ch):
        entered.append(drain_gate.is_set())
        return bytes(reversed(data)), "neural"

    monkeypatch.setattr(nnr, "reduce_voice_noise", _reduce)
    controller = IncrementalDenoiseController()
    cleanup_done = _cleanup_signal(controller)
    frames = [b"\x67\x68" * 480]
    raw = b"".join(frames)
    spool = str(send_env / "spool.pcm")
    open(spool, "wb").write(b"work")

    class _Worker:
        def finish(self):
            raise AssertionError("finish must not run after the switch")

        def abort(self):
            pass

    session = _send_session(controller, frames, worker=_Worker(),
                            spool=spool, stream=_FakeStream())

    def _drain():
        # A drain whose native call stays blocked until released: only
        # then observes the cancel and follows the exit contract.
        drain_gate.wait(10)
        while not session["cancel"]:
            time.sleep(0.02)
        session["drain_cleaned"] = True
        try:
            os.unlink(session["spool"])
        except OSError:
            pass
        controller.release_slot(session["local_id"])

    drain = threading.Thread(target=_drain, daemon=True)
    drain.start()
    session["thread"] = drain

    mw = _FakeSendMW(controller)
    mw.settings["general"]["voice_nr_moment"] = "send"  # changed mid-flight
    panel = _SendPanel(mw, frames, session)
    send_thread = threading.Thread(
        target=lambda: vr.VoiceRecordingMixin._send_voice_message(panel, None),
        daemon=True)
    send_thread.start()
    # The send is parked at the barrier: no offline pass while the drain
    # can still hold its model.
    time.sleep(0.5)
    assert entered == []
    drain_gate.set()
    assert mw.message_queue.event.wait(10)
    send_thread.join(10)
    assert entered == [True]  # offline ran only AFTER the drain exited
    assert _wav_payload(mw.message_queue.enqueued[0].audio_path) == bytes(reversed(raw))
    assert cleanup_done.wait(5)
    drain.join(5)


# --- Review 18:59 residuals: the NORMAL send in the same exclusion,
# converter-temp cleanup, and the old handoff row's media cache. ---


def test_normal_send_uses_gate_while_capture_holds_slot(send_env, monkeypatch):
    # Residual 1 (18:59): the NORMAL send path takes the same reservation
    # as the retry — with the slot held by a capture, the offline neural
    # pass is never attempted; the published gate applies instead.
    import core.neural_noise_reduction as nnr
    called = []
    monkeypatch.setattr(nnr, "reduce_voice_noise",
                        lambda *a: (called.append(a), (a[0], "neural"))[1])
    controller = IncrementalDenoiseController()
    assert controller.acquire_slot("other-note", {"cancel": False})
    frames = [b"\x69\x6a" * 480]
    mw = _FakeSendMW(controller)
    mw.settings["general"]["voice_nr_moment"] = "send"
    panel = _SendPanel(mw, frames, None)
    WX_CALLS.clear()
    vr.VoiceRecordingMixin._send_voice_message(panel, None)
    assert mw.message_queue.event.wait(10)
    assert called == []  # no second ONNX next to the capture's model
    controller.release_slot("other-note")


def test_normal_send_stays_pending_while_capture_holds_slot(send_env, monkeypatch):
    # Same exclusion with the fourth option: keep_pending keeps the note
    # pending instead of running the offline pass next to the capture.
    import core.neural_noise_reduction as nnr
    called = []
    monkeypatch.setattr(nnr, "reduce_voice_noise",
                        lambda *a: (called.append(a), (a[0], "neural"))[1])
    controller = IncrementalDenoiseController()
    assert controller.acquire_slot("other-note", {"cancel": False})
    frames = [b"\x6b\x6c" * 480]
    mw, announced = _keep_pending_mw(controller)
    mw.settings["general"]["voice_nr_moment"] = "send"
    panel = _SendPanel(mw, frames, None)
    panel.failed = []
    panel._mark_message_failed = lambda local_id: panel.failed.append(local_id)
    WX_CALLS.clear()
    vr.VoiceRecordingMixin._send_voice_message(panel, None)
    deadline = time.time() + 10
    while time.time() < deadline and not WX_CALLS:
        time.sleep(0.05)
    assert WX_CALLS, "the note was never kept pending"
    _drive_wx_calls()
    assert called == []
    assert announced == ["noise_reduction_kept_pending"]
    assert panel.failed == [panel.registered["_local_id"]]
    controller.release_slot("other-note")


def test_capture_cannot_acquire_during_normal_offline_send(send_env, monkeypatch):
    # The other direction (residual 1): while the NORMAL offline pass
    # holds the reservation, a new capture cannot take the slot; it can
    # once the reduction ended.
    import core.neural_noise_reduction as nnr
    gate = threading.Event()
    entered = threading.Event()

    def _slow(data, rate, ch):
        entered.set()
        gate.wait(10)
        return bytes(reversed(data)), "neural"

    monkeypatch.setattr(nnr, "reduce_voice_noise", _slow)
    controller = IncrementalDenoiseController()
    frames = [b"\x6d\x6e" * 480]
    mw = _FakeSendMW(controller)
    mw.settings["general"]["voice_nr_moment"] = "send"
    panel = _SendPanel(mw, frames, None)
    WX_CALLS.clear()
    send_thread = threading.Thread(
        target=lambda: vr.VoiceRecordingMixin._send_voice_message(panel, None),
        daemon=True)
    send_thread.start()
    assert entered.wait(10)
    assert controller.acquire_slot("new-note", {"cancel": False}) is False
    gate.set()
    send_thread.join(10)
    assert mw.message_queue.event.wait(10)
    assert controller.acquire_slot("new-note", {"cancel": False}) is True
    controller.release_slot("new-note")


def test_handoff_cleans_ogg_temp_when_read_fails(send_env, monkeypatch):
    # Residual 2 (18:59): a converter temp OGG whose READ fails is deleted
    # too — never left behind while the send continues with the WAV.
    import core.neural_noise_reduction as nnr
    monkeypatch.setattr(nnr, "reduce_voice_noise",
                        lambda data, rate, ch: (bytes(reversed(data)), "neural"))
    controller = IncrementalDenoiseController()
    mw, announced = _retry_mw(controller)
    ogg_left = str(send_env / "voice_messages" / "leftover.ogg")
    converted = []
    read_attempts = []

    def _convert(wav_path):
        with open(ogg_left, "wb") as fh:
            fh.write(b"OggS-fake")
        converted.append(ogg_left)
        return ogg_left

    mw._convert_wav_to_ogg = _convert
    real_open = open

    def _failing_open(path, mode="r", *a, **k):
        if str(path) == ogg_left and "r" in mode:
            read_attempts.append(path)
            raise OSError("read error")
        return real_open(path, mode, *a, **k)

    raw = b"\x6f\x70" * 480
    msg, kept = _kept_note(send_env, raw)
    monkeypatch.setattr("builtins.open", _failing_open)
    panel = _RetryPanel(mw, row=msg)
    WX_CALLS.clear()
    vnp.VoiceNrPendingMixin._retry_nr_pending(
        panel, msg, "peer@s.whatsapp.net", kept)
    _wait_wx_calls()
    _drive_wx_calls()
    assert announced == ["resend_started"]
    # The mono converter ran and its output's read genuinely failed — the
    # cleanup under test is not a vacuous pass.
    assert converted == [ogg_left]
    assert read_attempts, "the OGG read never happened: no coverage"
    assert not os.path.exists(ogg_left)
    os.unlink(mw.message_queue.enqueued[0].audio_path)


def test_second_handoff_drops_the_first_rows_media_cache(send_env, monkeypatch):
    # Residual 3 (18:59): after a processed-voice resend, the FIRST
    # handoff row's .msv dies with its row; every artifact of the NEW row
    # (its valid owned files) stays.
    import core.neural_noise_reduction as nnr
    from core.message_resend import RESEND_PATH_KEY
    monkeypatch.setattr(nnr, "reduce_voice_noise",
                        lambda data, rate, ch: (bytes(reversed(data)), "neural"))
    controller = IncrementalDenoiseController()
    mw, announced = _retry_mw(controller)
    raw = b"\x71\x72" * 480
    msg, kept = _kept_note(send_env, raw)
    panel = _RetryPanel(mw, row=msg)
    WX_CALLS.clear()
    # First handoff: kept pending -> processed row.
    vnp.VoiceNrPendingMixin._retry_nr_pending(
        panel, msg, "peer@s.whatsapp.net", kept)
    _wait_wx_calls()
    _drive_wx_calls()
    assert announced == ["resend_started"]
    first_row = panel.registered[0]
    first_id = first_row["_local_id"]
    first_resend = first_row[RESEND_PATH_KEY]
    first_msv = str(send_env / "voice_messages" / (first_id + ".msv"))
    assert os.path.exists(first_resend) and os.path.exists(first_msv)
    # Transport failure on the first row (no claim about the queue's
    # cleanup moment is made — Milo withdrew it; we only assert the file
    # is still there for the manual resend).
    first_row["_send_failed"] = True
    first_row["_local_pending"] = False
    # Second handoff: resend of the processed voice note.
    WX_CALLS.clear()
    vnp.VoiceNrPendingMixin._resend_processed_voice(
        panel, first_row, "peer@s.whatsapp.net", first_resend)
    _wait_wx_calls()
    _drive_wx_calls()
    assert announced == ["resend_started", "resend_started"]
    assert not os.path.exists(first_resend)
    assert not os.path.exists(first_msv)
    second_row = panel.registered[1]
    second_id = second_row["_local_id"]
    assert sorted(os.listdir(send_env / "voice_messages")) == \
        sorted([second_id + ".resend.wav", second_id + ".msv"])
    os.unlink(second_row[RESEND_PATH_KEY])
    os.unlink(str(send_env / "voice_messages" / (second_id + ".msv")))


def test_menu_offers_send_raw_only_on_nr_pending_notes():
    # The second manual action lives in the message menu, gated on the
    # keep-pending marker — not on ordinary failed attachments. (Source
    # check: the menu module imports pyperclip, unavailable in this
    # sandbox — the same wx-free constraint as the rest of the UI.)
    import io as _io
    import os.path as _osp
    src = _io.open(_osp.join(_osp.dirname(vr.__file__), "message_menu.py"),
                   encoding="utf-8").read()
    assert 'msg.get("_nr_pending_retry")' in src
    assert "nr_pending_send_raw" in src
    assert "_on_menu_send_nr_pending_raw" in src


def test_ordered_send_checks_revoke_when_delayed_action_is_released(send_env, monkeypatch):
    """A later prepared note can be deleted while an earlier ticket is pending."""
    from core.ordered_dispatch import OrderedDispatcher
    import core.audio_processing as ap
    dispatcher = OrderedDispatcher()
    monkeypatch.setattr(vr, 'voice_dispatcher', dispatcher)
    monkeypatch.setattr(ap, 'apply_noise_gate', lambda data, rate, ch, **kw: data)
    jid = 'peer@s.whatsapp.net'
    previous = dispatcher.reserve(jid)
    controller = IncrementalDenoiseController()
    frames = [b'\x01\x02' * 480]
    session = _send_session(controller, frames, worker=None,
                            spool=str(send_env / 'unused.pcm'),
                            stream=_FakeStream())
    session['basic_only'] = True
    controller.release_slot(session['local_id'])
    mw = _FakeSendMW(controller)
    panel = _SendPanel(mw, frames, session)
    complete = threading.Event()
    original_complete = dispatcher.complete

    def finished(key, ticket, action):
        original_complete(key, ticket, action)
        complete.set()

    monkeypatch.setattr(dispatcher, 'complete', finished)
    vr.VoiceRecordingMixin._send_voice_message(panel, None)
    assert complete.wait(10)
    assert not mw.message_queue.enqueued
    assert controller.revoke(session['local_id'])
    original_complete(jid, previous, None)
    assert not mw.message_queue.enqueued
    assert not controller.is_preparing(session['local_id'])
