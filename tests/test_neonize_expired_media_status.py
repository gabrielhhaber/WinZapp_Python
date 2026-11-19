"""An expired CDN link (403) is answered 410 so the client remembers it (issue #747)."""
import sys
from pathlib import Path

import pytest

pytest.importorskip("neonize")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "neonize_bridge"))

import transfers  # noqa: E402


@pytest.mark.parametrize("text", ["HTTP 403", "Forbidden", "404 not found", "status 410",
             "failed to download media from last host: hash of media ciphertext doesn't match",
             "invalid media hmac", "no url present"])
def test_gone_or_expired_media_is_410(text):
    assert transfers.status_for_download_error(Exception(text)) == 410


def test_other_failures_stay_500():
    assert transfers.status_for_download_error(Exception("connection reset")) == 500
