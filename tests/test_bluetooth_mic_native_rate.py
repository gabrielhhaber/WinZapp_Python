"""A recording device is asked for its own sample rate before the fixed list.

Reported from a real install on 2026-09-09 — selecting a Bluetooth headset as
the recording device failed with "could not activate the selected recording
device", four times across two sessions:

    [audio_devices] Input device test failed for every sample-rate/channel
    combo (index=14)      ... and index=19 in the later session

PyAudio was present (that warning only exists on the branch where it loaded)
and the device was enumerated and found. What failed is `pa.open()`, for every
combination in RECORDING_SAMPLE_CONFIGS — 48000 and 44100.

That list was written for WASAPI devices, whose native rate is 48000 on
everything tested. A Bluetooth headset used as a *microphone* is the family it
leaves out: recording takes the device out of A2DP and into the Hands-Free
Profile, whose SCO link is mono at 8000 Hz (CVSD) or 16000 Hz (mSBC). Neither
fixed rate is on offer, so the device looks broken and is not.

The failure was never confined to Settings validation. RECORDING_SAMPLE_CONFIGS
says it must mirror _start_voice_recording()'s chain and did, so recording with
that headset could not have worked either — the dialog was reporting a limit
that is WinZapp's.

Asking the device is what makes the fix general: PortAudio already reports
`defaultSampleRate`, so 8000 and 16000 are covered without being named, and so
is whatever comes next. The fixed list stays as the tail, because a default is
not the only rate a device accepts.
"""

import pytest

import core.audio_devices as audio_devices
from core.audio_devices import RECORDING_SAMPLE_CONFIGS, recording_configs_for


class _FakePyAudio:
    """Just the one call recording_configs_for() makes."""

    def __init__(self, info=None, raises=False):
        self._info = info or {}
        self._raises = raises
        self.terminated = 0

    def get_device_info_by_index(self, index):
        if self._raises:
            raise OSError("device gone")
        return self._info

    def terminate(self):
        self.terminated += 1


@pytest.fixture(autouse=True)
def _pyaudio_present(monkeypatch):
    """The module degrades to the fixed list when PyAudio is absent; these
    tests are about the branch where it is there."""
    monkeypatch.setattr(audio_devices, "pyaudio", object())


def _hfp(rate):
    """What Windows reports for a Bluetooth headset's HFP microphone."""
    return {"defaultSampleRate": float(rate), "maxInputChannels": 1}


class TestTheDevicesOwnRateComesFirst:
    @pytest.mark.parametrize("rate", [8000, 16000])
    def test_a_bluetooth_headset_is_offered_its_own_link_rate(self, rate):
        """The reported failure: neither 48000 nor 44100 is on offer, so every
        combination was refused before this."""
        configs = recording_configs_for(3, _FakePyAudio(_hfp(rate)))
        assert configs[0] == (rate, 1)

    def test_a_single_channel_device_is_never_asked_for_two(self):
        """An HFP microphone is mono. Asking for stereo is one more refusal on
        a path whose whole cost is refusals."""
        configs = recording_configs_for(3, _FakePyAudio(_hfp(16000)))
        assert (16000, 2) not in configs

    def test_a_stereo_device_gets_mono_first_anyway(self):
        """WhatsApp voice messages are mono, and a stereo capture costs a
        downmix loop in pure Python — the reason the fixed list is ordered that
        way too."""
        info = {"defaultSampleRate": 32000.0, "maxInputChannels": 2}
        configs = recording_configs_for(3, _FakePyAudio(info))
        assert configs[:2] == [(32000, 1), (32000, 2)]

    def test_the_fixed_list_still_follows(self):
        """A default is not the only rate a device accepts, and a driver that
        reports one thing and opens another is what this chain exists for."""
        configs = recording_configs_for(3, _FakePyAudio(_hfp(16000)))
        assert configs[-len(RECORDING_SAMPLE_CONFIGS):] == RECORDING_SAMPLE_CONFIGS

    def test_a_native_rate_already_in_the_list_is_not_repeated(self):
        info = {"defaultSampleRate": 48000.0, "maxInputChannels": 2}
        configs = recording_configs_for(3, _FakePyAudio(info))
        assert configs == RECORDING_SAMPLE_CONFIGS
        assert len(configs) == len(set(configs))


class TestItNeverMakesThingsWorse:
    def test_an_unreadable_device_falls_back_to_the_old_behaviour(self):
        configs = recording_configs_for(3, _FakePyAudio(raises=True))
        assert configs == RECORDING_SAMPLE_CONFIGS

    @pytest.mark.parametrize("value", [None, 0, "", "nonsense"])
    def test_a_missing_or_junk_rate_is_ignored(self, value):
        info = {"defaultSampleRate": value, "maxInputChannels": 1}
        configs = recording_configs_for(3, _FakePyAudio(info))
        assert configs == RECORDING_SAMPLE_CONFIGS

    def test_without_pyaudio_it_answers_the_fixed_list(self, monkeypatch):
        monkeypatch.setattr(audio_devices, "pyaudio", None)
        assert recording_configs_for(3) == RECORDING_SAMPLE_CONFIGS

    def test_a_borrowed_pyaudio_instance_is_not_terminated(self):
        """Both callers pass their own live instance — test_input_device()
        goes on using it for pa.open(), and _start_voice_recording() keeps it
        for the whole recording."""
        pa = _FakePyAudio(_hfp(16000))
        recording_configs_for(3, pa)
        assert pa.terminated == 0


def test_both_call_sites_resolve_per_device():
    """The constant's own comment requires the validation and the recording to
    try the same combinations; two copies of that decision is how they would
    drift apart."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "client"
    for rel in ("core/audio_devices.py", "ui/conversations.py"):
        source = (root / rel).read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "recording_configs_for"
        ]
        assert calls, f"{rel} no longer resolves the combinations per device"
