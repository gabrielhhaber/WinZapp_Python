"""Operational logs must not accept content, credentials or response bodies."""
import logging

from core.image_description.diagnostics import record


def test_allowlisted_diagnostics_only_record_operational_fields(caplog):
    with caplog.at_level(logging.INFO, logger="winzapp.photo_description"):
        record("response_headers", status=429, generation=2, category="quota")
    assert "response_headers http=429 generation=2 category=quota" in caplog.text


def test_private_strings_and_unknown_fields_cannot_enter_log(caplog):
    with caplog.at_level(logging.INFO, logger="winzapp.photo_description"):
        record("private-key-secret")
        record("worker_finished", status="private-key-secret", generation="private-question",
               category="private-answer", exception_type="private-response-body")
    assert "worker_finished exception=other" in caplog.text
    for private in ("private-key-secret", "private-question", "private-answer", "private-response-body"):
        assert private not in caplog.text
