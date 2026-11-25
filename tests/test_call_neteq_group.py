"""Group calls: one NetEq per sender, mixed on the device callback, and the
fallbacks that keep the senders from interleaving in the legacy reservoir."""
from types import SimpleNamespace

import numpy as np
import pytest

from core.call_audio import _OutputJitterBuffer
from core.call_audio_resampling import PerSenderResampler, StreamingMonoResampler
from core.call_neteq_buffer import (MAX_STREAMS, STREAM_IDLE_SECONDS, NetEqOutputBuffer,
                                    timed_ingress_limit)


class Native:
    def __init__(self, level):
        self.level = level
        self.packets = []
        self.closed = False
        self.fail = False

    def insert(self, ts, samples):
        self.packets.append((ts, samples.copy()))

    def render(self, out):
        out[:] = self.level
        return not self.fail

    def close(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def group_buffer(levels=(.25,), available=True):
    created = []
    def create(rate):
        native = Native(levels[min(len(created), len(levels) - 1)])
        created.append(native)
        return native
    legacy = _OutputJitterBuffer(48000, neonize=True)
    clock = Clock()
    result = NetEqOutputBuffer(legacy, 48000, clock=clock,
                               library_loader=lambda: SimpleNamespace(create=create) if available else None)
    return result, created, legacy, clock


def frame(value=0.):
    return np.full(2880, value, dtype=np.float32)


def test_each_group_sender_gets_its_own_neteq_on_its_own_timeline():
    b, created, legacy, _ = group_buffer()
    b.push_timed(frame(), 1000, 16000, 11, 48000, mixed=True)
    b.push_timed(frame(), 70000, 16000, 22, 48000, mixed=True)
    b.push_timed(frame(), 1960, 16000, 11, 48000, mixed=True)
    b.push_timed(frame(), 70960, 16000, 22, 48000, mixed=True)
    assert len(created) == 2 and not any(n.closed for n in created)
    # Each sender's origin is its own; 960 samples at 16 kHz are 2880 at 48 kHz.
    assert [p[0] for p in created[0].packets] == [0, 2880]
    assert [p[0] for p in created[1].packets] == [0, 2880]
    assert b.stream_count == 2 and legacy.buffered_samples == 0


def test_device_callback_sums_the_senders_and_clips():
    b, _, _, _ = group_buffer(levels=(.25, .5))
    b.push_timed(frame(), 0, 16000, 11, 48000, mixed=True)
    b.push_timed(frame(), 0, 16000, 22, 48000, mixed=True)
    out = np.zeros((137, 2), dtype=np.float32)
    assert b.fill(out[:, 0])  # a strided device view, as render_output passes
    assert np.allclose(out[:, 0], .75) and not np.any(out[:, 1])
    b, _, _, _ = group_buffer(levels=(.75,))
    for ssrc in (1, 2, 3):
        b.push_timed(frame(), 0, 16000, ssrc, 48000, mixed=True)
    out = np.zeros(480, dtype=np.float32)
    assert b.fill(out) and np.all(out == 1.0)


def test_one_sender_changing_codec_clock_resets_only_its_own_neteq():
    b, created, _, _ = group_buffer()
    b.push_timed(frame(), 0, 16000, 11, 48000, mixed=True)
    b.push_timed(frame(), 0, 16000, 22, 48000, mixed=True)
    b.push_timed(frame(), 960, 16000, 22, 16000, mixed=True)
    assert not created[0].closed and created[1].closed and len(created) == 3


def test_a_sender_that_stops_is_closed_by_the_worker_after_it_falls_idle():
    b, created, _, clock = group_buffer()
    b.push_timed(frame(), 0, 16000, 11, 48000, mixed=True)
    b.push_timed(frame(), 0, 16000, 22, 48000, mixed=True)
    clock.now += STREAM_IDLE_SECONDS / 2
    b.push_timed(frame(), 960, 16000, 22, 48000, mixed=True)
    assert not created[0].closed
    clock.now += STREAM_IDLE_SECONDS / 2 + .1
    b.push_timed(frame(), 1920, 16000, 22, 48000, mixed=True)
    assert created[0].closed and not created[1].closed and b.stream_count == 1


def test_past_the_stream_limit_the_least_recently_heard_sender_is_closed():
    b, created, _, clock = group_buffer()
    for ssrc in range(MAX_STREAMS):
        clock.now += .01
        b.push_timed(frame(), 0, 16000, ssrc, 48000, mixed=True)
    clock.now += .01
    b.push_timed(frame(), 960, 16000, 0, 48000, mixed=True)  # sender 0 heard again
    clock.now += .01
    b.push_timed(frame(), 0, 16000, 999, 48000, mixed=True)
    assert b.stream_count == MAX_STREAMS
    assert created[1].closed and not created[0].closed


def test_a_direct_frame_after_a_group_keeps_one_sender():
    b, created, _, _ = group_buffer()
    b.push_timed(frame(), 0, 16000, 11, 48000, mixed=True)
    b.push_timed(frame(), 0, 16000, 22, 48000, mixed=True)
    b.push_timed(frame(), 960, 16000, 11, 48000)
    assert not created[0].closed and created[1].closed and b.stream_count == 1


def test_group_senders_never_enter_the_single_source_reservoir_without_neteq():
    b, _, legacy, _ = group_buffer(available=False)
    b.push_timed(frame(.5), 0, 16000, 11, 48000, mixed=True)
    b.push_timed(frame(.5), 0, 16000, 22, 48000, mixed=True)
    assert legacy.buffered_samples == 0
    b.push_timed(frame(.5), 0, 16000, 11, 48000)  # direct audio keeps the reservoir
    assert legacy.buffered_samples == 2880


def test_a_failed_neteq_drops_group_audio_instead_of_interleaving_it():
    b, created, legacy, _ = group_buffer()
    b.push_timed(frame(), 0, 16000, 11, 48000, mixed=True)
    created[0].fail = True
    out = np.ones(137, dtype=np.float32)
    assert b.fill(out) and not np.any(out) and b.native_failed
    b.release_failed()
    assert created[0].closed
    b.push_timed(frame(.5), 960, 16000, 11, 48000, mixed=True)
    assert legacy.buffered_samples == 0


def test_untimed_mixer_audio_closes_every_sender():
    b, created, legacy, _ = group_buffer()
    b.push_timed(frame(), 0, 16000, 11, 48000, mixed=True)
    b.push_timed(frame(), 0, 16000, 22, 48000, mixed=True)
    b.push(frame())
    assert all(n.closed for n in created) and legacy.buffered_samples == 2880


def test_timed_ingress_holds_480_ms_per_sender():
    assert timed_ingress_limit(0, False) == 8
    assert timed_ingress_limit(5, False) == 8
    assert timed_ingress_limit(0, True) == 8
    assert timed_ingress_limit(3, True) == 32
    assert timed_ingress_limit(10**6, True) == 8 * MAX_STREAMS


def test_each_sender_keeps_its_own_fir_history():
    rng = np.random.default_rng(1)
    a = [rng.standard_normal(960).astype(np.float32) for _ in range(4)]
    b = [rng.standard_normal(960).astype(np.float32) for _ in range(4)]
    shared = PerSenderResampler()
    alone_a, alone_b = StreamingMonoResampler(), StreamingMonoResampler()
    for x, y in zip(a, b):
        got_a = shared.process(11, (16000, 48000), x, 16000, 48000)
        got_b = shared.process(22, (16000, 48000), y, 16000, 48000)
        assert np.array_equal(got_a, alone_a.process(x, 16000, 48000))
        assert np.array_equal(got_b, alone_b.process(y, 16000, 48000))


def test_a_sender_changing_clock_starts_a_fresh_fir_history():
    shared = PerSenderResampler()
    x = np.ones(960, dtype=np.float32)
    shared.process(11, (16000, 48000), x, 16000, 48000)
    fresh = StreamingMonoResampler().process(x, 16000, 48000)
    assert np.array_equal(shared.process(11, (16000, 16000), x, 16000, 48000), fresh)


def test_the_least_recently_heard_fir_history_is_dropped_past_the_limit():
    shared = PerSenderResampler(limit=2)
    x = np.ones(960, dtype=np.float32)
    shared.process(1, (16000, 48000), x, 16000, 48000)
    shared.process(2, (16000, 48000), x, 16000, 48000)
    shared.process(1, (16000, 48000), x, 16000, 48000)
    shared.process(3, (16000, 48000), x, 16000, 48000)
    assert list(shared._senders) == [1, 3]


@pytest.fixture
def session():
    from core.call_audio import CallAudioConfig, CallAudioSession
    from tests.test_call_audio_session import _SoundDevice
    result = CallAudioSession(None, CallAudioConfig(session='account', neonize_audio=True),
                              sounddevice_module=_SoundDevice())
    result.set_media_epoch('a'*32)
    yield result
    result.stop(notify_bridge=False)


def test_session_queue_holds_more_timed_frames_for_a_group(session):
    session._output_buffer, _, _, _ = group_buffer()
    for ssrc in (1, 2, 3):
        session._output_buffer.push_timed(frame(), 0, 16000, ssrc, 48000, mixed=True)
    for i in range(40):
        session.enqueue_remote_audio(b'\x00\x00'*960, 16000, media_epoch='a'*32,
                                     timeline=(i*960, i % 3 + 1, 48000, True))
    assert session._output_queue.qsize() == timed_ingress_limit(3, True)
    item = session._output_queue.get_nowait()
    assert item.mixed and item.epoch == 'a'*32


@pytest.mark.parametrize('cause', ['no_neteq', 'live_translation'])
def test_session_drops_group_audio_and_releases_the_native_transport(session, cause):
    from core.call_media_ownership import TimedPlaybackPCM
    b, created, legacy, _ = group_buffer(available=cause != 'no_neteq')
    session._output_buffer = b
    if cause == 'live_translation':
        session._live_translation = (SimpleNamespace(stop=lambda: None),)
    released = []
    session._transport = SimpleNamespace(fallback=lambda: released.append(True))
    item = TimedPlaybackPCM(b'', 16000, 'a'*32, 0, 11, 48000, True)
    session._push_group_sender(item, frame(.5), 16000, 48000)
    assert not created and legacy.buffered_samples == 0
    session._log_output_underruns()
    assert released == [True]
    session._log_output_underruns()
    assert released == [True]  # once per dropped frame, not on every pass


def test_session_feeds_each_group_sender_through_its_own_neteq(session):
    from core.call_media_ownership import TimedPlaybackPCM
    b, created, _, _ = group_buffer()
    session._output_buffer = b
    for ssrc in (11, 22):
        item = TimedPlaybackPCM(b'', 16000, 'a'*32, 0, ssrc, 48000, True)
        session._push_group_sender(item, np.zeros(960, dtype=np.float32), 16000, 48000)
    assert len(created) == 2 and all(len(n.packets) == 1 for n in created)
    assert created[0].packets[0][1].size == 2880  # converted to the device rate


def test_compiled_neteq_mixes_two_senders_on_one_device_clock():
    from pathlib import Path
    from core.call_neteq import NetEqLibrary
    dll = Path(__file__).parents[1]/'neonize_bridge/neonize_build/dist/neonize/winzapp_call_neteq.dll'
    if not dll.is_file():
        pytest.skip('NetEq component is built by the Neonize job')
    library = NetEqLibrary(dll)
    legacy = _OutputJitterBuffer(48000, neonize=True)
    b = NetEqOutputBuffer(legacy, 48000, library_loader=lambda: library)
    t = np.arange(2880 * 6, dtype=np.float32) / 48000
    tones = {11: .2 * np.sin(2*np.pi*300*t), 22: .2 * np.sin(2*np.pi*1100*t)}
    try:
        for n in range(6):
            for ssrc, tone in tones.items():
                b.push_timed(tone[n*2880:(n+1)*2880].astype(np.float32), n*960, 16000, ssrc, 48000, mixed=True)
        assert b.stream_count == 2
        out = np.concatenate([np.zeros(480, dtype=np.float32) for _ in range(30)])
        for i in range(30):
            block = out[i*480:(i+1)*480]
            assert b.fill(block)
        spectrum = np.abs(np.fft.rfft(out[-9600:]))
        freqs = np.fft.rfftfreq(9600, 1/48000)
        level = lambda f: spectrum[np.argmin(np.abs(freqs - f))]
        noise = np.median(spectrum)
        assert level(300) > 50 * noise and level(1100) > 50 * noise  # both senders heard
        assert np.max(np.abs(out)) <= 1.0 and not b.native_failed
    finally:
        b.reset()
