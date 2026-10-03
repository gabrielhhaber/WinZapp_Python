"""core/claude_client.py mirrors core/gemini_client.py's shape — this file
covers what's specific to it: resolve_model()'s "Automatic" fallback, and the
audio/video guards (the Claude API has no audio input at all, and no raw
video-file input either — see the module docstring), all without any real
network call or API key.
"""

import pytest

from core.claude_client import (
    DEFAULT_MODEL,
    RECOMMENDED_MODELS,
    ClaudeClientError,
    describe_visual_media,
    ask_about_visual_media,
    transcribe_audio,
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
        assert resolve_model("claude-opus-5") == "claude-opus-5"

    def test_surrounding_whitespace_is_stripped(self):
        assert resolve_model("  claude-sonnet-5  ") == "claude-sonnet-5"


class TestTheDefaultIsAlwaysAnOfferedChoice:
    def test_default_model_is_one_of_the_recommended_models(self):
        assert DEFAULT_MODEL in RECOMMENDED_MODELS


class TestAudioIsNotSupported:
    """The Claude API has no audio-input modality at all — unlike Gemini and
    OpenAI, there's no fallback path to skip here, just a clear refusal."""

    def test_transcribe_audio_always_refuses(self):
        with pytest.raises(ClaudeClientError):
            transcribe_audio("/tmp/x.ogg", "fake-key")


class TestVideoIsNotSupported:
    def test_describe_visual_media_refuses_video(self):
        with pytest.raises(ClaudeClientError):
            describe_visual_media("/tmp/x.mp4", "fake-key", is_video=True)

    def test_ask_about_visual_media_refuses_video(self):
        with pytest.raises(ClaudeClientError):
            ask_about_visual_media("/tmp/x.mp4", "fake-key", "o que é isso?", is_video=True)


class TestMissingApiKey:
    def test_describe_visual_media_without_a_key_raises_a_friendly_error(self):
        with pytest.raises(ClaudeClientError):
            describe_visual_media("/tmp/x.jpg", "")
