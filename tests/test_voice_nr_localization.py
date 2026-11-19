"""List choices are plain text, not native label mnemonics."""
from pathlib import Path

import polib
import pytest

ROOT = Path(__file__).resolve().parents[1]
CHOICE_KEYS = (
    "voice_nr_engine_basic", "voice_nr_engine_neural",
    "voice_nr_moment_record", "voice_nr_moment_send",
)
CATALOGS = sorted((ROOT / "translations").glob("*/LC_MESSAGES/winzapp.po"))


@pytest.mark.parametrize("path", CATALOGS, ids=lambda p: p.parents[1].name)
def test_noise_reduction_combo_choices_have_no_mnemonic_markers(path):
    catalog = polib.pofile(str(path), check_for_duplicates=True)
    entries = {entry.msgctxt: entry for entry in catalog if not entry.obsolete}
    for key in CHOICE_KEYS:
        entry = entries[key]
        assert entry.msgstr.strip() and not entry.fuzzy
        assert "&" not in entry.msgid, (path, key, entry.msgid)
        assert "&" not in entry.msgstr, (path, key, entry.msgstr)
