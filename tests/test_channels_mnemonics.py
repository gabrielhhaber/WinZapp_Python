"""Keyboard mnemonics of the Channels window (Alt+9), in every locale.

The search field, the list, the details box and Close each carry an
Alt+letter; a letter shared inside the window only cycles the focus between
the controls, and no screen reader says so. The channel edit dialog's two
fields likewise. Which letter a locale picks is its own decision; this checks
only that the picks do not collide.
"""

import pytest

from tests.locales import registered_locale_codes
from tests.mnemonics import load_strings, mnemonic

WINDOW_KEYS = ("channels_search", "channels_list", "channels_details", "close")
EDIT_KEYS = ("channels_name", "channels_description")


@pytest.fixture(params=registered_locale_codes())
def strings(request):
    return load_strings(request.param)


@pytest.mark.parametrize("keys", [WINDOW_KEYS, EDIT_KEYS])
def test_every_labelled_control_has_its_own_letter(strings, keys):
    letters = [mnemonic(strings[k]) for k in keys]
    assert None not in letters, dict(zip(keys, letters))
    assert len(set(letters)) == len(letters), dict(zip(keys, letters))
