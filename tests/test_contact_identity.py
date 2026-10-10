"""Identity lines of the chat-info dialog (core/contact_identity.py).

Plain functions over a dict and an injected translator: no network, no wx.
"""

import pytest

from core.contact_identity import contact_identity_lines

LABELS = {
    "contact_profile_name_label": "Profile name",
    "contact_verified_name_label": "Verified name",
    "contact_business_account": "Business account",
}


class _I18n:
    @staticmethod
    def t(key):
        return LABELS[key]


_t = _I18n()


def test_a_personal_contact_with_a_profile_name_gets_one_line():
    lines = contact_identity_lines({"pushname": "Ana"}, "Ana Silva", _t)
    assert lines == ["Profile name: Ana"]


def test_a_profile_name_that_repeats_the_shown_name_is_not_read_twice():
    assert contact_identity_lines({"pushname": "ana silva"}, "Ana Silva", _t) == []


def test_a_business_gets_verified_name_then_account_type():
    cdata = {"pushname": "Loja", "verifiedName": "Loja Oficial Ltda", "isBusiness": True}
    lines = contact_identity_lines(cdata, "Atendimento", _t)
    assert lines == [
        "Profile name: Loja",
        "Verified name: Loja Oficial Ltda",
        "Business account",
    ]


def test_a_verified_name_equal_to_the_profile_name_is_listed_once():
    cdata = {"pushname": "Loja X", "verifiedName": "Loja X"}
    assert contact_identity_lines(cdata, "", _t) == ["Profile name: Loja X"]


def test_a_business_flag_must_be_a_real_true():
    for not_true in ("false", "true", 1, None, 0, False):
        assert contact_identity_lines({"isBusiness": not_true}, "X", _t) == []


@pytest.mark.parametrize("cdata", [None, [], "x", 5, {}, {"pushname": None}])
def test_missing_or_malformed_replies_produce_no_lines(cdata):
    assert contact_identity_lines(cdata, "X", _t) == []


@pytest.mark.parametrize("value", [None, 5, True, [], {}, "", "   "])
def test_non_text_or_blank_names_are_ignored(value):
    cdata = {"pushname": value, "verifiedName": value}
    assert contact_identity_lines(cdata, "X", _t) == []


def test_surrounding_whitespace_is_trimmed():
    assert contact_identity_lines({"pushname": "  Ana  "}, "Maria", _t) == [
        "Profile name: Ana"
    ]


def test_a_missing_shown_name_still_lists_the_profile_name():
    assert contact_identity_lines({"pushname": "Ana"}, None, _t) == ["Profile name: Ana"]
