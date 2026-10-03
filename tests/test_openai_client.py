"""core/openai_client.py mirrors core/gemini_client.py's shape — this file
covers what's specific to it: resolve_model()'s "Automatic" fallback, and the
video guard (the OpenAI vision API has no video-file input, unlike Gemini —
see the module docstring), all without any real network call or API key.
"""

import pytest

from core.openai_client import (
    DEFAULT_MODEL,
    RECOMMENDED_MODELS,
    OpenAIClientError,
    describe_visual_media,
    ask_about_visual_media,
    resolve_model,
)


class TestAutomaticFollowsTheDefault:
    def test_none_falls_back_to_the_default(self):
        assert resolve_model(None) == DEFAULT_MODEL

    def test_empty_string_falls_back_to_the_default(self):
        assert resolve_model("") == DEFAULT_MODEL

    def test_whitespace_only_falls_back_to_the_default(self):
        assert resolve_model("   ") == DEFAULT_MODEL


class TestAConfiguredModelIsPinned:
    def test_a_chosen_model_is_used_as_is(self):
        assert resolve_model("gpt-4.1") == "gpt-4.1"

    def test_surrounding_whitespace_is_stripped(self):
        assert resolve_model("  gpt-4o  ") == "gpt-4o"


class TestTheDefaultIsAlwaysAnOfferedChoice:
    def test_default_model_is_one_of_the_recommended_models(self):
        assert DEFAULT_MODEL in RECOMMENDED_MODELS


class TestVideoIsNotSupported:
    """The OpenAI chat/vision API only accepts images, never a raw video
    file — core/ai_providers.py is supposed to never route video here, but
    both functions guard themselves too, in case a future caller forgets."""

    def test_describe_visual_media_refuses_video(self):
        with pytest.raises(OpenAIClientError):
            describe_visual_media("/tmp/x.mp4", "fake-key", is_video=True)

    def test_ask_about_visual_media_refuses_video(self):
        with pytest.raises(OpenAIClientError):
            ask_about_visual_media("/tmp/x.mp4", "fake-key", "o que é isso?", is_video=True)


class TestMissingApiKey:
    def test_describe_visual_media_without_a_key_raises_a_friendly_error(self):
        with pytest.raises(OpenAIClientError):
            describe_visual_media("/tmp/x.jpg", "")
