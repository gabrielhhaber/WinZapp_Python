"""The media-download error no longer blames WPPConnect, which the Neonize engine lacks (#747)."""
from pathlib import Path

import pytest

from tests.locales import registered_locale_codes

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("locale", registered_locale_codes())
def test_message_does_not_name_wppconnect(locale):
    po = (ROOT / "translations" / locale / "LC_MESSAGES" / "winzapp.po").read_text(encoding="utf-8")
    start = po.index('msgctxt "media_download_failed"')
    entry = po[start:po.index("\n\n", start)]
    assert "WPPConnect" not in entry
