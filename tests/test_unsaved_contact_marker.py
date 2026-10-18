"""The "~" before the name of a contact that is not saved (Settings > User interface).

Plain stubs, no wx, account or database: the real marker and the real
sender-label funnel run against a stand-in main window.
"""
from core.message_sender_labels import message_sender_label
from core.unsaved_contact_marker import (
    contact_is_saved, is_unsaved_contact, mark_unsaved_name, unsaved_marker_enabled,
)
from core.utils import DEFAULT_SETTINGS, backfill_missing_defaults, is_phone_like

PHONE = "5511900000002@s.whatsapp.net"
LID = "222222222222222@lid"
GROUP = "120000000000000@g.us"
ME = "5511900000001@s.whatsapp.net"


class _I18n:
    def t(self, key):
        return {"unknown_contact": "Unknown contact", "unnamed_participant": "Unnamed",
                "unknown_group": "Unknown group", "self_chat_name": "Me (messages to myself)"
                }.get(key, key)


class _MW:
    def __init__(self, enabled=True, contacts=None):
        self.settings = {"user_interface": {"mark_unsaved_contacts": enabled}}
        self.i18n = _I18n()
        self.contacts = contacts or {}
        self._lid_to_phone = {}
        self._phone_to_lid = {}

    def _is_self_jid(self, jid):
        return jid == ME

    def _is_bad_contact_name(self, name):
        return not name.strip() or is_phone_like(name) or "unknown" in name.lower()

    def _get_contact_tolerant(self, jid):
        return self.contacts.get(jid)


def test_unsaved_contact_gets_the_tilde():
    mw = _MW(contacts={PHONE: {"pushName": "Ana"}})
    assert mark_unsaved_name(mw, PHONE, "Ana") == "~Ana"


def test_contact_without_a_record_is_unsaved():
    assert mark_unsaved_name(_MW(), PHONE, "Ana") == "~Ana"


def test_each_kind_of_saved_contact_keeps_the_plain_name():
    for record in ({"isMyContact": True}, {"isSaved": True}, {"syncedToPhone": True}):
        mw = _MW(contacts={PHONE: record})
        assert contact_is_saved(record)
        assert mark_unsaved_name(mw, PHONE, "Ana") == "Ana"


def test_lid_follows_the_saved_phone_record():
    mw = _MW(contacts={PHONE: {"isMyContact": True}})
    mw._lid_to_phone[LID] = PHONE
    assert not is_unsaved_contact(mw, LID)
    assert mark_unsaved_name(mw, LID, "Ana") == "Ana"


def test_phone_follows_a_saved_lid_record():
    mw = _MW(contacts={LID: {"isSaved": True}})
    mw._phone_to_lid[PHONE] = LID
    assert mark_unsaved_name(mw, PHONE, "Ana") == "Ana"


def test_option_off_leaves_every_name_alone():
    assert mark_unsaved_name(_MW(enabled=False), PHONE, "Ana") == "Ana"
    assert not unsaved_marker_enabled({"user_interface": {"mark_unsaved_contacts": False}})
    assert unsaved_marker_enabled({"user_interface": {}})
    assert unsaved_marker_enabled({})


def test_option_is_on_by_default_and_backfilled():
    assert DEFAULT_SETTINGS["user_interface"]["mark_unsaved_contacts"] is True
    settings = {"user_interface": {}}
    backfill_missing_defaults(settings, DEFAULT_SETTINGS)
    assert settings["user_interface"]["mark_unsaved_contacts"] is True


def test_groups_self_system_account_and_placeholders_are_never_marked():
    mw = _MW()
    assert mark_unsaved_name(mw, GROUP, "Family") == "Family"
    assert mark_unsaved_name(mw, ME, "Me") == "Me"
    assert mark_unsaved_name(mw, "0@s.whatsapp.net", "WhatsApp") == "WhatsApp"
    assert mark_unsaved_name(mw, PHONE, "+55 11 90000-0002") == "+55 11 90000-0002"
    assert mark_unsaved_name(mw, PHONE, "Unknown contact") == "Unknown contact"
    assert mark_unsaved_name(mw, PHONE, "Me (messages to myself)") == "Me (messages to myself)"
    assert mark_unsaved_name(mw, PHONE, "") == ""


def test_a_name_is_never_marked_twice():
    assert mark_unsaved_name(_MW(), PHONE, "~Ana") == "~Ana"


def test_message_sender_label_marks_an_unsaved_sender_only():
    msg = {"key": {"remoteJid": GROUP, "participant": PHONE}}
    assert message_sender_label("Ana", msg, _MW(), _I18n()) == "~Ana"
    saved = _MW(contacts={PHONE: {"isMyContact": True}})
    assert message_sender_label("Ana", msg, saved, _I18n()) == "Ana"
    own = {"key": {"remoteJid": PHONE, "fromMe": True}}
    assert message_sender_label("Me", own, _MW(), _I18n()) == "Me"
    assert message_sender_label("Ana", msg, _MW(enabled=False), _I18n()) == "Ana"


def test_private_message_uses_the_chat_jid():
    msg = {"key": {"remoteJid": PHONE}}
    assert message_sender_label("Ana", msg, _MW(), _I18n()) == "~Ana"
