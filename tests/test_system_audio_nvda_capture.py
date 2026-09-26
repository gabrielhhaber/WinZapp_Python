"""Synthetic three-source tests through the real worker; no devices or GUI."""
import inspect
import struct
import threading
import unittest

from core.system_audio_capture import AudioPacket, SystemAudioRecorder, TimelineMixer
from tests.test_system_audio_volume import ControlledBackend, ControlledStream, QuietDiagnostics


class NvdaTimelineTests(unittest.TestCase):
    def test_third_source_retains_timing_guards_and_bounded_late_recovery(self):
        packet = lambda stamp, count=80: AudioPacket(stamp, struct.pack('<f', .5) * count)
        for violation, packets in (
                ('timestamp', [packet(100_000), packet(100_000)]),
                ('overlap', [packet(0), packet(50_000), packet(100_000)]),
                ('future', [packet(30_000_000)]),
                ('malformed', [AudioPacket(0, b'x')])):
            with self.subTest(violation=violation):
                mix = TimelineMixer(8000, 1, 0, source_count=3)
                with self.assertRaises(ValueError):
                    for item in packets:
                        mix.add(2, item)
        mix = TimelineMixer(8000, 1, 0, source_count=3, late_tolerance_seconds=.5)
        mix.render_until(100_000)
        mix.add(2, packet(50_000))
        self.assertEqual(mix.render_until(150_000), struct.pack('<h', 8192) * 40)
        self.assertEqual(mix.late_packets, [0, 0, 1])
        self.assertEqual(mix.late_frames, [0, 0, 40])
        mix.render_until(10_000_000)
        with self.assertRaises(ValueError):
            mix.add(2, packet(200_000))

    def test_three_sources_clip_and_ring_wrap_without_replaying_samples(self):
        mix = TimelineMixer(8000, 2, 0, capacity_seconds=.02, source_count=3)
        for batch in range(30):
            stamp = batch * 100_000
            for source in range(3):
                mix.add(source, AudioPacket(stamp, struct.pack('<2f', 1, -1) * 80))
            self.assertEqual(mix.render_until(stamp + 100_000),
                             struct.pack('<2h', 32767, -32768) * 80)
        self.assertEqual(mix.render_until(3_100_000), bytes(80 * 4))

    def test_three_sources_keep_half_gain_and_independent_history(self):
        self.assertIn('source_count', inspect.signature(TimelineMixer).parameters)
        mix = TimelineMixer(8000, 2, 0, source_count=3)
        for source, signal in enumerate(((.25, 0), (0, .5), (.125, -.125))):
            mix.add(source, AudioPacket(0, struct.pack('<2f', *signal) * 80))
        self.assertEqual(mix.render_until(100_000), struct.pack('<2h', 6144, 6144) * 80)
        self.assertEqual(mix.late_packets, [0, 0, 0])
        for source in (-1, 3):
            with self.assertRaises(ValueError):
                mix.add(source, AudioPacket(100_000, struct.pack('<2f', 0, 0)))
        for count in (0, -1, 2.5, True):
            with self.assertRaises(ValueError):
                TimelineMixer(8000, 2, 0, source_count=count)


class NvdaStream(ControlledStream):
    def read_packets(self):
        if self.error is not None:
            raise self.error
        return super().read_packets()

    def reset(self):
        super().reset()
        self.backend.log.append((self.kind, 'reset'))
        self.backend.batch.pop(self.kind, None)


def signal(source, frames):
    values = []
    for frame in range(frames):
        nvda = .125 if frame % 2 else -.125
        values.extend(((.25,), (0, .5), (nvda, -nvda))[source])
    return struct.pack('<%df' % len(values), *values)


class NvdaBackend(ControlledBackend):
    def __init__(self, *, present=True, open_error=None):
        super().__init__()
        self.present, self.open_error = present, open_error
        self.mic = NvdaStream(self, 'mic')
        self.loop = NvdaStream(self, 'loop')
        self.nvda = NvdaStream(self, 'nvda')
        self.detected = 0

    def open_nvda_loopbacks(self, rate, channels):
        self.detected += 1
        if self.open_error:
            raise self.open_error
        if not self.present:
            return None
        self.log.extend([('loop', 'open_process'), ('nvda', 'open_process')])
        return self.loop, self.nvda

    def feed(self, frames=80):
        with self.condition:
            stamp = self.now
            end = stamp + round(frames * 10_000_000 / self.rate)
            target = self.generation + 1
            kinds = ('mic', 'loop', 'nvda') if self.present else ('mic', 'loop')
            self.pending = end, {kind: [AudioPacket(stamp, signal(source, frames))]
                                 for source, kind in enumerate(kinds)}
            if not self.condition.wait_for(lambda: self.completed >= target, timeout=2):
                raise AssertionError('Worker did not mix synthetic batch')


class NvdaRecorderTests(unittest.TestCase):
    def make_recorder(self, backend=None, **kwargs):
        backend, frames, errors = backend or NvdaBackend(), [], []
        recorder = SystemAudioRecorder(frames.append, errors.append, rate=8000,
            channels=2, backend_factory=lambda: backend, diagnostics=QuietDiagnostics(),
            separate_nvda=True, **kwargs)
        self.addCleanup(recorder.close)
        return recorder, backend, frames, errors

    def expected(self, other=100, nvda=100, frames=80):
        values = []
        for frame in range(frames):
            voice = (.125 if frame % 2 else -.125) * nvda / 100
            values.extend((round((.25 + voice) * 16384),
                           round((.25 + .5 * other / 100 - voice) * 16384)))
        return struct.pack('<%dh' % len(values), *values)

    def test_nvda_gain_validation_and_thread_safe_live_updates(self):
        self.assertTrue(callable(getattr(SystemAudioRecorder, 'set_nvda_volume', None)))
        recorder, backend, frames, errors = self.make_recorder(nvda_volume=50)
        recorder.start()
        for invalid in (-1, 100.01, float('nan'), float('inf'), -float('inf'), None, 'loud', 1j):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    self.make_recorder(nvda_volume=invalid)
                with self.assertRaises(ValueError):
                    recorder.set_nvda_volume(invalid)
        backend.feed()
        recorder.stop()
        self.assertEqual(errors, [])
        self.assertEqual(b''.join(frames), self.expected(nvda=50))

    def test_independent_initial_gains_through_real_worker_to_exact_pcm(self):
        self.assertIn('separate_nvda', inspect.signature(SystemAudioRecorder).parameters)
        self.assertIn('nvda_volume', inspect.signature(SystemAudioRecorder).parameters)
        for other in (100, 50, 0):
            for nvda in (100, 50, 0):
                with self.subTest(other=other, nvda=nvda):
                    recorder, backend, frames, errors = self.make_recorder(
                        system_audio_volume=other, nvda_volume=nvda)
                    self.assertFalse(recorder.nvda_volume_available)
                    recorder.start()
                    self.assertTrue(recorder.nvda_volume_available)
                    backend.feed()
                    recorder.stop()
                    self.assertEqual(errors, [])
                    self.assertEqual(b''.join(frames), self.expected(other, nvda))
                    self.assertNotIn(('loop', 'open'), backend.log)
                    self.assertEqual([s.closed for s in (backend.mic, backend.loop, backend.nvda)], [1, 1, 1])

    def test_live_gains_freeze_pending_ring_and_emitted_pcm_independently(self):
        recorder, backend, frames, errors = self.make_recorder()
        recorder.start()
        backend.feed()
        backend.feed()
        self.assertEqual(frames, [])
        expected = self.expected(frames=160)
        for other, nvda, size in ((100, 50, 80), (50, 50, 80), (50, 0, 4800),
                                  (0, 0, 80), (0, 100, 80), (100, 100, 80)):
            recorder.set_system_audio_volume(other)
            recorder.set_nvda_volume(nvda)
            prior = b''.join(frames)
            backend.feed(size)
            self.assertTrue(b''.join(frames).startswith(prior))
            expected += self.expected(other, nvda, size)
        self.assertTrue(frames)
        recorder.stop()
        self.assertEqual(errors, [])
        self.assertEqual(b''.join(frames), expected)

    def test_default_gains_match_explicit_100_and_old_summed_endpoint(self):
        recordings = []
        for kwargs in ({}, {'system_audio_volume': 100, 'nvda_volume': 100}):
            recorder, backend, frames, errors = self.make_recorder(**kwargs)
            recorder.start()
            backend.feed()
            recorder.stop()
            self.assertEqual(errors, [])
            recordings.append(b''.join(frames))
        backend = NvdaBackend(present=False)
        # Emulate endpoint PCM already containing the other+NVDA mixture.
        original_read = backend.loop.read_packets
        def read_combined():
            packets = original_read()
            combined = []
            for packet in packets:
                other = struct.unpack('<%df' % (len(packet.data) // 4), packet.data)
                voice = struct.unpack('<%df' % len(other), signal(2, len(other) // 2))
                combined.append(AudioPacket(packet.timestamp,
                    struct.pack('<%df' % len(other), *(a + b for a, b in zip(other, voice)))))
            return combined
        backend.loop.read_packets = read_combined
        frames, errors = [], []
        recorder = SystemAudioRecorder(frames.append, errors.append, rate=8000, channels=2,
            backend_factory=lambda: backend, diagnostics=QuietDiagnostics())
        self.addCleanup(recorder.close)
        recorder.start()
        backend.feed()
        recorder.stop()
        self.assertEqual(backend.detected, 0)
        self.assertFalse(recorder.nvda_volume_available)
        self.assertEqual(errors, [])
        self.assertEqual(recordings, [b''.join(frames)] * 2)
        self.assertEqual(recordings[0], self.expected())

    def test_no_nvda_uses_endpoint_with_disabled_availability_and_no_gain_effect(self):
        recorder, backend, frames, errors = self.make_recorder(NvdaBackend(present=False))
        recorder.start()
        self.assertFalse(recorder.nvda_volume_available)
        backend.feed()
        recorder.set_nvda_volume(0)
        backend.feed()
        recorder.stop()
        self.assertEqual(backend.detected, 1)
        self.assertIn(('loop', 'open'), backend.log)
        self.assertNotIn(('nvda', 'start'), backend.log)
        self.assertEqual(errors, [])
        self.assertEqual(b''.join(frames), self.expected(nvda=0, frames=160))

    def test_native_open_or_either_process_start_failure_never_falls_back(self):
        for failing in ('open', 'loop', 'nvda'):
            with self.subTest(failing=failing):
                backend = NvdaBackend(open_error=NotImplementedError('unsupported')
                                      if failing == 'open' else None)
                backend.fail_start = failing
                recorder, backend, frames, errors = self.make_recorder(backend)
                with self.assertRaises((NotImplementedError, RuntimeError)):
                    recorder.start()
                recorder.stop()
                self.assertFalse(recorder.nvda_volume_available)
                self.assertEqual(frames, [])
                self.assertEqual(errors, [])  # startup errors are raised, not callback errors
                self.assertNotIn(('loop', 'open'), backend.log)
                self.assertEqual(backend.closed, 1)
                self.assertEqual(backend.mic.closed, 1)
                if failing != 'open':
                    self.assertEqual((backend.loop.closed, backend.nvda.closed), (1, 1))

    def test_pause_gains_survive_all_stream_reset_and_fresh_resume(self):
        recorder, backend, frames, errors = self.make_recorder(nvda_volume=50)
        recorder.start()
        backend.feed()
        recorder.set_paused(True)
        self.assertEqual(b''.join(frames), self.expected(nvda=50))
        for stream in (backend.mic, backend.loop, backend.nvda):
            self.assertFalse(stream.started)
            self.assertEqual(stream.resets, 1)
        backend.feed()  # synthetic preview during pause must not leak after resume
        self.assertEqual(b''.join(frames), self.expected(nvda=50))
        with backend.condition:
            backend.now += 10_000_000
        recorder.set_nvda_volume(0)
        recorder.set_system_audio_volume(50)
        recorder.set_paused(False)
        backend.feed()
        recorder.set_paused(True)
        recorder.set_nvda_volume(100)
        recorder.set_system_audio_volume(0)
        recorder.set_paused(False)
        backend.feed()
        recorder.stop()
        self.assertEqual(errors, [])
        self.assertEqual(b''.join(frames), self.expected(nvda=50) +
                         self.expected(other=50, nvda=0) + self.expected(other=0))
        self.assertEqual([s.resets for s in (backend.mic, backend.loop, backend.nvda)], [2, 2, 2])

    def test_pause_ack_waits_for_third_stream_reset(self):
        recorder, backend, frames, errors = self.make_recorder()
        recorder.start()
        backend.feed()
        entered, release, acknowledged = threading.Event(), threading.Event(), threading.Event()
        original = backend.nvda.reset
        def reset():
            entered.set()
            if not release.wait(2):
                raise AssertionError('test did not release reset')
            original()
        backend.nvda.reset = reset
        failures = []
        def pause():
            try:
                recorder.set_paused(True)
                acknowledged.set()
            except Exception as exc:
                failures.append(exc)
        worker = threading.Thread(target=pause)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertFalse(acknowledged.is_set())
            self.assertTrue(all(not s.started for s in (backend.mic, backend.loop, backend.nvda)))
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertTrue(acknowledged.is_set())
        recorder.stop()
        self.assertEqual(errors, [])
        self.assertEqual(b''.join(frames), self.expected())

    def test_any_source_read_failure_preserves_only_verified_prefix(self):
        for source in ('mic', 'loop', 'nvda'):
            with self.subTest(source=source):
                recorder, backend, frames, errors = self.make_recorder()
                recorder.start()
                backend.feed(4800)
                prior = b''.join(frames)
                self.assertTrue(prior)
                failure = RuntimeError('synthetic device failure')
                with backend.condition:
                    stamp = backend.now
                    backend.pending = stamp + 100_000, {
                        kind: [AudioPacket(stamp, signal(index, 80))]
                        for index, kind in enumerate(('mic', 'loop', 'nvda'))}
                    getattr(backend, source).error = failure
                self.assertTrue(recorder._done.wait(2))
                recorder.stop()
                self.assertEqual(errors, [failure])
                self.assertIs(recorder.failure, failure)
                self.assertEqual(b''.join(frames), self.expected(frames=4800))
                self.assertTrue(b''.join(frames).startswith(prior))
                self.assertNotIn(('loop', 'open'), backend.log)

    def test_third_source_loss_while_paused_preserves_partial_pcm(self):
        recorder, backend, frames, errors = self.make_recorder()
        recorder.start()
        backend.feed()
        recorder.set_paused(True)
        failure = RuntimeError('removed while paused')
        backend.nvda.error = failure
        self.assertTrue(recorder._done.wait(2))
        recorder.stop()
        with self.assertRaises(RuntimeError):
            recorder.set_paused(False)
        self.assertEqual(errors, [failure])
        self.assertEqual(b''.join(frames), self.expected())

    def test_third_reset_failure_is_not_acknowledged_as_success(self):
        recorder, backend, frames, errors = self.make_recorder()
        recorder.start()
        backend.feed()
        failure = RuntimeError('reset failed')
        def reset():
            raise failure
        backend.nvda.reset = reset
        with self.assertRaises(RuntimeError):
            recorder.set_paused(True)
        recorder.stop()
        self.assertEqual(errors, [failure])
        self.assertEqual(b''.join(frames), self.expected())

    def test_stop_flushes_last_three_source_batch_before_returning(self):
        recorder, backend, frames, errors = self.make_recorder()
        recorder.start()
        backend.feed()
        original_stop = backend.mic.stop
        def stop():
            original_stop()
            with backend.condition:
                backend.batch = {kind: [AudioPacket(backend.now - 100_000, signal(index, 80))]
                                 for index, kind in enumerate(('mic', 'loop', 'nvda'))}
        backend.mic.stop = stop
        # Native Stop preserves unread final packets for collect(); model that
        # contract explicitly rather than keeping fake started flags true.
        for stream in (backend.mic, backend.loop, backend.nvda):
            original_read = stream.read_packets
            def read(stream=stream, original_read=original_read):
                if not stream.started:
                    return backend.batch.pop(stream.kind, [])
                return original_read()
            stream.read_packets = read
        with backend.condition:
            backend.now += 100_000
        recorder.stop()
        self.assertEqual(errors, [])
        self.assertEqual(b''.join(frames), self.expected(frames=160))
        recorder.stop()
        self.assertEqual([s.closed for s in (backend.mic, backend.loop, backend.nvda)], [1, 1, 1])

    def test_delayed_start_waiter_does_not_misclassify_runtime_nvda_failure(self):
        recorder, backend, frames, errors = self.make_recorder()
        original_ready = recorder._ready
        owner = self
        class DelayedReady:
            def set(self): original_ready.set()
            def is_set(self): return original_ready.is_set()
            def wait(self, timeout):
                ready = original_ready.wait(timeout)
                if ready:
                    backend.feed()
                    backend.nvda.error = RuntimeError('runtime failure')
                    owner.assertTrue(recorder._done.wait(2))
                return ready
        recorder._ready = DelayedReady()
        recorder.start()
        recorder.stop()
        self.assertEqual(len(errors), 1)
        self.assertEqual(b''.join(frames), self.expected())

    def test_cancel_during_process_open_closes_all_without_start(self):
        recorder, backend, frames, errors = self.make_recorder(startup_timeout=.02)
        entered, release = threading.Event(), threading.Event()
        original = backend.open_nvda_loopbacks
        def open_pair(rate, channels):
            entered.set()
            release.wait(2)
            return original(rate, channels)
        backend.open_nvda_loopbacks = open_pair
        try:
            with self.assertRaises(TimeoutError):
                recorder.start()
            self.assertTrue(entered.is_set())
        finally:
            release.set()
            recorder.stop()
        self.assertFalse(recorder.nvda_volume_available)
        self.assertEqual(frames, [])
        self.assertEqual(errors, [])
        self.assertFalse(any(action == 'start' for _, action in backend.log))
        self.assertEqual([s.closed for s in (backend.mic, backend.loop, backend.nvda)], [1, 1, 1])


if __name__ == '__main__':
    unittest.main()
