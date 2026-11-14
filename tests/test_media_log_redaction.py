"""The media request's log line never carries the session token.

log.log of 2026-11-11 had "[get_base64_from_media] Requesting media for ...,
url=http://127.0.0.1:6300/api/<session>:$2b$10$<token>/get-media-by-message/":
WPPConnect's credential sits in the URL path, and users send that log in
their reports (docs/traps/log-pii.md)."""
from pathlib import Path

from core.api_client import redact_api_url

MEDIA = Path(__file__).resolve().parents[1] / "client" / "main_window" / "media.py"


def test_the_request_line_logs_the_redacted_url():
    source = MEDIA.read_text(encoding="utf-8")
    start = source.index("[get_base64_from_media] Requesting media")
    call = source[start:source.index(")", source.index("media_type", start))]
    assert "redact_api_url(url)" in call


def test_the_redacted_url_has_no_token():
    url = "http://127.0.0.1:6300/api/abc:$2b$10$O.P2K9TMErPUzZB/get-media-by-message/"
    shown = redact_api_url(url)
    assert "$2b$" not in shown and "abc:" not in shown and "get-media-by-message" in shown
