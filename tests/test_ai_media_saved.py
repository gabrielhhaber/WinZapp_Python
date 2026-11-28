"""The description already stored with a message is reused, never re-requested."""
from types import SimpleNamespace

import pytest

from core.ai_media.saved import saved_description
from core.transcription import stored


def message(text, kind=stored.DESCRIPTION_KIND, deleted=False):
    result = SimpleNamespace(text=text, language=None, language_probability=None,
                             model_id="gemini", backend=stored.ONLINE_BACKEND, vad_used=True)
    value = stored.record_from_result(result, 100.0, inline=True, kind=kind)
    if deleted:
        value = stored.tombstone(200.0)
    return {"messageType": "imageMessage", stored.TRANSCRIPTION_KEY: value}


@pytest.mark.parametrize("kind", ["image", "sticker", "video"])
def test_saved_description_is_returned_for_visual_kinds(kind):
    assert saved_description(message("  A red door. "), kind) == "A red door."


@pytest.mark.parametrize("kind", ["audio", "pdf", "text"])
def test_other_kinds_never_reuse_a_stored_text(kind):
    assert saved_description(message("A red door."), kind) == ""


def test_a_transcription_of_the_sound_is_not_a_description():
    assert saved_description(message("hello there", kind=None), "video") == ""


def test_nothing_stored_or_deleted_gives_nothing():
    assert saved_description({"messageType": "imageMessage"}, "image") == ""
    assert saved_description(message("x", deleted=True), "image") == ""
    assert saved_description(None, "image") == ""
