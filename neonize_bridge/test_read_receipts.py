"""Read receipts follow the account's privacy setting (read_receipts.py).

Reported: with read receipts off in WhatsApp's privacy settings, contacts
still saw blue ticks for messages read in WinZapp on Neonize."""
from types import SimpleNamespace

from neonize.utils.enum import ReceiptType

import read_receipts as rr

ALL, NONE = 2, 7     # PrivacySettings.PrivacySetting values (profile_privacy.py)


def test_hidden_read_receipts_go_to_the_own_devices_only():
    assert rr.receipt_name("55@s.whatsapp.net", "READ", "none") == "READ_SELF"
    assert rr.receipt_name("77@lid", "PLAYED", "none") == "PLAYED_SELF"


def test_groups_always_get_the_receipt_as_on_the_phone():
    assert rr.receipt_name("1203@g.us", "READ", "none") == "READ"


def test_shown_or_unknown_read_receipts_keep_the_plain_receipt():
    assert rr.receipt_name("55@s.whatsapp.net", "READ", "all") == "READ"
    assert rr.receipt_name("55@s.whatsapp.net", "READ", None) == "READ"


def _client(value):
    return SimpleNamespace(get_privacy_settings=lambda: SimpleNamespace(ReadReceipts=value))


def test_the_setting_is_read_from_whatsmeow_and_an_unknown_one_keeps_the_last():
    assert rr.read_receipts_setting(_client(NONE)) == "none"
    assert rr.read_receipts_setting(_client(ALL), "none") == "all"
    # whatsmeow answers an empty struct when its fetch failed
    assert rr.read_receipts_setting(_client(0), "none") == "none"

    def broken():
        raise RuntimeError("not connected")

    assert rr.read_receipts_setting(SimpleNamespace(get_privacy_settings=broken), "all") == "all"


def test_the_session_sends_read_self_with_receipts_hidden():
    session = rr.ReadReceipts()
    session.client = _client(NONE)
    assert session.receipt_for("55@s.whatsapp.net", "READ") is ReceiptType.READ_SELF
    assert session.receipt_for("1203@g.us", "READ") is ReceiptType.READ
    session.client = _client(0)     # a failed fetch: the last value known holds
    assert session.receipt_for("55@s.whatsapp.net", "PLAYED") is ReceiptType.PLAYED_SELF


def test_hidden_read_receipts_hide_the_contacts_reads_too():
    # bilateral: with the setting off we do not see their blue ticks either
    assert rr.incoming_kind("55@s.whatsapp.net", "READ", "none") == "DELIVERED"
    assert rr.incoming_kind("77@lid", "PLAYED", "none") == "DELIVERED"
    assert rr.incoming_kind("55@s.whatsapp.net", "DELIVERED", "none") == "DELIVERED"


def test_incoming_reads_stay_in_groups_and_when_shown_or_unknown():
    assert rr.incoming_kind("1203@g.us", "READ", "none") == "READ"
    assert rr.incoming_kind("55@s.whatsapp.net", "READ", "all") == "READ"
    assert rr.incoming_kind("55@s.whatsapp.net", "PLAYED", None) == "PLAYED"
