"""Owned waiting audio and speech handoff; every audio/device call is fake."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.ai_media.feedback import ProcessingCue
from core.ai_media.transcript import render_history, spoken_answer
from core import sound_system


def _window():
    return SimpleNamespace(sound_system=SimpleNamespace(enabled=True), settings={},
                           _default_sound_pack={"id": "default"},
                           get_active_sound_pack=lambda: {"id": "custom"})


def _fake_audio(monkeypatch, *, failing=None):
    events = []
    class FakeSound:
        def play(self):
            events.append("play")
            if failing == "play":
                raise RuntimeError("audio unavailable")
        def stop(self):
            events.append("stop")
            if failing == "stop":
                raise RuntimeError("stale handle")
        def free(self):
            events.append("free")
            if failing == "free":
                raise RuntimeError("already freed")
    def load(system, path, **kwargs):
        events.append(("load", path, kwargs))
        return FakeSound()
    monkeypatch.setattr(sound_system, "load_sound", load)
    monkeypatch.setattr(sound_system, "resolve_sound_event_path", lambda *args: "synthetic.ogg")
    return events


def test_waiting_audio_has_an_owned_loop_and_stops_then_frees_once(monkeypatch):
    events = _fake_audio(monkeypatch)
    cue = ProcessingCue(_window())
    cue.start()
    assert events == [("load", "synthetic.ogg", {"event_key": "ai_processing", "pack_id": "custom",
                                                "looping": True, "allow_device_recovery": False}), "play"]
    cue.stop()
    cue.stop()
    assert events[-2:] == ["stop", "free"] and len(events) == 4
    assert cue._sound is None


def test_restarting_waiting_audio_releases_the_previous_private_stream(monkeypatch):
    events = _fake_audio(monkeypatch)
    cue = ProcessingCue(_window())
    cue.start()
    cue.start()
    assert [event for event in events if isinstance(event, str)] == ["play", "stop", "free", "play"]
    cue.stop()


@pytest.mark.parametrize("disabled", ["background", "system", "event", "missing-system", "missing-file"])
def test_disabled_or_missing_audio_never_opens_a_stream(monkeypatch, disabled):
    events = _fake_audio(monkeypatch)
    window = _window()
    if disabled == "background":
        window.background_mode = True
    elif disabled == "system":
        window.sound_system.enabled = False
    elif disabled == "event":
        window.settings = {"sound_events": {"custom": {"ai_processing": {"enabled": False}}}}
    elif disabled == "missing-system":
        window.sound_system = None
    else:
        monkeypatch.setattr(sound_system, "resolve_sound_event_path", lambda *args: "")
    cue = ProcessingCue(window)
    cue.start()
    cue.stop()
    assert not events


def test_custom_sound_path_uses_the_existing_pack_resolver(monkeypatch):
    events = _fake_audio(monkeypatch)
    window = _window()
    window.settings = {"sound_events": {"custom": {"ai_processing": {"path": "chosen.ogg"}}}}
    resolved = []
    def resolve(*args):
        resolved.append(args)
        return "resolved.ogg"
    monkeypatch.setattr(sound_system, "resolve_sound_event_path", resolve)
    cue = ProcessingCue(window)
    cue.start()
    cue.stop()
    assert resolved == [({"id": "custom"}, {"id": "default"}, "ai_processing", "chosen.ogg")]
    assert events[0][1] == "resolved.ogg"


@pytest.mark.parametrize("failing", ["play", "stop", "free"])
def test_audio_errors_do_not_escape_and_cleanup_remains_idempotent(monkeypatch, failing):
    events = _fake_audio(monkeypatch, failing=failing)
    cue = ProcessingCue(_window())
    cue.start()
    cue.stop()
    cue.stop()
    assert [event for event in events if isinstance(event, str)] == ["play", "stop", "free"]
    assert cue._sound is None


def test_request_owned_sound_cannot_create_an_unstoppable_device_fallback(monkeypatch):
    # Invoke the real Sound.play method, but never a BASS or stream constructor.
    base = sound_system.Sound.__mro__[1]
    monkeypatch.setattr(base, "stop", lambda self: None)
    def fail(*args, **kwargs):
        raise RuntimeError("device unavailable")
    monkeypatch.setattr(base, "play", fail)
    recovery = []
    monkeypatch.setattr(sound_system.stream, "FileStream", lambda **kwargs: recovery.append("new-stream"))
    sound = sound_system.Sound.__new__(sound_system.Sound)
    sound.sound_system = SimpleNamespace(_effects_device=None,
                                        handle_playback_failure=lambda: recovery.append("global-reinit"))
    sound.event_key = sound.pack_id = None
    sound.allow_device_recovery = False
    sound_system.Sound.play(sound)
    assert not recovery


@pytest.mark.parametrize("answer,spoken", [
    ("description", "description"),
    ("First sentence.\n\nSecond sentence.\r\nLast sentence.",
     "First sentence. Second sentence. Last sentence."),
    ("  Mavi\t dikdörtgen.\u2028Kırmızı\u00a0daire.  ", "Mavi dikdörtgen. Kırmızı daire."),
])
@pytest.mark.parametrize("regenerate", [True, False])
def test_dialog_releases_the_real_cue_controller_before_auto_reading(monkeypatch, answer, spoken, regenerate):
    from tests.test_ai_media_ui_logic import DialogStub
    from core.ai_media.payload import Media
    import ui.dialogs.ai_result_dialog as dialog_module
    monkeypatch.setattr(dialog_module, "active_account_id", lambda: "a")
    events = _fake_audio(monkeypatch)
    dialog = DialogStub()
    dialog.config["read_answers"] = True
    dialog._processing = ProcessingCue(_window())
    dialog.main_window.output = lambda text: events.append(("speak", text))
    generation, _ = dialog.session.begin("describe")
    dialog._busy(True)
    dialog._complete(generation, "describe", regenerate, (Media("image", b"photo", "image/jpeg", "image.jpg"), (answer, "gemini")), None)
    assert events[-3:] == ["stop", "free", ("speak", spoken)]
    assert dialog._latest == answer and dialog.session.history[-1] == ("assistant", answer)
    assert dialog.result.value == (answer if regenerate else
                                   f"ai_history_question\ndescribe\n\nai_history_answer\n{answer}")
    assert dialog._processing._sound is None and dialog.copy.enabled


def test_load_sound_passes_request_ownership_without_changing_default_recovery(monkeypatch):
    calls = []
    monkeypatch.setattr(sound_system, "Sound", lambda *args, **kwargs: calls.append(kwargs))
    sound_system.load_sound(object(), "synthetic.ogg", looping=True, allow_device_recovery=False)
    sound_system.load_sound(object(), "synthetic.ogg")
    assert calls[0]["looping"] and calls[0]["allow_device_recovery"] is False
    assert calls[1]["allow_device_recovery"] is True


def test_processing_event_resolves_to_a_bundled_asset_and_has_all_locale_labels():
    root = Path(__file__).resolve().parents[1]
    assert ("ai_processing", "ai_processing.ogg") in sound_system.SOUND_EVENTS
    folder = root / "client" / "sounds" / "default"
    manifest = json.loads((folder / "default.pack.json").read_text(encoding="utf-8"))
    assert manifest["events"]["ai_processing"] == "ai_processing.ogg"
    assert (folder / manifest["events"]["ai_processing"]).is_file()
    from tests.locales import load_strings, registered_locale_codes
    for locale in registered_locale_codes():
        values = load_strings(locale)
        assert values["sound_event_ai_processing"]


def test_digital_signature_is_a_complete_five_second_ogg_vorbis():
    from tests.ogg_info import ogg_vorbis_info
    path = Path(__file__).resolve().parents[1] / "client" / "sounds" / "default" / "ai_processing.ogg"
    channels, rate, frames = ogg_vorbis_info(path)
    assert (channels, rate) == (1, 44100)
    assert frames == 5 * 44100


@pytest.mark.parametrize("selection,expected", [
    ("default", "ai_processing.ogg"),
    ("older_pack", "ai_processing.ogg"),
    ("stale_override", "ai_processing.ogg"),
    ("custom_pack", "synchronizing.ogg"),
    ("custom_override", "synchronizing.ogg"),
])
def test_processing_event_resolver_preserves_pack_and_user_choices(selection, expected):
    folder = Path(__file__).resolve().parents[1] / "client" / "sounds" / "default"
    default = sound_system.discover_sound_packs(str(folder.parent))["default"]
    active = default
    override = ""
    if selection in {"older_pack", "stale_override"}:
        active = {"id": "older", "dir": str(folder), "events": {}}
    if selection == "stale_override":
        override = str(folder / "missing-photo-cue.wav")
    elif selection == "custom_pack":
        active = {"id": "custom", "dir": str(folder),
                  "events": {"ai_processing": "synchronizing.ogg"}}
    elif selection == "custom_override":
        override = str(folder / "synchronizing.ogg")
    resolved = sound_system.resolve_sound_event_path(active, default, "ai_processing", override)
    assert Path(resolved) == folder / expected
    assert Path(resolved).is_file()
    # The independent synchronization event keeps its existing sound.
    assert Path(sound_system.resolve_sound_event_path(default, default, "synchronizing")) == folder / "synchronizing.ogg"


@pytest.mark.parametrize("description_first,history,expected", [
    (True, [], ""),
    (True, [("user", "internal"), ("assistant", "Description")], "Description"),
    (False, [("user", "Read\nthis text"), ("assistant", "Line 1\nLine 2")],
     "Question:\nRead\nthis text\n\nAnswer:\nLine 1\nLine 2"),
    (True, [("user", "internal"), ("assistant", "Description"), ("user", "internal"), ("assistant", "Reply")],
     "Description\n\nQuestion:\ninternal\n\nAnswer:\nReply"),
])
def test_transcript_rendering_is_presentation_only(description_first, history, expected):
    original = list(history)
    assert render_history(history, description_first=description_first,
                          question_label="Question:", answer_label="Answer:") == expected
    assert history == original


@pytest.mark.parametrize("text,expected", [
    ("", ""),
    (" \n\r\t\u2028\u2029", ""),
    ("Satır bir.\rSatır iki.\n\nSon cümle!", "Satır bir. Satır iki. Son cümle!"),
    ("1. Mavi.\n2. Kırmızı; sarı?", "1. Mavi. 2. Kırmızı; sarı?"),
    ("WINZAPP DEMO 123 — https://example.invalid/a-b", "WINZAPP DEMO 123 — https://example.invalid/a-b"),
])
def test_spoken_answer_only_normalizes_layout_whitespace(text, expected):
    assert spoken_answer(text) == expected
