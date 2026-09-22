"""Accented microphone names must not turn into mojibake.

PortAudio hands device names over as UTF-8 and PyAudio 0.2.14 decodes them as
latin-1, so "Mixagem estéreo (Realtek(R) Audio)" was listed as
"Mixagem estÃ©reo (...)" in the microphone combos (reported 2026-09-21). The
mangled name was also what got saved, and it never matched the real name when
a call or a recording resolved the device -- the choice silently fell back to
the default microphone.
"""

import pytest

from core import audio_devices
from core.audio_devices import (
    _match_device,
    _pyaudio_input_devices,
    repair_device_name,
    repair_stored_input_device_names,
)
from core.call_audio import CallAudioSession

REAL = "Mixagem estéreo (Realtek(R) Audio)"
MANGLED = REAL.encode("utf-8").decode("latin-1")   # what PyAudio returns


def test_the_reported_name_is_repaired():
    assert MANGLED == "Mixagem estÃ©reo (Realtek(R) Audio)"
    assert repair_device_name(MANGLED) == REAL


@pytest.mark.parametrize("name", [
    REAL,                                   # already right
    "Microfone (USB Audio Device)",         # plain ASCII
    "Mikrofon (Urządzenie zgodne z USB)",   # Polish, not latin-1 encodable
    "Hoparlör (Realtek(R) Audio)",          # Turkish
    "",
])
def test_a_correct_name_is_left_alone(name):
    assert repair_device_name(name) == name


def test_repairing_twice_changes_nothing_more():
    assert repair_device_name(repair_device_name(MANGLED)) == REAL


def test_polish_mojibake_is_repaired_too():
    real = "Mikrofon (Urządzenie zgodne z USB)"
    assert repair_device_name(real.encode("utf-8").decode("latin-1")) == real


class _FakePyAudio:
    def get_host_api_info_by_type(self, _kind):
        return {"index": 0, "deviceCount": 2}

    def get_device_info_by_host_api_device_index(self, _host, i):
        return [
            {"index": 7, "name": MANGLED, "maxInputChannels": 2},
            {"index": 8, "name": "Speakers", "maxInputChannels": 0},
        ][i]


def test_the_pyaudio_list_shows_the_real_name(monkeypatch):
    if audio_devices.pyaudio is None:
        monkeypatch.setattr(audio_devices, "pyaudio", type("P", (), {"paWASAPI": 13}))
    assert _pyaudio_input_devices(_FakePyAudio()) == [(7, REAL)]


def test_a_name_saved_as_mojibake_still_finds_its_device():
    assert _match_device(MANGLED, [(3, "Other"), (7, REAL)]) == 7
    assert _match_device(REAL, [(7, MANGLED)]) == 7


def test_saved_microphone_names_are_repaired_in_both_sections():
    settings = {
        "audio_devices": {"input_device_name": MANGLED},
        "call_audio_devices": {"input_device_name": MANGLED, "output_device_name": "x"},
    }

    assert repair_stored_input_device_names(settings) is True
    assert settings["audio_devices"]["input_device_name"] == REAL
    assert settings["call_audio_devices"]["input_device_name"] == REAL
    # nothing left to do the second time: no needless save on every launch
    assert repair_stored_input_device_names(settings) is False


def test_settings_without_those_sections_are_untouched():
    settings = {"general": {}}
    assert repair_stored_input_device_names(settings) is False
    assert settings == {"general": {}}


def test_a_call_resolves_a_mojibake_name_to_the_real_device():
    assert CallAudioSession._normalized_name(MANGLED) == CallAudioSession._normalized_name(REAL)


def test_settings_load_runs_the_repair():
    import inspect
    from main import MainWindow
    assert "repair_stored_input_device_names(self.settings)" in inspect.getsource(
        MainWindow._migrate_settings
    )
