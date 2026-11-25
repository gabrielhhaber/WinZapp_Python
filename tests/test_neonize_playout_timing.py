"""Golden native delivery schedules through the real Python playback reservoir.

The schedules are pinned by meowcaller's
TestAudioPlayoutBufferedSinkScheduleMatchesConsumerTimingContract. No device,
socket, wall-clock sleep or codec is involved; this measures local queueing,
not provider/hardware end-to-end call latency.
"""
import numpy as np
import pytest

from core.call_audio import _OutputJitterBuffer, output_buffer_profile


def render(arrivals, *, buffered, rate=48000):
    # Baseline's first two 60 ms frames leave together on packet three;
    # subsequent packets release their predecessor. The opt-in sink delivers
    # each decoded frame immediately. Native Go tests pin both schedules.
    if buffered:
        deliveries = list(zip(arrivals, range(len(arrivals))))
    else:
        deliveries = [(arrivals[2], 0), (arrivals[2], 1)]
        deliveries += [(arrivals[i], i-1) for i in range(3, len(arrivals))]
    reservoir = _OutputJitterBuffer(rate, neonize=True)
    step, packet = rate * 5 // 1000, rate * 60 // 1000
    cursor = 0
    first = None
    played = []
    for now in range(0, arrivals[-1] + 1, 5):
        while cursor < len(deliveries) and deliveries[cursor][0] <= now:
            _, identifier = deliveries[cursor]
            reservoir.push(np.full(packet, (identifier+1)/100, dtype=np.float32))
            cursor += 1
        out = np.empty(step, dtype=np.float32)
        reservoir.fill(out)
        if first is None and np.any(out):
            first = now
        if first is not None:
            played.append(out)
        assert reservoir.buffered_samples <= reservoir._max_samples
    return first, np.concatenate(played), reservoir


@pytest.mark.parametrize("rate", [16000, 48000])
def test_one_downstream_reservoir_starts_one_codec_packet_earlier(rate):
    arrivals = [0, 60, 120, 180, 240, 300]
    before, old_pcm, old = render(arrivals, buffered=False, rate=rate)
    after, new_pcm, new = render(arrivals, buffered=True, rate=rate)
    assert (before, after) == (120, 60)
    assert old.underruns == new.underruns == 0
    assert old.dropped_samples == new.dropped_samples == 0
    np.testing.assert_array_equal(old_pcm, new_pcm[:len(old_pcm)])
    assert new._prebuffer_samples == old._prebuffer_samples == rate * 120 // 1000
    assert output_buffer_profile(True) == (120, 240, 360)


@pytest.mark.parametrize("arrivals", [
    [0, 75, 110, 185, 230, 315],  # variable inter-packet delay
    [0, 60, 180, 180, 240, 300],  # two packets delivered in a burst
])
def test_earlier_delivery_preserves_pcm_without_starvation_under_jitter(arrivals):
    before, old_pcm, old = render(arrivals, buffered=False)
    after, new_pcm, new = render(arrivals, buffered=True)
    assert after < before
    assert old.underruns == new.underruns == 0
    assert old.dropped_samples == new.dropped_samples == 0
    np.testing.assert_array_equal(old_pcm, new_pcm[:len(old_pcm)])


def test_a_network_outage_keeps_the_existing_starvation_and_refill_policy():
    arrivals = [0, 60, 120, 180, 600, 660, 720, 780, 840]
    _, _, old = render(arrivals, buffered=False)
    _, pcm, new = render(arrivals, buffered=True)
    assert old.underruns == new.underruns == 1
    assert old._target_samples == new._target_samples
    assert old.dropped_samples == new.dropped_samples == 0
    assert np.any(pcm == 0)  # the outage is not hidden by replaying old speech


def test_wppconnect_playback_profile_keeps_its_existing_policy():
    assert output_buffer_profile(False) == (60, 120, 200)
