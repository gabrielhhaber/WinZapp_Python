"""Analytic generated PCM checks; no devices, native codec or signal capture."""
import json
import math
import struct

import pytest

from call_audio_signal import SignalWindow


LEVELS = ("rms_dbfs", "ac_rms_dbfs", "dc_rms_dbfs", "peak_dbfs")


def pcm(values):
    return b"".join(struct.pack("<h", value) for value in values)


def db(amplitude):
    return round(20 * math.log10(amplitude / 32768), 1)


def test_no_samples_is_distinct_from_valid_digital_silence():
    window = SignalWindow()
    assert all(window.summary()[key] is None for key in LEVELS)
    assert window.add(b"\0\0" * 320, 16000)
    result = window.summary()
    assert (result["frames"], result["samples"], result["zero_frames"]) == (1, 320, 1)
    assert result["duration_ms"] == 20 and result["rates_hz"] == [16000]
    assert all(result[key] == -120.0 for key in LEVELS)


@pytest.mark.parametrize("value", [-16384, 1, 16384])
def test_constant_frame_is_dc_with_zero_ac(value):
    window = SignalWindow()
    assert window.add(pcm([value] * 960), 16000)
    result = window.summary()
    assert result["ac_rms_dbfs"] == -120.0
    assert result["dc_rms_dbfs"] == result["rms_dbfs"] == db(abs(value))
    assert result["peak_dbfs"] == db(abs(value))
    assert result["zero_frames"] == 0


def test_opposite_signed_dc_frames_do_not_cancel_into_ac():
    window = SignalWindow()
    for value in (8192, -8192, 8192, -8192):
        assert window.add(pcm([value] * 320), 16000)
    result = window.summary()
    assert result["ac_rms_dbfs"] == -120.0
    assert result["dc_rms_dbfs"] == result["rms_dbfs"] == -12.0
    assert result["frames"] == 4


@pytest.mark.parametrize("rate", [16000, 48000])
@pytest.mark.parametrize("offset", [0, 4096])
def test_integer_cycle_sine_separates_ac_and_dc(rate, offset):
    # 20 complete 1-kHz cycles: zero-mean sine with analytic RMS A/sqrt(2).
    values = [round(8192 * math.sin(2 * math.pi * 1000 * n / rate)) + offset
              for n in range(rate // 50)]
    window = SignalWindow()
    assert window.add(pcm(values), rate)
    result = window.summary()
    assert result["ac_rms_dbfs"] == pytest.approx(db(8192 / math.sqrt(2)), abs=0.1)
    assert result["dc_rms_dbfs"] == (-120.0 if not offset else db(offset))
    assert result["rms_dbfs"] == pytest.approx(db(math.sqrt(8192 ** 2 / 2 + offset ** 2)), abs=0.1)
    assert result["peak_dbfs"] == db(8192 + offset)


def test_impulse_has_expected_sample_weighted_energy():
    window = SignalWindow()
    assert window.add(pcm([16384] + [0] * 255), 16000)
    result = window.summary()
    assert result["rms_dbfs"] == db(16384 / 16)
    assert result["dc_rms_dbfs"] == db(16384 / 256)
    assert result["ac_rms_dbfs"] == pytest.approx(db(16384 * math.sqrt(255) / 256), abs=0.1)
    assert result["peak_dbfs"] == db(16384)


def test_full_scale_boundaries_are_counted_without_clipping_verdict():
    window = SignalWindow()
    assert window.add(pcm([-32768, 32767, -32767, 32766, 0]), 48000)
    result = window.summary()
    assert result["full_scale_samples"] == 2
    assert result["peak_dbfs"] == 0
    assert not any("clip" in key or "speech" in key or "heard" in key for key in result)


def test_weight_by_samples_not_frames_and_track_both_rate_durations():
    window = SignalWindow()
    assert window.add(pcm([8192] * 160), 16000)  # 10 ms
    assert window.add(pcm([-16384] * 480), 48000)  # 10 ms, three times samples
    result = window.summary()
    expected = math.sqrt((160 * 8192 ** 2 + 480 * 16384 ** 2) / 640)
    assert result["rms_dbfs"] == db(expected)
    assert result["dc_rms_dbfs"] == db(expected)
    assert result["ac_rms_dbfs"] == -120.0
    assert result["samples"] == 640 and result["duration_ms"] == 20
    assert result["rates_hz"] == [16000, 48000]


def test_nearly_constant_fullscale_values_keep_small_ac_variance():
    window = SignalWindow()
    assert window.add(pcm([32766, 32767] * 12000), 48000)
    result = window.summary()
    assert result["ac_rms_dbfs"] == db(0.5)
    assert result["full_scale_samples"] == 12000
    assert result["duration_ms"] == 500


@pytest.mark.parametrize("factory", [bytes, bytearray, memoryview])
def test_accepts_bytes_like_and_never_retains_input(factory):
    original = bytearray(pcm([1234] * 320))
    value = factory(original)
    window = SignalWindow()
    assert window.add(value, 16000)
    before = window.summary()
    original[:] = b"\0" * len(original)
    assert window.summary() == before
    # Object state contains only numeric aggregates and the two-rate map.
    for name in window.__slots__:
        field = getattr(window, name)
        if isinstance(field, dict):
            assert set(field) == {16000, 48000}
            assert all(type(n) is int for n in field.values())
        else:
            assert type(field) in (int, float)


@pytest.mark.parametrize("data,rate", [
    (None, 16000), ("\0\0", 16000), ([0, 0], 16000), (2, 16000),
    (b"", 16000), (b"\0", 16000), (b"\0\0\0", 16000),
    (b"\0\0", 8000), (b"\0\0", 44100), (b"\0\0", 16000.0),
    (b"\0\0", "16000"), (b"\0\0", None), (b"\0\0", True),
    (b"\0" * 16002, 16000), (b"\0" * 48002, 48000),
    (memoryview(b"\0" * 8)[::2], 16000),
], ids=["none", "text", "list", "integer", "empty", "one-byte", "odd",
        "8khz", "44khz", "float-rate", "text-rate", "none-rate", "bool-rate",
        "oversize-16khz", "oversize-48khz", "noncontiguous"])
def test_invalid_frames_are_counted_without_changing_valid_totals(data, rate):
    window = SignalWindow()
    assert window.add(pcm([123] * 20), 16000)
    before = window.summary()
    assert window.add(data, rate) is False
    after = window.summary()
    assert after.pop("invalid_frames") == 1
    before.pop("invalid_frames")
    assert after == before


def test_summary_reset_clears_every_counter_and_does_not_alias_rates():
    window = SignalWindow()
    window.add(pcm([-32768, 0]), 48000)
    window.add(b"?", 48000)
    first = window.summary(reset=True)
    assert first["frames"] == first["invalid_frames"] == first["full_scale_samples"] == 1
    empty = window.summary()
    assert all(empty[key] == 0 for key in ("frames", "samples", "invalid_frames", "zero_frames", "full_scale_samples", "duration_ms"))
    assert all(empty[key] is None for key in LEVELS)
    assert empty["rates_hz"] == []
    first["rates_hz"].append(123)
    assert window.summary()["rates_hz"] == []


def test_json_is_finite_for_no_data_silence_boundary_and_dc():
    window = SignalWindow()
    json.dumps(window.summary(), allow_nan=False)
    for values in ([0] * 100, [-32768, 32767] * 50, [12345] * 100):
        assert window.add(pcm(values), 16000)
        result = window.summary(reset=True)
        json.dumps(result, allow_nan=False)
        assert all(math.isfinite(result[key]) for key in LEVELS)
