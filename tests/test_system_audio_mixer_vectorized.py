"""The NumPy mixer (issue #717) renders exactly what the per-sample loop did.

The reference below is the loop TimelineMixer used before: three sources at
48 kHz stalled the recording worker on slow CPUs, every source losing ~5% of
the recording as late packets. The rewrite must be faster and byte-identical.
"""
import math
import random
import struct
import time
from array import array

from core.system_audio_capture import AudioPacket, TimelineMixer


class LoopMixer(TimelineMixer):
    """TimelineMixer with the per-sample mixing and rendering it replaced."""

    def _mix(self, samples, count, start, size, begin, end, following):
        for frame in range(begin, end):
            location = (frame - start) * count / size
            left = min(count - 1, int(location))
            fraction = location - left
            for channel in range(self.channels):
                a = float(samples[left * self.channels + channel])
                if left + 1 < count:
                    b = float(samples[(left + 1) * self.channels + channel])
                elif following is not None and size != count:
                    b = struct.unpack_from('<f', following.data, channel * 4)[0]
                else:
                    b = a
                a = max(-65536., min(65536., a)) if math.isfinite(a) else 0.
                b = max(-65536., min(65536., b)) if math.isfinite(b) else 0.
                value = a + fraction * (b - a)
                offset = (frame % self.capacity) * self.channels + channel
                self._ring[offset] += value * .5

    def render_until(self, timestamp):
        end = max(self.position, self._frame(timestamp))
        for source, packet in enumerate(self._pending):
            if packet is not None and self._frame(packet.timestamp) <= end:
                self._commit(source)
        if end - self.position > self.capacity:
            raise ValueError('Capture worker missed the bounded buffer deadline')
        pcm = array('h')
        while self.position < end:
            for channel in range(self.channels):
                offset = (self.position % self.capacity) * self.channels + channel
                sample = self._ring[offset]
                self._ring[offset] = 0
                pcm.append(max(-32768, min(32767, round(sample * 32768))))
            self.position += 1
        return struct.pack(f'<{len(pcm)}h', *pcm)


def _session(rng, rate=1000, channels=2, sources=3, packets=40):
    """Interleaved add()/render_until() calls: jittery stamps, ~1% clock drift,
    gaps, loud, NaN and infinite samples."""
    events = []
    stamps = [0] * sources
    for _ in range(packets):
        for source in range(sources):
            frames = rng.randint(1, 30)
            values = []
            for _ in range(frames * channels):
                pick = rng.random()
                if pick < .02:
                    values.append(float('nan'))
                elif pick < .04:
                    values.append(float('inf') * rng.choice((1, -1)))
                elif pick < .06:
                    values.append(rng.uniform(-3, 3))
                else:
                    values.append(rng.uniform(-1, 1))
            data = struct.pack(f'<{len(values)}f', *values)
            events.append(('add', source, AudioPacket(stamps[source], data)))
            duration = frames * 10_000_000 // rate
            drift = rng.choice((0, 0, rng.randint(-1, 1) * duration // 100))
            gap = rng.choice((0, 0, 0, rng.randint(1, 5) * 10_000_000 // rate))
            stamps[source] += max(1, duration + drift + gap)
        events.append(('render', min(stamps) - 50 * 10_000_000 // rate))
    events.append(('render', max(stamps) + 10_000_000))
    return events


def _play(mixer_type, events, rate=1000, channels=2, sources=3, snap=()):
    mixer = mixer_type(rate, channels, 0, source_count=sources, snap_sources=snap)
    out = []
    for event in events:
        try:
            if event[0] == 'add':
                mixer.add(event[1], event[2])
            else:
                out.append(mixer.render_until(event[1]))
        except ValueError as error:
            out.append(('error', str(error)))
            break
    return out, mixer.late_packets, mixer.late_frames


def test_numpy_mixer_renders_exactly_what_the_loop_did():
    for seed in range(60):
        rng = random.Random(seed)
        channels = rng.choice((1, 2))
        snap = rng.choice(((), (0,)))
        events = _session(rng, channels=channels)
        expected = _play(LoopMixer, events, channels=channels, snap=snap)
        assert _play(TimelineMixer, events, channels=channels, snap=snap) == expected, seed


def test_lookahead_sample_from_the_next_packet_is_mixed_the_same():
    # A packet stretched by drift reads one sample from the following packet.
    events = [
        ('add', 0, AudioPacket(0, struct.pack('<10f', *[i / 10 for i in range(10)]))),
        ('add', 0, AudioPacket(100_500, struct.pack('<10f', *([-.75] * 10)))),
        ('add', 0, AudioPacket(201_000, struct.pack('<2f', .25, .25))),
        ('render', 400_000),
    ]
    expected = _play(LoopMixer, events, channels=1, sources=1)
    assert _play(TimelineMixer, events, channels=1, sources=1) == expected
    assert expected[0][0] != b''


def _one_second_of_three_48k_stereo_streams(mixer_type):
    mixer = mixer_type(48000, 2, 0, source_count=3)
    data = struct.pack('<960f', *([.25] * 960))   # 10 ms of stereo
    started = time.perf_counter()
    for step in range(100):
        for source in range(3):
            mixer.add(source, AudioPacket(step * 100_000 + source, data))
        mixer.render_until(step * 100_000)
    return time.perf_counter() - started


def test_three_48k_stereo_streams_mix_many_times_faster_than_the_loop():
    # Measured 1.35 s (loop) against 0.11 s here: the loop spent more CPU
    # than the recorded time on a slow machine. Relative, so CI speed does
    # not matter.
    loop = _one_second_of_three_48k_stereo_streams(LoopMixer)
    vectorized = _one_second_of_three_48k_stereo_streams(TimelineMixer)
    assert vectorized * 4 < loop, (vectorized, loop)
