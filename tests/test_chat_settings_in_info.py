"""Mute and disappearing-messages lines reach the personal chat-info text.

Same harness as test_contact_presence_views.py: the unbound dialog method on a
plain stand-in, no wx.App. The rules themselves are in test_chat_settings.py;
this pins the wiring and the "no data, no claim" behaviour on the real method.
"""
import time

from ui.dialogs.conversation_data_dialog import ConversationDataDialog
from tests.test_contact_presence_freshness import Owner, PN
from tests.test_contact_presence_views import views


class _Labels:
    TEXT = {
        "chat_notifications_label": "Notifications",
        "chat_notifications_not_muted": "Not muted",
        "chat_notifications_muted": "Muted",
        "chat_notifications_muted_until": "Muted until {when}",
        "chat_disappearing_label": "Disappearing messages",
        "chat_disappearing_off": "Off",
        "chat_disappearing_on": "On",
        "chat_disappearing_24h": "24 hours",
        "chat_disappearing_7d": "7 days",
        "chat_disappearing_90d": "90 days",
        "datetime_fmt": "%d/%m/%Y %H:%M",
        "chat_encryption_label": "Encryption",
        "chat_encryption_notice": "Messages and calls are end-to-end encrypted.",
    }

    def __init__(self, base):
        self._base = base

    def t(self, key):
        return self.TEXT.get(key) or self._base.t(key)


def _info_lines(owner):
    _panel, dialog, _button, text, _layouts = views(owner)
    dialog._i18n = _Labels(dialog._i18n)
    ConversationDataDialog._populate_personal_unsafe(dialog, {})
    return text.text.splitlines()


def test_muted_for_good_with_a_week_timer_shows_both_lines():
    owner = Owner()
    owner._muted_chats = {PN: -1}
    owner.get_chat = lambda jid: {"ephemeralDuration": 604800}
    lines = _info_lines(owner)
    assert "Notifications: Muted" in lines
    assert "Disappearing messages: 7 days" in lines


def test_a_mute_that_expires_later_names_the_time():
    owner = Owner()
    owner._muted_chats = {PN: int(time.time()) + 3600}
    lines = _info_lines(owner)
    assert any(line.startswith("Notifications: Muted until ") for line in lines)


def test_not_muted_and_timer_off():
    owner = Owner()
    owner._muted_chats = {}
    owner.get_chat = lambda jid: {"ephemeralDuration": 0}
    lines = _info_lines(owner)
    assert "Notifications: Not muted" in lines
    assert "Disappearing messages: Off" in lines


def test_without_the_data_the_dialog_claims_nothing():
    lines = _info_lines(Owner())
    assert not any(line.startswith(("Notifications", "Disappearing")) for line in lines)


def test_a_personal_chat_states_that_it_is_end_to_end_encrypted():
    lines = _info_lines(Owner())
    assert "Encryption: Messages and calls are end-to-end encrypted." in lines
