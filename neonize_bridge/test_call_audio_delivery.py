"""Delivery diagnostics are bounded, private and distinguish native rejection."""
import base64

import pytest

from call_audio_delivery import AudioDelivery, NAMES, counters
from test_call_placement_state import prepared, snapshot


def test_reports_are_rate_limited_and_never_accept_arbitrary_fields(caplog):
    now = [0.0]
    stats = AudioDelivery(clock=lambda: now[0])
    with caplog.at_level("INFO", logger="bridge"):
        stats.add("mic_packets")
        stats.report()
        assert caplog.records == []
        now[0] = 5
        stats.report()
        stats.report()
        assert len(caplog.records) == 1
        stats.report(force=True)
        assert len(caplog.records) == 2
    with pytest.raises(ValueError):
        stats.add("identity@example.invalid")
    with pytest.raises(ValueError):
        stats.add("mic_bytes", -1)
    assert set(stats.summary()) <= NAMES


def active():
    fake, media, sent, started = prepared(snapshot())
    fake.dial = lambda *a: {"call": "C", "generation": 7}
    media.dial("5511999990000@c.us")
    return fake, media


def test_native_false_is_counted_as_rejection_not_success():
    fake, media = active()
    fake.write_audio = lambda *args: False
    payload = {"sampleRate": 48000, "pcm": base64.b64encode(b"\x01\x00" * 2880).decode()}
    media.microphone(payload)
    stats = counters(media._calls["C"]).summary()
    assert stats["mic_packets"] == 1 and stats["mic_native_rejected"] == 1
    assert stats.get("mic_native_write_ok", 0) == 0
    fake.write_audio = lambda *args: True
    media.microphone(payload)
    assert counters(media._calls["C"]).summary()["mic_native_write_ok"] == 1


@pytest.mark.parametrize("data, category", [(None, "mic_invalid_payload"),
    ({"pcm": "???"}, "mic_invalid_payload"),
    ({"pcm": "AA=="}, "mic_invalid_format"),
    ({"pcm": "AAA=", "sampleRate": 44100}, "mic_invalid_format")])
def test_invalid_payloads_have_safe_counts(data, category):
    fake, media = active()
    media.microphone(data)
    assert counters(media._calls["C"]).summary()[category] == 1
    assert fake.written == []


def test_counters_stay_with_the_old_record_after_same_id_replacement():
    fake, media = active()
    old = media._calls["C"]
    counters(old).add("mic_packets", 3)
    media.handle({"ev": "end", "call": "C", "generation": 7})
    media._placed("C", "5511999990000@c.us", False, "")
    assert counters(media._calls["C"]).summary() == {}
    assert counters(old).summary() == {"mic_packets": 3}
