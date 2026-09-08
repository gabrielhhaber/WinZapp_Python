"""Tests for whether a stored WPPConnect token may be reused for a new pairing.

_bg_pairing_flow() persists WA_token as soon as WPPConnect emits a pairing code
— well before the pairing completes. The reuse check used to accept any stored
token for a matching number, which broke the cancel-and-retry flow outright:

  1. "Continuar" → code ABCD1234 appears, WA_token is persisted.
  2. "Cancelar pareamento" → cleanup_pairing_session() calls /close-session and
     WPPConnect drops that token from clientsArray.
  3. "Continuar" again → the stale token still matched, so the flow reused it
     and called /start-session on a session that no longer existed. No code was
     ever emitted; the dialog sat on "Conectando..." for the full 90 s wait.

The same applies when the app is killed mid-pairing, where no cleanup runs at
all. Requiring `paired` — which is only ever set once WhatsApp is genuinely
linked — is what separates "a session worth reusing" from "a token left over
from an attempt that never finished".

Connect is a plain class, so the check is exercised directly.
"""

import pytest

from ui.dialogs.connect import Connect


can_reuse = Connect._can_reuse_existing_session

PHONE = "5511999999999"
TOKEN = "0000000000000000000000000000dead:$2b$10$abc"


def _priv(**overrides):
    """A privateinfo dict for a fully paired account, before overrides.

    No WA_token key: the token lives Fernet-protected under
    WA_token_protected and reaches the check as an argument, read by
    MainWindow._get_wa_token() (see core/token_vault.py).
    """
    base = {
        "WA_phone_number": PHONE,
        "paired": True,
    }
    base.update(overrides)
    return base


def test_reuses_a_genuinely_paired_session():
    assert can_reuse(_priv(), PHONE, TOKEN) is True


def test_rejects_a_token_from_a_cancelled_pairing():
    """The reported bug: code shown, cancelled, retried. The token is present
    because it was persisted optimistically, but pairing never completed."""
    assert can_reuse(_priv(paired=False), PHONE, TOKEN) is False


def test_rejects_when_paired_key_is_absent_entirely():
    """cleanup_pairing_session() pops the key rather than setting it False."""
    priv = _priv()
    del priv["paired"]
    assert can_reuse(priv, PHONE, TOKEN) is False


def test_rejects_a_different_number():
    assert can_reuse(_priv(), "5521888888888", TOKEN) is False


def test_rejects_when_no_token_is_stored():
    """Also covers a protected token that failed to decrypt — _get_wa_token()
    returns "" for that too, deliberately indistinguishable from "no token"."""
    assert can_reuse(_priv(), PHONE, "") is False


def test_ignores_a_legacy_plaintext_token_left_in_privateinfo():
    """A stale plaintext WA_token in settings must not stand in for the real
    (protected) one — only what _get_wa_token() returns counts."""
    assert can_reuse(_priv(WA_token=TOKEN), PHONE, "") is False


def test_rejects_an_empty_privateinfo():
    assert can_reuse({}, PHONE, TOKEN) is False


def test_rejects_a_missing_privateinfo():
    assert can_reuse(None, PHONE, TOKEN) is False


def test_rejects_an_empty_phone_number():
    """An empty field must never match an empty stored number."""
    assert can_reuse(_priv(WA_phone_number=""), "", TOKEN) is False


@pytest.mark.parametrize("stored,typed", [
    ("+55 11 99999-9999", "5511999999999"),
    ("5511999999999", "+55 (11) 99999-9999"),
    ("+55 11 99999-9999", "+55 11 99999-9999"),
])
def test_number_comparison_ignores_formatting(stored, typed):
    """The stored number keeps whatever formatting the user typed; both sides
    are normalised to digits before comparing."""
    assert can_reuse(_priv(WA_phone_number=stored), typed, TOKEN) is True


class TestOnlyTheVerySameNumberResumesASession:
    """WA_phone_number holds what the user typed into this dialog and nothing
    else, so the comparison is exact — and briefly was not.

    A tolerant version was introduced here while the wipe check still compared
    a typed number against the one WhatsApp reported; it accepted any pair one
    digit apart whose first two digits and last eight matched. "The first two
    digits are the country code" is false for every three-digit dialling code,
    and the pairs below are real, distinct subscribers it collapsed. Reusing a
    session across one of them is worse than the missing wipe it was meant to
    avoid: WinZapp connects the account belonging to the other number, without
    deleting anything, and shows it as the user's own.

    The wipe check now compares a confirmed number against a confirmed number
    (WA_phone_number_linked, tests/test_another_number_linked_wipe.py), which
    is what removed the reason for any tolerance here.
    """

    COLLIDING = [
        # +49 211 1234567 vs +49 211 234567 — two Düsseldorf subscribers.
        ("492111234567", "49211234567"),
        # +43 1 ... — two Vienna landlines.
        ("431123456789", "43123456789"),
        # Italian mobiles, 10 and 9 digits.
        ("393331234567", "39331234567"),
        # A Portuguese mobile against a Sofia landline: different countries.
        ("351924567890", "35924567890"),
    ]

    def test_a_number_one_digit_away_never_resumes(self):
        for stored, typed in self.COLLIDING:
            assert can_reuse(_priv(WA_phone_number=stored), typed, TOKEN) is False
            assert can_reuse(_priv(WA_phone_number=typed), stored, TOKEN) is False

    def test_the_brazilian_ninth_digit_form_is_not_the_typed_number_either(self):
        """The user types what they type; nothing else writes this field."""
        assert can_reuse(_priv(WA_phone_number="551199999999"),
                         "5511999999999", TOKEN) is False

    def test_the_same_number_still_resumes(self):
        assert can_reuse(_priv(WA_phone_number="5511999999999"),
                         "5511999999999", TOKEN) is True
