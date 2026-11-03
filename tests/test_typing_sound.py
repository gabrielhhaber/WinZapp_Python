"""Bundled cue and fallback resolution, without opening an audio device."""
import json
from pathlib import Path
import wave
from types import SimpleNamespace
import pytest

from core import typing_sound
from core.sound_system import NullSound, SOUND_EVENTS
from tests.locales import registered_locale_codes, load_strings


def test_bundled_cue_is_registered_short_valid_pcm_and_named_in_every_locale():
    root = Path(__file__).resolve().parents[1]
    assert ("typing", "typing.wav") in SOUND_EVENTS
    manifest = json.loads((root / "client/sounds/default/default.pack.json").read_text(encoding="utf-8"))
    assert manifest["events"]["typing"] == "typing.wav"
    for locale in registered_locale_codes():
        assert load_strings(locale)["sound_event_typing"]


@pytest.mark.parametrize("bad", ["missing", "invalid override", "invalid pack", "all invalid"])
def test_custom_and_pack_failures_fall_back_once_and_remain_stoppable(tmp_path, monkeypatch, bad):
    custom = tmp_path / "custom.wav"
    active_file = tmp_path / "active.wav"
    default_file = tmp_path / "default.wav"
    for path in (custom, active_file, default_file):
        path.write_bytes(b"not decoded by this test")
    active = {"dir": str(tmp_path), "events": {"typing": "active.wav"}}
    default = {"dir": str(tmp_path), "events": {"typing": "default.wav"}}
    if bad == "missing":
        custom.unlink()
    calls = []
    success = SimpleNamespace()
    def load(system, path, **options):
        calls.append((path, options))
        if bad == "all invalid" or path == str(custom) or (bad == "invalid pack" and path == str(active_file)):
            return NullSound()
        return success
    monkeypatch.setattr(typing_sound, "load_sound", load)
    sound = typing_sound.load_typing_sound(None, active, default, str(custom), "custom-pack")
    assert len({path for path, _ in calls}) == len(calls) <= 3
    assert all(options == {"event_key": "typing", "pack_id": "custom-pack", "allow_device_recovery": False}
               for _, options in calls)
    if bad == "all invalid":
        assert isinstance(sound, NullSound)
    else:
        assert sound is success
        expected = default_file if bad == "invalid pack" else active_file
        assert calls[-1][0] == str(expected)


@pytest.mark.parametrize("filename", ["typing.wav", "typing_bubble.wav", "typing_pulse.wav"])
def test_selected_bundled_alternatives_are_short_pcm_and_load_as_user_overrides(filename, monkeypatch):
    root = Path(__file__).resolve().parents[1] / "client/sounds/default"
    path = root / filename
    with wave.open(str(path), "rb") as audio:
        assert audio.getnchannels() in (1, 2)
        assert audio.getsampwidth() == 2
        assert 16000 <= audio.getframerate() <= 48000
        assert 0 < audio.getnframes() / audio.getframerate() <= 1.5
        assert len(audio.readframes(audio.getnframes())) == audio.getnframes() * audio.getnchannels() * 2
    pack = {"dir": str(root), "events": {"typing": "typing.wav"}}
    loaded = []
    selected = SimpleNamespace()
    def load(system, resolved, **options):
        loaded.append((resolved, options))
        return selected
    monkeypatch.setattr(typing_sound, "load_sound", load)
    assert typing_sound.load_typing_sound(None, pack, pack, str(path), "default") is selected
    assert loaded == [(str(path), {"event_key": "typing", "pack_id": "default", "allow_device_recovery": False})]
