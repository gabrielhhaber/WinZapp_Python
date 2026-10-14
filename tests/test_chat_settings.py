"""Mute and disappearing-messages lines of the chat-info dialog
(core/chat_settings.py). Plain data in, text out: no wx, no network."""

import pytest

from core.chat_settings import (
    chat_settings_lines,
    disappearing_duration,
    mute_state,
)

PN = "5511999990000@s.whatsapp.net"
LID = "99887766554433@lid"
NOW = 1_800_000_000


class _I18n:
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
    }

    def t(self, key):
        return self.TEXT[key]


def _fmt(ts, i18n):
    return f"T{ts}"


class _Owner:
    """Whatever a test gives it; everything else is absent, like a stub."""

    def __init__(self, muted=None, chat=None, jids=None):
        if muted is not None:
            self._muted_chats = muted
        if jids is not None:
            self._mute_state_jids = lambda jid: jids
        if chat is not None:
            self.get_chat = lambda jid: chat


class TestMuteState:
    def test_nothing_filed_means_not_muted(self):
        assert mute_state({}, [PN], NOW) == ("off", None)

    def test_minus_one_is_muted_for_good(self):
        assert mute_state({PN: -1}, [PN], NOW) == ("forever", None)

    def test_a_future_expiry_is_muted_until_then(self):
        assert mute_state({PN: NOW + 60}, [PN], NOW) == ("until", NOW + 60)

    def test_an_expired_mute_is_not_muted(self):
        assert mute_state({PN: NOW - 1}, [PN], NOW) == ("off", None)
        assert mute_state({PN: NOW}, [PN], NOW) == ("off", None)

    def test_forever_wins_over_a_dated_mute_on_the_other_jid_form(self):
        muted = {PN: NOW + 60, LID: -1}
        assert mute_state(muted, [PN, LID], NOW) == ("forever", None)

    def test_the_longest_of_several_expiries_is_reported(self):
        muted = {PN: NOW + 60, LID: NOW + 600}
        assert mute_state(muted, [PN, LID], NOW) == ("until", NOW + 600)

    def test_only_the_asked_jids_count(self):
        assert mute_state({"other@g.us": -1}, [PN], NOW) == ("off", None)

    @pytest.mark.parametrize("junk", ["-1", "123", None, True, [], {}])
    def test_values_that_are_not_numbers_are_ignored(self, junk):
        assert mute_state({PN: junk}, [PN], NOW) == ("off", None)

    def test_a_missing_map_is_fine(self):
        assert mute_state(None, [PN], NOW) == ("off", None)


class TestDisappearingDuration:
    @pytest.mark.parametrize("seconds", [0, 86400, 604800, 7776000])
    def test_the_timer_is_read_as_seconds(self, seconds):
        assert disappearing_duration({"ephemeralDuration": seconds}) == seconds

    def test_a_float_is_truncated(self):
        assert disappearing_duration({"ephemeralDuration": 86400.0}) == 86400

    def test_a_chat_without_the_field_says_nothing(self):
        assert disappearing_duration({"remoteJid": PN}) is None

    @pytest.mark.parametrize("junk", [None, "86400", True, -5, [], {}])
    def test_unusable_values_say_nothing(self, junk):
        assert disappearing_duration({"ephemeralDuration": junk}) is None

    @pytest.mark.parametrize("chat", [None, [], "x", 5])
    def test_a_chat_that_is_not_a_dict_says_nothing(self, chat):
        assert disappearing_duration(chat) is None


class TestLines:
    def _lines(self, owner, jid=PN):
        return chat_settings_lines(owner, jid, _I18n(), _fmt, now=NOW)

    def test_a_plain_chat(self):
        owner = _Owner(muted={}, chat={"ephemeralDuration": 0})
        assert self._lines(owner) == [
            "Notifications: Not muted",
            "Disappearing messages: Off",
        ]

    def test_muted_for_good_and_a_week_timer(self):
        owner = _Owner(muted={PN: -1}, chat={"ephemeralDuration": 604800})
        assert self._lines(owner) == [
            "Notifications: Muted",
            "Disappearing messages: 7 days",
        ]

    def test_muted_until_a_formatted_time(self):
        owner = _Owner(muted={PN: NOW + 3600})
        assert self._lines(owner) == [f"Notifications: Muted until T{NOW + 3600}"]

    def test_a_time_that_cannot_be_formatted_falls_back_to_plain_muted(self):
        owner = _Owner(muted={PN: NOW + 3600})
        lines = chat_settings_lines(owner, PN, _I18n(), lambda ts, i18n: "", now=NOW)
        assert lines == ["Notifications: Muted"]

    @pytest.mark.parametrize(
        "seconds,text",
        [(86400, "24 hours"), (604800, "7 days"), (7776000, "90 days"), (1234, "On")],
    )
    def test_each_timer_reads_as_its_own_label(self, seconds, text):
        owner = _Owner(chat={"ephemeralDuration": seconds})
        assert self._lines(owner) == [f"Disappearing messages: {text}"]

    def test_the_other_jid_form_of_the_mute_is_honoured(self):
        owner = _Owner(muted={LID: -1}, jids={PN, LID})
        assert self._lines(owner) == ["Notifications: Muted"]

    def test_no_data_means_no_claim(self):
        # No mute map and no chat: nothing is said, in particular not
        # "not muted" and not "off".
        assert self._lines(_Owner()) == []

    def test_a_chat_without_the_timer_adds_only_the_mute_line(self):
        owner = _Owner(muted={}, chat={"remoteJid": PN})
        assert self._lines(owner) == ["Notifications: Not muted"]

    def test_a_mute_map_of_the_wrong_type_is_not_trusted(self):
        owner = _Owner()
        owner._muted_chats = ["not", "a", "dict"]
        assert self._lines(owner) == []

    def test_a_lookup_that_returns_nothing_still_checks_the_chat_jid(self):
        owner = _Owner(muted={PN: -1}, jids=set())
        assert self._lines(owner) == ["Notifications: Muted"]
