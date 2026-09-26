"""Transient-loss regression tests. Synthetic PCM/COM only, no desktop capture."""
import struct
import unittest


class NativeRecoveryTests(unittest.TestCase):
    def test_observed_short_microphone_gaps_are_not_fatal(self):
        from tests.test_wasapi_capture import NativeHarness
        # The actual logs had 40/70ms between starts of nominal 10ms packets.
        for delta in (399864, 699948):
            with self.subTest(delta=delta):
                payload = struct.pack('<480f', *([.5] * 480))
                harness = NativeHarness([(1000000, 1, payload),
                    (1000000 + delta, 1, payload), (1100000 + delta, 0, payload)])
                stream = harness.stream()
                try:
                    try:
                        packets = stream.read_packets()
                    except harness.w.WasapiError as exc:
                        self.fail('Short microphone discontinuity must continue: ' + str(exc))
                    self.assertEqual(len(packets), 3)
                    self.assertEqual(packets[1].timestamp, 1000000 + delta)
                    self.assertEqual(packets[1].data, payload)
                    self.assertEqual(sum(k == 'capture' and i == 4 for k, i, a in harness.calls), 3)
                finally:
                    stream.close()


    def test_capture_buffer_reserve_and_recoveries_are_visible(self):
        from core.system_audio_diagnostics import CaptureDiagnostics
        from tests.test_wasapi_capture import NativeHarness
        import json
        import tempfile
        from pathlib import Path
        h = NativeHarness([(1000000, 1, bytes(1920)), (1700000, 1, bytes(1920))])
        stream = h.stream()
        try:
            init = next(args for kind, index, args in h.calls if kind == 'client' and index == 3)
            self.assertEqual(init[2], 10000000, 'Request one second of native capture reserve')
            stream.read_packets()
            native = stream.diagnostic_snapshot()
            self.assertEqual(native.get('discontinuities'), 1)
            stream.reset()
            self.assertEqual(stream.diagnostic_snapshot().get('discontinuities'), 1)
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'capture.log'
                diag = CaptureDiagnostics(rate=48000, channels=1, path=path)
                diag.update(source='microphone', native=native,
                            microphone_late_packets=2, microphone_late_frames=480,
                            playout_delay_ms=500, late_tolerance_ms=500)
                diag.event('stop')
                diag.close()
                row = json.loads(path.read_text())
                self.assertEqual(row['native']['microphone']['discontinuities'], 1)
                self.assertEqual(row.get('microphone_late_packets'), 2)
                self.assertEqual(row.get('microphone_late_frames'), 480)
                self.assertEqual(row.get('playout_delay_ms'), 500)
        finally:
            stream.close()


class MixerRecoveryTests(unittest.TestCase):
    def mixer(self):
        from core.system_audio_capture import TimelineMixer
        try:
            return TimelineMixer(1000, 1, 0, late_tolerance_seconds=.5)
        except TypeError:
            self.fail('Mixer lacks bounded late-packet recovery')

    def test_partial_late_packet_keeps_only_unrendered_tail_at_original_time(self):
        from core.system_audio_capture import AudioPacket
        mix = self.mixer()
        self.assertEqual(mix.render_until(200000), bytes(40))
        mix.add(0, AudioPacket(100000, struct.pack('<30f', *([.5] * 30))))
        mix.add(1, AudioPacket(200000, struct.pack('<20f', *([.5] * 20))))
        pcm = mix.render_until(400000)
        self.assertEqual(struct.unpack('<20h', pcm), (16384,) * 20)
        self.assertEqual(mix.position, 40)
        self.assertEqual(mix.late_packets, [1, 0])
        self.assertEqual(mix.late_frames, [10, 0])

    def test_wholly_late_packet_is_not_replayed_or_mixed_into_next_audio(self):
        from core.system_audio_capture import AudioPacket
        mix = self.mixer()
        mix.render_until(200000)
        mix.add(0, AudioPacket(0, struct.pack('<10f', *([1.] * 10))))
        mix.add(0, AudioPacket(200000, struct.pack('<10f', *([.5] * 10))))
        self.assertEqual(struct.unpack('<10h', mix.render_until(300000)), (8192,) * 10)
        self.assertEqual(mix.late_frames, [10, 0])

    def test_stereo_gap_is_silence_only_in_microphone_without_stretch_or_drift(self):
        from core.system_audio_capture import AudioPacket, TimelineMixer
        for gap_ms in (30, 60):
            with self.subTest(gap_ms=gap_ms):
                mix = TimelineMixer(48000, 2, 0, late_tolerance_seconds=.5)
                mic = struct.pack('<2f', .5, 0.) * 480
                end_frames = (20 + gap_ms) * 48
                mix.add(0, AudioPacket(0, mic))
                mix.add(0, AudioPacket((10 + gap_ms) * 10000, mic))
                mix.add(1, AudioPacket(0, struct.pack('<2f', 0., .5) * end_frames))
                data = mix.render_until((20 + gap_ms) * 10000)
                samples = struct.unpack('<' + str(end_frames * 2) + 'h', data)
                self.assertEqual(samples[::2], (8192,) * 480 + (0,) * (gap_ms * 48) + (8192,) * 480)
                self.assertEqual(samples[1::2], (8192,) * end_frames)

    def test_large_lateness_bad_timestamps_and_overflow_remain_fatal(self):
        from core.system_audio_capture import AudioPacket
        mix = self.mixer()
        mix.render_until(10000000)
        with self.assertRaisesRegex(ValueError, 'deadline'):
            mix.add(0, AudioPacket(0, bytes(4)))
        mix.add(0, AudioPacket(11000000, bytes(40)))
        with self.assertRaisesRegex(ValueError, 'backwards'):
            mix.add(0, AudioPacket(10900000, bytes(40)))
        with self.assertRaisesRegex(ValueError, 'bounded buffer'):
            mix.add(1, AudioPacket(40000000, bytes(4)))


class TimestampOverlapTests(unittest.TestCase):
    def test_real_loopback_timestamps_trim_prefix_without_shift_or_double_mix(self):
        from core.system_audio_capture import AudioPacket, TimelineMixer
        mix = TimelineMixer(48000, 2, 0)
        stamp = lambda frame: round(frame * 10000000 / 48000)
        mix.add(0, AudioPacket(0, struct.pack('<2f', .25, 0.) * 1921))
        # Exact relative frame positions from the real packet_overlap log.
        packets = [(0, [.5] * 480), (480, [.5] * 480),
                   (955, [i / 1024 for i in range(480)]), (1441, [.125] * 480)]
        try:
            for frame, values in packets:
                mix.add(1, AudioPacket(stamp(frame), b''.join(
                    struct.pack('<2f', 0., value) for value in values)))
            pcm = mix.render_until(stamp(1921))
        except ValueError as exc:
            self.fail('Measured sub-millisecond overlap must not abort: ' + str(exc))
        samples = struct.unpack('<3842h', pcm)
        self.assertEqual(samples[::2], (4096,) * 1921)
        self.assertEqual(samples[1::2], (8192,) * 960 +
                         tuple(i * 16 for i in range(5, 480)) + (0,) * 6 + (2048,) * 480)

    def test_one_millisecond_overlap_is_bounded(self):
        from core.system_audio_capture import AudioPacket, TimelineMixer
        for overlap in (48, 49):
            with self.subTest(overlap=overlap):
                mix = TimelineMixer(48000, 1, 0)
                payload = struct.pack('<f', .5) * 480
                mix.add(0, AudioPacket(0, payload))
                mix.add(0, AudioPacket(round((480 - overlap) * 10000000 / 48000), payload))
                if overlap == 49:
                    with self.assertRaisesRegex(ValueError, 'overlap'):
                        mix.render_until(200000)
                else:
                    try:
                        pcm = mix.render_until(200000)
                    except ValueError as exc:
                        self.fail('Bounded overlap should be clipped: ' + str(exc))
                    self.assertEqual(struct.unpack('<960h', pcm), (8192,) * 912 + (0,) * 48)

    def test_large_overlap_and_backwards_timestamps_still_fail(self):
        from core.system_audio_capture import AudioPacket, TimelineMixer
        mix = TimelineMixer(48000, 1, 0)
        payload = struct.pack('<f', .5) * 480
        mix.add(0, AudioPacket(0, payload))
        mix.add(0, AudioPacket(50000, payload))
        with self.assertRaisesRegex(ValueError, 'overlap'):
            mix.render_until(200000)
        with self.assertRaisesRegex(ValueError, 'backwards'):
            mix.add(0, AudioPacket(40000, payload))


class WorkerRecoveryTests(unittest.TestCase):
    def test_short_take_flushes_buffer_before_pause_and_stop(self):
        import threading
        from core.system_audio_capture import SystemAudioRecorder
        from tests.test_system_audio_capture import FakeBackend
        for action in ('pause', 'stop'):
            with self.subTest(action=action):
                delivered, frames, errors = threading.Event(), [], []
                def receive(data):
                    frames.append(data)
                    delivered.set()
                recorder = SystemAudioRecorder(receive, errors.append, rate=8000,
                                               backend_factory=FakeBackend)
                try:
                    recorder.start()
                    # No emitted PCM before the 500ms reserve, but stop/pause
                    # must flush even a short recording instead of losing it.
                    self.assertFalse(delivered.wait(.03))
                    if action == 'pause': recorder.set_paused(True)
                    else: recorder.stop()
                    self.assertTrue(frames)
                    samples = struct.unpack('<' + str(sum(map(len, frames)) // 2) + 'h', b''.join(frames))
                    self.assertTrue(any(sample for sample in samples))
                    self.assertEqual(errors, [])
                finally:
                    recorder.stop()

    def test_delayed_read_does_not_discard_backlog_or_desynchronise_sources(self):
        from core.system_audio_capture import AudioPacket, SystemAudioRecorder
        # Replay the scheduling shape: microphone is read first; loopback read
        # advances wall time; the next microphone poll carries the backlog.
        # Unlike a real microphone, this fake provides exact known PCM.
        for delay_ms in (249, 335, 650):
            with self.subTest(delay_ms=delay_ms):
                class Backend:
                    now = 10000000
                    poll = 0
                    def clock(self): return self.now
                    def open_microphone(self, name, rate, channels):
                        self.rate = rate
                        self.mic = Stream(self, False)
                        return self.mic
                    def open_loopback(self, rate, channels):
                        self.loop = Stream(self, True)
                        return self.loop
                    def close(self): pass
                class Stream:
                    def __init__(self, backend, loop):
                        self.backend, self.loop = backend, loop
                        self.started = False
                    def start(self):
                        self.started = True
                        self.next_stamp = self.backend.now
                    def stop(self): self.started = False
                    def close(self): self.stop()
                    def read_packets(self):
                        b = self.backend
                        if not self.started: return []
                        if not self.loop:
                            b.poll += 1
                            b.now += 100000
                            if b.poll == 120:
                                recorder._stop_stamp = b.now
                                recorder._stop.set()
                        elif b.poll == 70:
                            b.now += delay_ms * 10000
                        n = (b.now - self.next_stamp) * b.rate // 10000000
                        packet = AudioPacket(self.next_stamp, struct.pack('<f', .5) * n)
                        self.next_stamp += n * 10000000 // b.rate
                        return [packet] if n else []
                class Sink:
                    def __init__(self): self.metrics = {}
                    def update(self, **kw): self.metrics.update(kw)
                    def packet(self, *args, **kw): pass
                    def event(self, *args, **kw): pass
                    def close(self): pass
                class Wake:
                    def clear(self): pass
                    def wait(self, timeout): pass
                backend, sink, frames, errors = Backend(), Sink(), [], []
                recorder = SystemAudioRecorder(frames.append, errors.append, rate=8000,
                    backend_factory=lambda: backend, diagnostics=sink)
                recorder._wake = Wake()
                recorder._run()
                self.assertIsNone(recorder.failure)
                self.assertEqual(errors, [])
                pcm = b''.join(frames)
                expected_frames = (1200 + delay_ms) * 8
                samples = struct.unpack('<' + str(len(pcm) // 2) + 'h', pcm)
                self.assertEqual(len(samples), expected_frames)
                expected = [16384] * expected_frames
                # Beyond the playout reserve, only the already emitted mic
                # portion is irretrievable; loopback and all later PCM stay put.
                dropped = max(0, delay_ms - 500) * 8
                expected[700 * 8:700 * 8 + dropped] = [8192] * dropped
                self.assertEqual(samples, tuple(expected),
                                 'Recovered audio must keep duration and source alignment')
                self.assertEqual(sink.metrics.get('playout_delay_ms'), 500)
                self.assertEqual(sink.metrics.get('microphone_late_frames', 0), dropped)
                self.assertEqual(sink.metrics.get('microphone_late_packets', 0), int(dropped > 0))


if __name__ == '__main__':
    unittest.main()
