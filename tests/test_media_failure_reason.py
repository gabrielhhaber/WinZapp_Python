"""core/media_failure.py: the sentence for a media the phone could not send
again says why (issue #766: every failure said "the link may have expired or
the connection may be unavailable", with the phone connected and answering)."""
import json

import pytest

from core import media_failure


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(media_failure, "_reasons", type(media_failure._reasons)())


def _msg(mid="M1"):
    return {"key": {"id": mid}}


@pytest.mark.parametrize(("code", "key"), [
    ("media_retry_no_answer", "media_download_phone_no_answer"),
    ("media_retry_unreadable", "media_download_retried_unreadable"),
    ("media_gone_from_phone", "media_download_gone_from_phone"),
])
def test_each_reason_has_its_sentence_once(code, key):
    media_failure.note("M1", json.dumps({"status": "error", "code": code}))
    assert media_failure.failure_key(_msg()) == key
    assert media_failure.failure_key(_msg()) == "media_download_failed"   # used once


def test_without_a_code_the_old_sentence_stays():
    media_failure.note("M1", "Bad gateway")                     # not JSON
    media_failure.note("M2", json.dumps({"status": "error", "code": ""}))   # WPPConnect, others
    assert media_failure.failure_key(_msg("M1")) == "media_download_failed"
    assert media_failure.failure_key(_msg("M2")) == "media_download_failed"
    assert media_failure.failure_key(None) == "media_download_failed"


def test_a_later_answer_replaces_the_reason_and_old_ones_are_dropped():
    media_failure.note("M1", json.dumps({"code": "media_retry_no_answer"}))
    media_failure.note("M1", "plain failure")
    assert media_failure.failure_key(_msg()) == "media_download_failed"
    for i in range(media_failure._KEEP + 5):
        media_failure.note(f"X{i}", json.dumps({"code": "media_retry_no_answer"}))
    assert len(media_failure._reasons) == media_failure._KEEP
    assert media_failure.failure_key(_msg("X0")) == "media_download_failed"


@pytest.mark.parametrize("code", ["en-US", "pt-BR", "pt-PT", "es-ES", "pl", "tr-TR", "ro", "ar", "it"])
def test_every_sentence_exists_in_every_locale(code):
    from tests.test_i18n_keys_exist import _locale
    strings = _locale(code)
    for key in media_failure.REASON_KEYS.values():
        assert strings.get(key), (code, key)
