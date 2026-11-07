"""Late/failed fake output callbacks; no PortAudio, devices or worker threads."""
import numpy as np
import pytest

from core.call_audio import CALL_FRAME_SAMPLES, CallAudioConfig, CallAudioSession
from tests.test_call_audio_session import _Socket, _SoundDevice


def opened(*, echo=True):
    device = _SoundDevice()
    session = CallAudioSession(_Socket(), CallAudioConfig(session="test", echo_cancellation=echo), sounddevice_module=device)
    session._output_stream, session._output_rate = session._open_output_stream()
    session._output_buffer.configure(session._output_rate)
    session._output_stream.start()
    return session, device, session._output_stream


def broken_close(stream):
    def fail():
        raise RuntimeError("fake device refuses close")
    stream.stop = stream.close = fail


def prime(session):
    session._output_buffer.push(np.full(session._output_rate // 5, 0.25, dtype=np.float32))


@pytest.mark.parametrize("new_rate", [48000, 44100])
def test_failed_close_old_callback_cannot_consume_replacement(new_rate):
    session, device, old = opened()
    broken_close(old)
    session._candidate_rates = lambda _device: [new_rate]
    session._reopen_output_exclusive()
    prime(session)
    before = session._output_buffer.buffered_samples
    tap_size = len(session._echo_reference_tap)
    assert not np.any(old.render())
    assert session._output_buffer.buffered_samples == before
    assert len(session._echo_reference_tap) == tap_size
    assert np.any(session._output_stream.render())
    assert session._output_buffer.buffered_samples == before - CALL_FRAME_SAMPLES
    assert session._echo_reference_tap[-1][1] == new_rate


def test_stop_invalidates_callback_even_when_driver_cannot_close():
    session, device, old = opened()
    broken_close(old)
    session.stop(notify_bridge=False)
    prime(session)
    before = session._output_buffer.buffered_samples
    assert not np.any(old.render())
    assert session._output_buffer.buffered_samples == before
    assert not session._echo_reference_tap


def test_failed_start_invalidates_both_old_and_new_callbacks():
    session, device, old = opened()
    broken_close(old)
    factory = device.OutputStream
    def create(**kwargs):
        stream = factory(**kwargs)
        broken_close(stream)
        stream.start = lambda: (_ for _ in ()).throw(RuntimeError("fake start"))
        return stream
    device.OutputStream = create
    with pytest.raises(RuntimeError, match="fake start"):
        session._reopen_output_exclusive()
    prime(session)
    before = session._output_buffer.buffered_samples
    assert not np.any(old.render())
    assert not np.any(device.output_streams[-1][1].render())
    assert session._output_buffer.buffered_samples == before


def test_reopen_between_callback_entry_and_buffer_fill_is_fenced(monkeypatch):
    session, device, old = opened()
    original_fill = session._output_buffer.fill
    entered = False
    def delayed_fill(out, **kwargs):
        nonlocal entered
        if not entered:
            entered = True
            session._reopen_output_exclusive()
            prime(session)
        return original_fill(out, **kwargs)
    monkeypatch.setattr(session._output_buffer, "fill", delayed_fill)
    assert not np.any(old.render())
    assert session._output_buffer.buffered_samples > CALL_FRAME_SAMPLES
    assert not session._echo_reference_tap
    assert np.any(session._output_stream.render())


def test_callback_past_fill_cannot_publish_reference_to_new_tap(monkeypatch):
    session, device, old = opened()
    prime(session)
    original_fill = session._output_buffer.fill
    entered = False
    def paused_after_fill(out, **kwargs):
        nonlocal entered
        result = original_fill(out, **kwargs)
        if not entered:
            entered = True
            session._reopen_output_exclusive()
            prime(session)
        return result
    monkeypatch.setattr(session._output_buffer, "fill", paused_after_fill)
    assert np.any(old.render())  # Already rendered before retirement.
    assert session._output_buffer.buffered_samples == session._output_rate // 5
    assert not session._echo_reference_tap
    assert np.any(session._output_stream.render())
    assert len(session._echo_reference_tap) == 1


class BrokenTap:
    def append(self, _chunk):
        raise RuntimeError("fake optional reference")


class BrokenCopy(np.ndarray):
    def copy(self, *args, **kwargs):
        raise RuntimeError("fake reference allocation")


@pytest.mark.parametrize("failure", ["append", "copy"])
def test_optional_reference_failure_does_not_erase_playback(failure):
    session, device, stream = opened()
    prime(session)
    if failure == "append":
        session._echo_reference_tap = BrokenTap()
    out = np.full((CALL_FRAME_SAMPLES, 1), 7, dtype=np.float32)
    if failure == "copy":
        out = out.view(BrokenCopy)
    session._on_output_frames(out, CALL_FRAME_SAMPLES, None, None)
    assert np.any(out != 0)
    assert not np.any(out == 7)


def test_required_fill_failure_still_writes_silence(monkeypatch):
    session, device, stream = opened()
    monkeypatch.setattr(session._output_buffer, "fill", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("fake fill")))
    assert not np.any(stream.render())


def test_disabled_echo_never_touches_reference_tap():
    session, device, stream = opened(echo=False)
    prime(session)
    session._echo_reference_tap = BrokenTap()
    out = np.full((CALL_FRAME_SAMPLES, 1), 7, dtype=np.float32)
    session._on_output_frames(out, CALL_FRAME_SAMPLES, None, None)
    assert np.any(out != 0) and not np.any(out == 7)


def test_real_stream_callback_keeps_output_when_reference_copy_fails():
    session, device, stream = opened()
    prime(session)
    out = np.full((CALL_FRAME_SAMPLES, 1), 7, dtype=np.float32).view(BrokenCopy)
    stream.callback(out, CALL_FRAME_SAMPLES, None, None)
    assert np.any(out != 0) and not np.any(out == 7)


def test_real_stream_callback_keeps_output_when_reference_append_fails(monkeypatch):
    session, device, stream = opened()
    import core.call_audio as audio
    monkeypatch.setattr(audio, "deque", lambda **kwargs: BrokenTap())
    session._reopen_output_exclusive()
    prime(session)
    assert np.any(session._output_stream.render())


def test_initial_start_failure_retires_its_callback():
    device = _SoundDevice()
    factory = device.OutputStream
    def create(**kwargs):
        stream = factory(**kwargs)
        broken_close(stream)
        stream.start = lambda: (_ for _ in ()).throw(RuntimeError("fake initial start"))
        return stream
    device.OutputStream = create
    session = CallAudioSession(_Socket(), CallAudioConfig(session="test"), sounddevice_module=device)
    with pytest.raises(RuntimeError, match="fake initial start"):
        session.start_output_only()
    prime(session)
    assert not np.any(device.output_streams[0][1].render())
    assert session._output_buffer.buffered_samples == session._output_rate // 5


def test_failed_candidate_callback_cannot_consume_successful_fallback():
    device = _SoundDevice()
    factory = device.OutputStream
    def create(**kwargs):
        stream = factory(**kwargs)
        if len(device.output_streams) == 1:
            raise RuntimeError("fake rejected candidate")
        return stream
    device.OutputStream = create
    session = CallAudioSession(_Socket(), CallAudioConfig(session="test"), sounddevice_module=device)
    session._output_stream, session._output_rate = session._open_output_stream()
    session._output_buffer.configure(session._output_rate)
    prime(session)
    before = session._output_buffer.buffered_samples
    assert not np.any(device.output_streams[0][1].render())
    assert session._output_buffer.buffered_samples == before
    assert np.any(session._output_stream.render())


def test_driver_close_calls_callback_without_waiting_for_output_lock():
    session, device, stream = opened()
    prime(session)
    rendered = []
    stream.close = lambda: rendered.append(stream.render())
    session._reopen_output_exclusive()
    assert len(rendered) == 1 and not np.any(rendered[0])


def test_stop_retires_replacement_opened_during_input_close():
    session, device, old = opened()
    class ReopenOnInputClose:
        def stop(self):
            pass
        def close(self):
            session._reopen_output_exclusive()
            broken_close(session._output_stream)
    session._input_stream = ReopenOnInputClose()
    session.stop(notify_bridge=False)
    replacement = device.output_streams[-1][1]
    assert replacement is not old
    prime(session)
    before = session._output_buffer.buffered_samples
    assert not np.any(replacement.render())
    assert session._output_buffer.buffered_samples == before
