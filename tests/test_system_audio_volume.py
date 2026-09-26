"""Live per-source gain through the real worker; synthetic PCM, no devices/UI."""
import inspect
import struct
import threading
import unittest

from core.system_audio_capture import AudioPacket, SystemAudioRecorder
from tests.test_system_audio_capture import FakeBackend, FakeStream


class ControlledStream(FakeStream):
    def read_packets(self):
        backend = self.backend
        with backend.condition:
            if self.kind == 'mic':
                # A new poll acknowledges the previous mix/render, not merely
                # a native read. Tests can then change gain without a race.
                backend.completed = backend.generation
                backend.condition.notify_all()
                if backend.pending is not None:
                    backend.now, backend.batch = backend.pending
                    backend.pending = None
                    backend.generation += 1
            if not self.started:
                return []
            packets = backend.batch.pop(self.kind, [])
            return packets


class ControlledBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.now = 10_000_000
        self.condition = threading.Condition()
        self.generation = self.completed = 0
        self.pending = None
        self.batch = {}
        self.mic = ControlledStream(self, 'mic')
        self.loop = ControlledStream(self, 'loop')

    def clock(self):
        return self.now

    def feed(self, frames=80, *, loop_offset=0):
        """Publish mono .25 mic and stereo .5/.25 loop at known QPC times."""
        with self.condition:
            stamp = self.now
            end = stamp + round(frames * 10_000_000 / self.rate)
            target = self.generation + 1
            mic = AudioPacket(stamp, struct.pack('<f', .25) * frames)
            loop = AudioPacket(stamp + round(loop_offset * 10_000_000 / self.rate),
                               struct.pack('<2f', .5, .25) * (frames - loop_offset))
            self.pending = end, {'mic': [mic], 'loop': [loop]}
            if not self.condition.wait_for(lambda: self.completed >= target, timeout=2):
                raise AssertionError('Capture worker did not finish synthetic packet')


class QuietDiagnostics:
    def update(self, **kwargs): pass
    def packet(self, *args, **kwargs): pass
    def event(self, *args, **kwargs): pass
    def close(self): pass


class SystemAudioVolumeTests(unittest.TestCase):
    def make_recorder(self, **kwargs):
        backend, frames, errors = ControlledBackend(), [], []
        recorder = SystemAudioRecorder(frames.append, errors.append, rate=8000,
                                       channels=2, backend_factory=lambda: backend,
                                       diagnostics=QuietDiagnostics(), **kwargs)
        self.addCleanup(recorder.close)
        return recorder, backend, frames, errors

    def expected(self, percent, frames=80):
        gain = percent / 100
        return struct.pack('<2h', round((.25 + .5 * gain) * 16384),
                           round((.25 + .25 * gain) * 16384)) * frames

    def test_constructor_volume_scales_only_loopback_before_summing(self):
        self.assertIn('system_audio_volume', inspect.signature(SystemAudioRecorder).parameters,
                      'Recorder needs the optional system_audio_volume argument')
        for volume in (0, 50, 100):
            with self.subTest(volume=volume):
                recorder, backend, frames, errors = self.make_recorder(system_audio_volume=volume)
                recorder.start()
                backend.feed()
                recorder.stop()
                self.assertEqual(errors, [])
                self.assertEqual(b''.join(frames), self.expected(volume))

    def test_live_changes_preserve_pending_buffered_and_emitted_audio(self):
        recorder, backend, frames, errors = self.make_recorder()
        self.assertTrue(callable(getattr(recorder, 'set_system_audio_volume', None)),
                        'Recorder needs a live system-audio volume setter')
        recorder.start()
        backend.feed()
        backend.feed()  # one committed packet and one pending lookahead packet
        self.assertEqual(frames, [])  # still inside the 500 ms playout reserve
        recorder.set_system_audio_volume(50)
        backend.feed()
        recorder.set_system_audio_volume(0)
        backend.feed(frames=4800)  # advance beyond reserve and emit earlier audio
        prior = b''.join(frames)
        self.assertTrue(prior)
        recorder.set_system_audio_volume(100)
        backend.feed()
        recorder.stop()
        pcm = b''.join(frames)
        self.assertTrue(pcm.startswith(prior))
        self.assertEqual(errors, [])
        self.assertEqual(pcm, self.expected(100, 160) + self.expected(50) +
                         self.expected(0, 4800) + self.expected(100))

    def test_invalid_constructor_or_live_values_are_rejected_without_changing_gain(self):
        recorder, backend, frames, errors = self.make_recorder(system_audio_volume=50)
        recorder.start()
        invalid = (-1, 100.01, float('nan'), float('inf'), -float('inf'),
                   None, 'loud', 1j)
        for value in invalid:
            with self.subTest(value=value, entry='constructor'):
                with self.assertRaises(ValueError):
                    self.make_recorder(system_audio_volume=value)
            with self.subTest(value=value, entry='live'):
                with self.assertRaises(ValueError):
                    recorder.set_system_audio_volume(value)
        backend.feed()
        recorder.stop()
        self.assertEqual(errors, [])
        self.assertEqual(b''.join(frames), self.expected(50))

    def test_pause_resume_retains_gain_and_accepts_changes_while_paused(self):
        recorder, backend, frames, errors = self.make_recorder(system_audio_volume=50)
        recorder.start()
        backend.feed()
        recorder.set_paused(True)
        self.assertEqual(b''.join(frames), self.expected(50))
        with backend.condition:
            backend.now += 10_000_000  # paused time must not appear in PCM
        recorder.set_paused(False)
        backend.feed()
        recorder.set_paused(True)
        self.assertEqual(b''.join(frames), self.expected(50, 160))
        recorder.set_system_audio_volume(0)
        recorder.set_paused(False)
        backend.feed()
        recorder.stop()
        self.assertEqual(errors, [])
        self.assertEqual((backend.mic.resets, backend.loop.resets), (2, 2))
        self.assertEqual(b''.join(frames), self.expected(50, 160) + self.expected(0))

    def test_default_is_identical_to_explicit_100(self):
        recordings = []
        for kwargs in ({}, {'system_audio_volume': 100}):
            recorder, backend, frames, errors = self.make_recorder(**kwargs)
            recorder.start()
            backend.feed()
            backend.feed()
            recorder.stop()
            self.assertEqual(errors, [])
            recordings.append(b''.join(frames))
        self.assertEqual(recordings[0], recordings[1])
        self.assertEqual(recordings[0], self.expected(100, 160))

    def test_fractional_prestart_setting_preserves_packet_timestamps(self):
        recorder, backend, frames, errors = self.make_recorder()
        recorder.set_system_audio_volume(12.5)
        recorder.start()
        backend.feed(loop_offset=16)
        recorder.stop()
        self.assertEqual(errors, [])
        self.assertEqual(b''.join(frames), self.expected(0, 16) + self.expected(12.5, 64))


if __name__ == '__main__':
    unittest.main()
