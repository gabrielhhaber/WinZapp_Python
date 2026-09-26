"""Headless tests: run with unittest discover, never create a wx.App."""
import importlib
import struct
import unittest


class TimelineTests(unittest.TestCase):
    def test_two_sources_share_clock_and_silent_loopback_keeps_duration(self):
        try:
            module = importlib.import_module('core.system_audio_capture')
        except ImportError:
            self.fail('headless system_audio_capture engine is not implemented')
        mix = module.TimelineMixer(rate=1000, channels=1, epoch=10_000_000)
        mix.add(0, module.AudioPacket(10_000_000, struct.pack('<4f', .5, .5, .5, .5)))
        mix.add(1, module.AudioPacket(10_020_000, struct.pack('<2f', .5, .5)))
        pcm = mix.render_until(10_060_000)
        self.assertEqual(struct.unpack('<6h', pcm), (8192, 8192, 16384, 16384, 0, 0))

    def test_bounded_ring_rejects_late_future_and_malformed_packets(self):
        from core.system_audio_capture import TimelineMixer, AudioPacket
        mix = TimelineMixer(1000, 1, 0)
        mix.render_until(10_000)
        for stamp, data in [(0, struct.pack('<f', .5)),
                            (30_000_000, struct.pack('<f', .5)),
                            (10_000, b'x')]:
            with self.subTest(stamp=stamp, data=data):
                with self.assertRaises(ValueError):
                    mix.add(0, AudioPacket(stamp, data))
        with self.assertRaises(ValueError):
            mix.render_until(100_000_000)

    def test_timestamp_rounding_does_not_double_mix_adjacent_packets(self):
        from core.system_audio_capture import TimelineMixer, AudioPacket
        mix = TimelineMixer(1000, 1, 0)
        mix.add(0, AudioPacket(0, struct.pack('<3f', .5, .5, .5)))
        mix.add(0, AudioPacket(24_999, struct.pack('<2f', .5, .5)))
        # QPC says the first packet spans 2.5 ms, rounded to 2 output
        # frames. Re-time it, never sum overlapping samples from one source.
        self.assertEqual(struct.unpack('<5h', mix.render_until(50_000)), (8192,) * 4 + (0,))
        with self.assertRaises(ValueError):
            mix.add(0, AudioPacket(10_000, struct.pack('<2f', .5, .5)))

    def test_clock_drift_is_retimed_without_growing_delay_or_silent_holes(self):
        from core.system_audio_capture import TimelineMixer, AudioPacket
        mix = TimelineMixer(1000, 1, 0)
        for i in range(10):
            mix.add(0, AudioPacket(i * 1_001_000, struct.pack('<100f', *([.5] * 100))))
        pcm = mix.render_until(10_009_000)
        self.assertEqual(struct.unpack('<1001h', pcm), (8192,) * 1001)

    def test_stereo_clip_nonfinite_and_startup_preroll(self):
        from core.system_audio_capture import TimelineMixer, AudioPacket
        mix = TimelineMixer(1000, 2, 10_000)
        mix.add(0, AudioPacket(0, struct.pack('<6f', 1, 1, 4, -4, float('nan'), float('inf'))))
        self.assertEqual(struct.unpack('<4h', mix.render_until(30_000)), (32767, -32768, 0, 0))


class FakeStream:
    def __init__(self, backend, kind):
        self.backend, self.kind = backend, kind
        self.channels = 1
        self.started = False
        self.error = None
        self.closed = 0
        self.resets = 0
        self.next_stamp = None
    def start(self):
        if self.kind == self.backend.fail_start:
            raise RuntimeError('start failure')
        self.started = True
        self.next_stamp = self.backend.clock()
        self.backend.log.append((self.kind, 'start'))
    def stop(self):
        self.started = False
        self.backend.log.append((self.kind, 'stop'))
    def reset(self):
        self.next_stamp = None
        self.resets += 1
    def close(self):
        self.closed += 1
    def read_packets(self):
        from core.system_audio_capture import AudioPacket
        if self.error:
            raise self.error
        if not self.started or self.next_stamp is None:
            return []
        now = self.backend.clock()
        frames = int((now - self.next_stamp) * self.backend.rate / 10_000_000)
        if frames <= 0:
            return []
        stamp = self.next_stamp
        self.next_stamp += round(frames * 10_000_000 / self.backend.rate)
        return [AudioPacket(stamp, struct.pack('<f', .25) * frames * self.channels)]


class FakeBackend:
    def __init__(self, fail_open=False, fail_start=None):
        import threading
        self.thread = threading.get_ident()
        self.fail_open, self.fail_start = fail_open, fail_start
        self.log, self.closed = [], 0
        self.mic, self.loop = FakeStream(self, 'mic'), FakeStream(self, 'loop')
    def clock(self):
        import time
        return time.monotonic_ns() // 100
    def open_microphone(self, name, rate, channels):
        self.rate, self.channels, self.name = rate, channels, name
        self.mic.channels = channels
        self.log.append(('mic', 'open'))
        return self.mic
    def open_loopback(self, rate, channels):
        if self.fail_open:
            raise RuntimeError('loopback unavailable')
        self.log.append(('loop', 'open'))
        self.loop.channels = channels
        return self.loop
    def close(self):
        self.closed += 1


class RecorderTests(unittest.TestCase):
    def make_recorder(self, **backend_args):
        import threading
        from core.system_audio_capture import SystemAudioRecorder
        self.frames, self.errors, self.threads = [], [], []
        self.frame_ready, self.error_ready = threading.Event(), threading.Event()
        self.backends = []
        def factory():
            backend = FakeBackend(**backend_args)
            self.backends.append(backend)
            return backend
        def frames(data):
            self.frames.append(data)
            self.threads.append(threading.get_ident())
            self.frame_ready.set()
        def error(exc):
            self.errors.append(exc)
            self.error_ready.set()
        recorder = SystemAudioRecorder(frames, error, microphone_name='USB', rate=8000,
                                        channels=2, backend_factory=factory)
        self.addCleanup(recorder.close)
        return recorder

    def test_worker_opens_both_then_delivers_pcm_and_stop_is_idempotent(self):
        import threading
        recorder = self.make_recorder()
        recorder.start()
        self.assertTrue(self.frame_ready.wait(2))
        recorder.stop_stream(); recorder.close(); recorder.stop()
        backend = self.backends[0]
        self.assertEqual(backend.log[:4], [('mic', 'open'), ('loop', 'open'), ('mic', 'start'), ('loop', 'start')])
        self.assertEqual((recorder.rate, recorder.channels, backend.name), (8000, 2, 'USB'))
        self.assertNotEqual(backend.thread, threading.get_ident())
        self.assertEqual(set(self.threads), {backend.thread})
        self.assertTrue(all(len(data) % 4 == 0 for data in self.frames))
        self.assertEqual((backend.closed, backend.mic.closed, backend.loop.closed), (1, 1, 1))
        self.assertFalse(self.errors)

    def test_loopback_open_or_start_failure_never_emits_microphone_only(self):
        for kwargs in ({'fail_open': True}, {'fail_start': 'loop'}):
            with self.subTest(kwargs=kwargs):
                recorder = self.make_recorder(**kwargs)
                with self.assertRaises(RuntimeError):
                    recorder.start()
                recorder.stop()
                self.assertEqual(self.frames, [])
                self.assertEqual(self.backends[0].closed, 1)

    def test_device_loss_reports_once_preserving_previously_emitted_frames(self):
        recorder = self.make_recorder()
        recorder.start()
        self.assertTrue(self.frame_ready.wait(2))
        prior = b''.join(self.frames)
        self.backends[0].loop.error = RuntimeError('device removed')
        self.assertTrue(self.error_ready.wait(2))
        recorder.stop(); recorder.stop()
        self.assertTrue(b''.join(self.frames).startswith(prior))
        self.assertEqual(len(self.errors), 1)
        self.assertIs(recorder.failure, self.errors[0])

    def test_runtime_failure_before_start_waiter_returns_preserves_successful_start(self):
        """A delayed UI opener must not discard PCM as a failed startup."""
        recorder = self.make_recorder()
        original_ready = recorder._ready
        owner = self

        class DelayedReady:
            def set(self):
                original_ready.set()

            def is_set(self):
                return original_ready.is_set()

            def wait(self, timeout):
                ready = original_ready.wait(timeout)
                if ready:
                    owner.assertTrue(owner.frame_ready.wait(2))
                    owner.backends[0].loop.error = RuntimeError('lost after successful dual start')
                    owner.assertTrue(owner.error_ready.wait(2))
                return ready

        recorder._ready = DelayedReady()
        recorder.start()  # successful start, followed by separately reported runtime loss
        self.assertTrue(self.frames)
        self.assertEqual(len(self.errors), 1)
        self.assertIs(recorder.failure, self.errors[0])

    def test_start_timeout_never_opens_or_starts_devices_after_cancellation(self):
        import threading
        from core.system_audio_capture import SystemAudioRecorder
        gate = threading.Event()
        backend = FakeBackend()
        def factory():
            gate.wait(1)
            return backend
        recorder = SystemAudioRecorder(lambda data: None, lambda exc: None,
                                        backend_factory=factory, startup_timeout=.02)
        try:
            with self.assertRaises(TimeoutError):
                recorder.start()
            gate.set()
            recorder.stop()
            self.assertEqual(backend.log, [])
            self.assertIsInstance(recorder.failure, TimeoutError)
            self.assertEqual(backend.closed, 1)
        finally:
            gate.set()
            recorder.close()

    def test_timeout_during_first_start_never_starts_second_source(self):
        import threading
        from core.system_audio_capture import SystemAudioRecorder
        gate, entered = threading.Event(), threading.Event()
        backend = FakeBackend()
        original = backend.mic.start
        def blocked_start():
            entered.set()
            gate.wait(1)
            original()
        backend.mic.start = blocked_start
        recorder = SystemAudioRecorder(lambda data: self.fail('unexpected PCM'), lambda exc: None,
                                        backend_factory=lambda: backend, startup_timeout=.02)
        try:
            with self.assertRaises(TimeoutError):
                recorder.start()
            self.assertTrue(entered.is_set())
            gate.set()
            recorder.stop()
            self.assertNotIn(('loop', 'start'), backend.log)
        finally:
            gate.set()
            recorder.stop()

    def test_paused_device_removal_still_reports_failure(self):
        recorder = self.make_recorder()
        recorder.start()
        recorder.set_paused(True)
        self.backends[0].loop.error = RuntimeError('removed while paused')
        self.assertTrue(self.error_ready.wait(1))
        recorder.stop()
        self.assertEqual(len(self.errors), 1)

    def test_callback_pause_is_rejected_rather_than_falsely_acknowledged(self):
        import threading
        from core.system_audio_capture import SystemAudioRecorder
        called = threading.Event()
        errors = []
        def frame(data):
            try:
                recorder.set_paused(True)
            except RuntimeError:
                called.set()
        recorder = SystemAudioRecorder(frame, errors.append, rate=8000,
                                        backend_factory=FakeBackend)
        recorder.start()
        try:
            self.assertTrue(called.wait(1))
        finally:
            recorder.stop()

    def test_pause_resets_both_and_emits_nothing_during_pause(self):
        import time
        recorder = self.make_recorder()
        recorder.start()
        self.assertTrue(self.frame_ready.wait(2))
        recorder.set_paused(True)
        n = len(self.frames)
        time.sleep(.04)
        self.assertEqual(len(self.frames), n)
        backend = self.backends[0]
        self.assertFalse(backend.mic.started or backend.loop.started)
        self.assertEqual((backend.mic.resets, backend.loop.resets), (1, 1))
        self.frame_ready.clear()
        recorder.set_paused(False)
        self.assertTrue(self.frame_ready.wait(2))
        recorder.stop()
        self.assertFalse(self.errors)


if __name__ == '__main__':
    unittest.main()
