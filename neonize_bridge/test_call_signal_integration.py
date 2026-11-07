"""Generated PCM at fake native boundaries; no device or codec acceptance."""
import base64
import json

import pytest

from call_audio_delivery import AudioDelivery, SIGNAL_STAGES, counters
from test_call_audio_delivery import active


def payload():
    return {"sampleRate": 48000, "pcm": base64.b64encode(b"\x00\x10" * 2880).decode()}


def stage(stats, name):
    return stats.signal_summary()["stages"][name]


def test_native_acceptance_is_measured_after_writer_result_only():
    fake, media = active()
    stats = counters(media._calls["C"])
    fake.write_audio = lambda *args: False
    media.microphone(payload())
    assert stage(stats, "mic_native_accepted")["frames"] == 0
    fake.write_audio = lambda *args: True
    media.microphone(payload())
    assert stage(stats, "mic_native_accepted")["frames"] == 1
    assert stage(stats, "mic_native_accepted")["samples"] == 960
    assert stage(stats, "mic_native_accepted")["rates_hz"] == [16000]
    assert stage(stats, "remote_native_pcm")["rms_dbfs"] is None


@pytest.mark.parametrize("rate,generation", [(48000, 7), (16000, 8)])
def test_remote_format_rejection_is_not_measured(rate, generation):
    fake, media = active()
    stats = counters(media._calls["C"])
    media._emit_audio("C", media._audio_readers["C"], b"\x00\x10" * 320, rate, generation)
    assert stage(stats, "remote_native_pcm")["frames"] == 0


def test_remote_measurement_precedes_emit_and_survives_emit_failure():
    fake, media = active()
    stats = counters(media._calls["C"])
    media.emit = lambda *args: (_ for _ in ()).throw(RuntimeError("fake emitter"))
    with pytest.raises(RuntimeError):
        media._emit_audio("C", media._audio_readers["C"], b"\x00\x10" * 320, 16000, 7)
    result = stage(stats, "remote_native_pcm")
    assert result["frames"] == 1 and result["dc_rms_dbfs"] == -18.1
    assert result["ac_rms_dbfs"] == -120.0


def test_same_id_replacement_never_mixes_signal_windows():
    fake, media = active()
    old = media._calls["C"]
    claim = media._audio_readers["C"]
    stats = counters(old)
    stats._last = stats._clock()  # Suppress periodic reporting during this interleaving.
    media.microphone(payload())
    assert stage(stats, "mic_native_accepted")["frames"] == 1
    media.handle({"ev": "end", "call": "C", "generation": 7})
    media._placed("C", "5511999990000@c.us", False, "")
    media._emit_audio("C", claim, b"\x00\x10" * 320, 16000, 7)
    new_stats = counters(media._calls["C"])
    assert new_stats is not stats
    assert all(s["frames"] == 0 for s in new_stats.signal_summary()["stages"].values())


def test_periodic_and_final_windows_do_not_repeat_samples_or_log_pcm(caplog):
    now = [0.0]
    stats = AudioDelivery(clock=lambda: now[0])
    pcm = b"\x00\x10" * 320
    with caplog.at_level("INFO", logger="bridge"):
        stats.observe("remote_native_pcm", pcm, 16000)
        stats.report()
        assert not caplog.records
        now[0] = 5
        stats.report()
        stats.report()
        assert len(caplog.records) == 1
        first = caplog.records[0].args
        assert first["window_ms"] == 5000
        assert first["stages"]["remote_native_pcm"]["frames"] == 1
        assert first["stages"]["mic_native_accepted"]["rms_dbfs"] is None
        now[0] = 6
        stats.observe("mic_native_accepted", b"\0\0" * 960, 48000)
        stats.report(force=True)
        stats.report(force=True)
        assert len(caplog.records) == 2
        final = caplog.records[-1].args
        assert final["window_ms"] == 1000
        assert final["stages"]["remote_native_pcm"]["frames"] == 0
        assert final["stages"]["mic_native_accepted"]["zero_frames"] == 1
        json.dumps([first, final], allow_nan=False)
    assert str(pcm) not in caplog.text and base64.b64encode(pcm).decode() not in caplog.text
    assert stats.summary() == {}  # Existing flat counter interface is unchanged.
    with pytest.raises(ValueError):
        stats.observe("private-person-name", pcm, 16000)
    assert set(first["stages"]) == set(SIGNAL_STAGES)


def test_end_flushes_a_short_call_window_once(caplog):
    fake, media = active()
    stats = counters(media._calls["C"])
    stats._last = stats._clock()
    with caplog.at_level("INFO", logger="bridge"):
        media.microphone(payload())
        media.handle({"ev": "end", "call": "C", "generation": 7})
        media.handle({"ev": "end", "call": "C", "generation": 7})
    signals = [r for r in caplog.records if r.msg == "call audio signal summary: %s"]
    assert len(signals) == 1
    assert signals[0].args["stages"]["mic_native_accepted"]["frames"] == 1


def test_invalid_measurement_and_internal_error_do_not_stop_media(monkeypatch):
    fake, media = active()
    stats = counters(media._calls["C"])
    stats._last = stats._clock()
    assert not stats.observe("remote_native_pcm", b"?", 16000)
    assert stage(stats, "remote_native_pcm")["invalid_frames"] == 1
    from call_audio_signal import SignalWindow
    monkeypatch.setattr(SignalWindow, "add", lambda *a: (_ for _ in ()).throw(RuntimeError("sensitive")))
    media.microphone(payload())
    media._emit_audio("C", media._audio_readers["C"], b"\x00\x10" * 320, 16000, 7)
    assert len(fake.written) == 1
    assert stats.summary()["mic_native_write_ok"] == 1
    assert stats.summary()["remote_emit_attempts"] == 1
    assert stats.signal_summary()["measurement_errors"] == 2


def test_failed_signal_log_keeps_window_and_does_not_raise(monkeypatch):
    stats = AudioDelivery(clock=lambda: 0)
    stats.observe("remote_native_pcm", b"\0\0" * 320, 16000)
    import logging
    logger = logging.getLogger("bridge")
    monkeypatch.setattr(logger, "info", lambda *a: (_ for _ in ()).throw(RuntimeError("fake handler")))
    stats.report(force=True)
    assert stage(stats, "remote_native_pcm")["frames"] == 1
