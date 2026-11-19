"""HTTP 402 is an account without credit, not a bad request. Reported: the
image description said "The provider rejected the request. Check the model
identifier and settings." while OpenRouter answered 402 three times in a row."""

from core.ai_media import auto
from core.ai_media.errors import DescriptionError, status_error
from core.ai_media.service import _NEXT_PROVIDER


def test_402_is_credits():
    assert status_error(402).category == "credits"
    assert status_error(402).key == "ai_error_credits"
    assert status_error(400).category == "request"


def test_credits_move_the_chain_on_and_pause_the_automatic_descriptions():
    assert "credits" in _NEXT_PROVIDER
    assert auto.pauses(DescriptionError("credits"))
