"""Mono microphone through the actual stereo recorder; no live devices/UI."""
import struct
import unittest

from core.system_audio_capture import AudioPacket, SystemAudioRecorder
from tests.test_system_audio_volume import ControlledBackend, QuietDiagnostics


class MonoBackend(ControlledBackend):
    def open_microphone(self, name, rate, channels):
        self.mic_channels = channels
        return super().open_microphone(name, rate, channels)

    def open_loopback(self, rate, channels):
        self.loop_channels = channels
        return super().open_loopback(rate, channels)

    def feed_samples(self, microphone, computer, *, mic_offset=0):
        with self.condition:
            stamp = self.now
            frames = len(computer) // self.loop_channels
            end = stamp + round(frames * 10_000_000 / self.rate)
            target = self.generation + 1
            mic = AudioPacket(stamp + round(mic_offset * 10_000_000 / self.rate),
                              struct.pack('<%df' % len(microphone), *microphone))
            loop = AudioPacket(stamp, struct.pack('<%df' % len(computer), *computer))
            self.pending = end, {'mic': [mic], 'loop': [loop]}
            if not self.condition.wait_for(lambda: self.completed >= target, timeout=2):
                raise AssertionError('Worker did not finish synthetic mono/stereo batch')


class MonoMicrophoneTests(unittest.TestCase):
    def make_recorder(self, *, channels=2, rate=48000):
        backend, output, errors = MonoBackend(), [], []
        recorder = SystemAudioRecorder(output.append, errors.append,
            rate=rate, channels=channels, backend_factory=lambda: backend,
            diagnostics=QuietDiagnostics())
        self.addCleanup(recorder.close)
        recorder.start()
        return recorder, backend, output, errors

    def test_native_mono_is_centred_without_downmixing_computer_stereo(self):
        recorder, backend, output, errors = self.make_recorder()
        self.assertEqual(backend.mic_channels, 1, 'Open microphone natively as mono')
        self.assertEqual(backend.loop_channels, 2, 'Keep computer capture stereo')
        microphone = [.25, -.125, .5, 0, -.25, .125, .0625]
        computer = [.125, -.25] * len(microphone)
        backend.feed_samples(microphone, computer)
        recorder.stop()
        expected = [round((sample + side) * 16384)
                    for sample in microphone for side in (.125, -.25)]
        self.assertEqual(errors, [])
        self.assertEqual(b''.join(output), struct.pack('<%dh' % len(expected), *expected))

    def test_mono_voice_keeps_position_across_unequal_packets_and_pause(self):
        recorder, backend, output, errors = self.make_recorder(rate=8000)
        # Seven microphone frames start three frames after the computer.
        backend.feed_samples([.25] * 7, [.125, -.25] * 10, mic_offset=3)
        recorder.set_paused(True)
        expected = struct.pack('<2h', 2048, -4096) * 3
        expected += struct.pack('<2h', 6144, 0) * 7
        self.assertEqual(b''.join(output), expected)
        with backend.condition:
            backend.now += 10_000_000  # Paused time must not enter output.
        recorder.set_paused(False)
        # One-frame and odd-size mono packets must not halve/double duration.
        for count in (1, 7, 17):
            backend.feed_samples([-.125] * count, [0, 0] * count)
            expected += struct.pack('<2h', -2048, -2048) * count
        recorder.stop()
        self.assertEqual(errors, [])
        self.assertEqual(b''.join(output), expected)

    def test_silent_microphone_does_not_collapse_or_swap_stereo(self):
        recorder, backend, output, errors = self.make_recorder()
        computer = [.125, -.25, .5, -.125, -.25, .25]
        backend.feed_samples([0] * 3, computer)
        recorder.stop()
        self.assertEqual(errors, [])
        self.assertEqual(b''.join(output), struct.pack('<6h',
                         *(round(value * 16384) for value in computer)))

    def test_existing_mono_output_is_not_expanded(self):
        recorder, backend, output, errors = self.make_recorder(channels=1)
        self.assertEqual((backend.mic_channels, backend.loop_channels), (1, 1))
        backend.feed_samples([.25, -.125, .5], [.125, .25, -.25])
        recorder.stop()
        self.assertEqual(errors, [])
        self.assertEqual(b''.join(output), struct.pack('<3h', 6144, 2048, 4096))

    def test_diagnostics_count_frames_not_duplicated_stereo_samples(self):
        class Diagnostics(QuietDiagnostics):
            def __init__(self):
                self.observed = []
            def packet(self, source, **kwargs):
                self.observed.append((source, kwargs['packet_frames']))
        recorder, backend, output, errors = self.make_recorder()
        diagnostics = Diagnostics()
        recorder._diagnostics = diagnostics
        backend.feed_samples([.25] * 7, [0, 0] * 7)
        recorder.stop()
        self.assertEqual(errors, [])
        self.assertEqual(diagnostics.observed, [('microphone', 7), ('loopback', 7)])
        self.assertEqual(len(b''.join(output)), 7 * 2 * 2)


if __name__ == '__main__':
    unittest.main()
