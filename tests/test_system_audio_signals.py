"""Synthetic signal checks of the real mixer, without opening audio devices."""
from array import array
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "client"))
from core.system_audio_capture import AudioPacket, TimelineMixer


def floats(samples):
    data = array("f", samples)
    if sys.byteorder != "little":
        data.byteswap()
    return data.tobytes()


def pcm_samples(data):
    samples = array("h")
    samples.frombytes(data)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


class SyntheticSignalTests(unittest.TestCase):
    def test_distinct_microphone_and_output_signals_survive_stereo_mix(self):
        rate, duration = 48000, 0.5
        length = int(rate * duration)
        mic = [0.35 * math.sin(2 * math.pi * 440 * n / rate) for n in range(length)]
        left = [0.3 * math.sin(2 * math.pi * 880 * n / rate) for n in range(length)]
        right = [0.25 * math.sin(2 * math.pi * 1320 * n / rate) for n in range(length)]
        mixer = TimelineMixer(rate, 2, 0)
        output = []
        # Different packet boundaries for the two independent capture sources.
        cursor = [0, 0]
        for deadline in range(480, length + 1, 480):
            for source, block in ((0, 137), (1, 193)):
                while cursor[source] < deadline:
                    start = cursor[source]
                    end = min(start + block, length)
                    signal = []
                    for n in range(start, end):
                        signal.extend((mic[n], mic[n]) if source == 0 else (left[n], right[n]))
                    mixer.add(source, AudioPacket(round(start * 10_000_000 / rate), floats(signal)))
                    cursor[source] = end
            output.append(mixer.render_until(max(0, deadline - 480) * 10_000_000 // rate))
        output.append(mixer.render_until(length * 10_000_000 // rate))
        samples = pcm_samples(b"".join(output))
        self.assertEqual(len(samples), length * 2)
        error = 0
        for n in range(length):
            for channel, system in ((0, left), (1, right)):
                expected = round((mic[n] + system[n]) * 0.5 * 32768)
                error = max(error, abs(samples[n * 2 + channel] - expected))
        self.assertLessEqual(error, 1, "mix must retain both signals and channel identity")

    def test_mono_loopback_silence_then_audio_keeps_alignment(self):
        rate = 48000
        mixer = TimelineMixer(rate, 1, 0)
        chunks = []
        for n in range(20):
            stamp = n * 100_000  # 10ms
            mixer.add(0, AudioPacket(stamp, floats([0.25] * 480)))
            if n >= 10:
                mixer.add(1, AudioPacket(stamp, floats([0.5] * 480)))
            chunks.append(mixer.render_until(max(0, stamp - 100_000)))
        chunks.append(mixer.render_until(2_000_000))
        samples = pcm_samples(b"".join(chunks))
        self.assertEqual(list(samples[:4800]), [4096] * 4800)
        self.assertEqual(list(samples[4800:]), [12288] * 4800)


if __name__ == "__main__":
    unittest.main()
