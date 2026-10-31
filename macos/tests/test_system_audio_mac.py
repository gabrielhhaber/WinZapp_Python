"""The capture behind Ctrl+Shift+H on macOS. No audio device is opened here."""

import array
import types

import pytest

from winzapp_mac import system_audio_mac as sam


class _Stream:
    def __init__(self):
        self.calls = []

    def start_stream(self):
        self.calls.append("start")

    def stop_stream(self):
        self.calls.append("stop")

    def close(self):
        self.calls.append("close")


class _Pa:
    def __init__(self):
        self.opened = {}
        self.terminated = False
        self.stream = _Stream()

    def open(self, **kw):
        self.opened = kw
        return self.stream

    def terminate(self):
        self.terminated = True


def _capture(**kw):
    pa = _Pa()
    return pa, sam.PortAudioCapture(pa, 7, 48000, 2, **kw)


def test_a_stamp_ignores_arrival_jitter():
    packets = sam.PacketClock(48000)
    first = packets.stamp(480, 1_000_000)
    assert packets.stamp(480, 1_100_500 + 5_000) == first + 100_000
    assert packets.stamp(480, 1_200_000) == first + 200_000


def test_a_real_gap_follows_the_clock():
    packets = sam.PacketClock(48000)
    packets.stamp(480, 1_000_000)
    assert packets.stamp(480, 3_000_000) == 2_900_000


def test_reset_starts_a_new_timeline():
    packets = sam.PacketClock(48000)
    packets.stamp(480, 1_000_000)
    packets.reset()
    assert packets.stamp(480, 1_100_000) == 1_000_000


def test_opening_does_not_start_and_reads_32_bit_float():
    pa, stream = _capture()
    assert pa.opened["start"] is False and pa.opened["format"] == 1
    assert pa.opened["input_device_index"] == 7
    assert pa.stream.calls == []


def test_audio_reaches_the_recorder_as_packets():
    _pa, stream = _capture()
    assert stream._on_audio(b"\0" * 3840, 480, None, 0) == (None, 0)
    packets = stream.read_packets()
    assert [len(p.data) for p in packets] == [3840]
    assert stream.read_packets() == []


def test_the_queue_is_bounded_and_counts_what_it_drops(monkeypatch):
    monkeypatch.setattr(sam, "_MAX_QUEUED_PACKETS", 2)
    _pa, stream = _capture()
    for _ in range(3):
        stream._on_audio(b"\0" * 8, 1, None, 0)
    assert len(stream.read_packets()) == 2
    assert stream.diagnostic_snapshot()["discontinuities"] == 1


def test_stop_and_close_are_idempotent():
    pa, stream = _capture()
    stream.start()
    stream.stop()
    stream.stop()
    stream.close()
    stream.close()
    assert pa.stream.calls == ["start", "stop", "close"]


def test_interleaved_audio_passes_through():
    assert sam.interleaved([(2, b"abcdefgh")], 2) == b"abcdefgh"


def test_one_buffer_per_channel_is_interleaved():
    left, right = array.array("f", [1, 2]), array.array("f", [3, 4])
    data = sam.interleaved([(1, left.tobytes()), (1, right.tobytes())], 2)
    assert array.array("f", data).tolist() == [1, 3, 2, 4]


def test_a_missing_channel_is_an_error_not_silence():
    with pytest.raises(sam.MacCaptureError):
        sam.interleaved([(1, b"\0" * 8)], 2)


class _Tap:
    device = 5

    def __init__(self):
        self.rates = []
        self.api = types.SimpleNamespace(
            AudioDeviceCreateIOProcID=lambda *a: 0, AudioDeviceStart=lambda *a: 0,
            AudioDeviceStop=lambda *a: 0, AudioDeviceDestroyIOProcID=lambda *a: 0)

    def set_rate(self, rate):
        self.rates.append(rate)


def test_the_tap_stream_fixes_the_rate_and_failures_are_counted_not_raised():
    tap = _Tap()
    stream = sam.TapCapture(tap, 48000, 2)
    assert tap.rates == [48000]
    assert stream._on_audio(5, None, None, None, None, None, None) == 0
    assert stream.diagnostic_snapshot()["discontinuities"] == 1
    stream.close()


def test_older_macos_keeps_the_feature_off(monkeypatch):
    monkeypatch.setattr(sam, "_macos_version", lambda: (14, 3))
    assert sam.available() is False
    monkeypatch.setattr(sam, "_macos_version", lambda: (14, 4))
    assert sam.available() is True
