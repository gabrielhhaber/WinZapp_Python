"""Localized photo guidance and operation labels; data only, no windows or audio."""
import json
from pathlib import Path

import pytest


LANGUAGES = Path(__file__).resolve().parents[1] / "client" / "languages"
LOCALES = tuple(json.loads((LANGUAGES / "language_map.json").read_text(encoding="utf-8")))


@pytest.fixture(params=LOCALES)
def translations(request):
    return json.loads((LANGUAGES / f"{request.param}.json").read_text(encoding="utf-8"))


def test_settings_guidance_is_split_into_four_readable_paragraphs(translations):
    paragraphs = translations["ai_settings_notice"].split("\n\n")
    assert len(paragraphs) == 4 and all(paragraph.strip() for paragraph in paragraphs)
    assert "API" in paragraphs[0]
    assert "ChatGPT" in paragraphs[2]
    assert "Gemini" in paragraphs[3]
    assert "store=false" not in translations["ai_settings_notice"]


def test_technical_information_retains_key_cache_and_provider_limits(translations):
    paragraphs = translations["ai_technical_notice"].split("\n\n")
    assert len(paragraphs) == 2 and all(paragraph.strip() for paragraph in paragraphs)
    assert "WhatsApp" in paragraphs[0]
    assert "store=false" in paragraphs[1] and "Gemini" in paragraphs[1]
    assert translations["ai_technical_info"].strip()


def test_operation_statuses_are_translated_distinct_and_do_not_expose_internal_keys(translations):
    keys = ("ai_description_loading", "ai_question_loading", "ai_connection_loading")
    values = [translations[key] for key in keys]
    assert len(set(values)) == 3
    assert all(value.strip() and value not in keys for value in values)
    assert translations["ai_settings_help"] != translations["ai_privacy_link"]


def test_consent_still_names_the_provider_and_retains_explicit_confirmation(translations):
    text = translations["ai_consent"].format(provider="SYNTHETIC PROVIDER")
    assert "SYNTHETIC PROVIDER" in text and "API" in text
    assert translations["ai_remember_consent"].strip()
    assert translations["ai_locked_consent"].strip()


def test_key_removal_guidance_names_both_save_actions(translations):
    text = translations["ai_key_removal_notice"]
    assert translations["apply"].replace("&", "") in text
    assert translations["ok"].replace("&", "") in text
    assert translations["cancel"].replace("&", "") in text
    assert "WinZapp" in text and "API" in text
    assert translations["ai_key_removal_pending"].strip()
